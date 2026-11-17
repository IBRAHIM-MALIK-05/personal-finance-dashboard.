"""Independent acceptance cases from the handbook and architecture agreements."""

from copy import deepcopy
from datetime import date

import pytest

from app.engine.budgets import spend_in
from app.engine.dates import DateParseError, parse_flexible
from app.engine.forecast import AssumptionSet, build_forecast, variable_assumptions
from app.engine.importer import ImportFatal, Settings, analyse, read_csv
from app.engine.models import Adjustment, RecurringBill, Txn
from app.engine.money import AmountError, parse_amount
from app.engine.recurring import occurrences
from app.engine.scenarios import run


AS_OF = date(2026, 11, 15)
NO_SPENDING = AssumptionSet([], 3, [], False)


@pytest.mark.parametrize("text,expected", [
    ("0.01", 1), ("Rs. 1,200.50", 120050), ("1,20,000", 12000000),
    ("(850)", -85000), ("۱۲۳٫۴۵", 12345),
])
def test_money_preserves_minor_units(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["NaN", "inf", "1e6", "1.001", "100000001", "1,2,3", "1 2", "--10", "-10-"])
def test_invalid_money_is_not_silently_reinterpreted(text):
    with pytest.raises(AmountError):
        parse_amount(text)


def test_ambiguous_date_requires_a_choice():
    result = parse_flexible("03/04/2026")
    assert result.value is None
    assert set(result.candidates) == {date(2026, 4, 3), date(2026, 3, 4)}
    assert parse_flexible("03/04/2026", "DMY").value == date(2026, 4, 3)


def test_impossible_date_has_a_recoverable_error():
    with pytest.raises(DateParseError):
        parse_flexible("31/02/2026")


def test_month_end_schedule_does_not_drift_after_february():
    bill = RecurringBill(1, "Rent", "Landlord", "housing", -100000, "monthly", date(2024, 1, 31))
    assert occurrences(bill, date(2024, 1, 1), date(2024, 3, 31)) == [
        date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31),
    ]


def test_refund_offsets_expense_and_transfers_are_excluded():
    txns = [
        Txn(1, AS_OF, -100000, "Shop", category_id="shopping"),
        Txn(2, AS_OF, 25000, "Shop", category_id="shopping", kind="refund"),
        Txn(3, AS_OF, -500000, "Savings", category_id="shopping", kind="transfer"),
    ]
    assert spend_in(txns, "shopping", AS_OF, AS_OF) == 75000


def test_opening_balance_and_forecast_cutoff_exclude_other_periods():
    txns = [
        Txn(1, date(2026, 10, 31), -90000, "Before opening"),
        Txn(2, AS_OF, -20000, "Posted"),
        Txn(3, date(2026, 11, 16), -70000, "Future"),
    ]
    result = build_forecast(opening_balance=100000, opening_date=date(2026, 11, 1),
                            txns=txns, bills=[], as_of=AS_OF, assumptions=NO_SPENDING)
    assert result.balance_now.value == 80000
    assert result.history[-1] == (AS_OF, 80000)


@pytest.mark.parametrize("linked,expected_events", [(True, 0), (False, 1)])
def test_only_confirmed_payment_suppresses_recurring_forecast(linked, expected_events):
    due = date(2026, 11, 16)
    bill = RecurringBill(1, "Phone", "Provider", "phone_internet", -10000, "monthly", due)
    payment = Txn(1, AS_OF, -10000, "Provider", category_id="phone_internet",
                  recurring_id=1 if linked else None, occurrence_date=due if linked else None)
    result = build_forecast(opening_balance=100000, opening_date=date(2026, 11, 1),
                            txns=[payment], bills=[bill], as_of=AS_OF,
                            horizon_days=2, assumptions=NO_SPENDING)
    assert len(result.events) == expected_events
    assert result.closing.value == 90000 - expected_events * 10000


def test_sparse_history_is_labelled_limited():
    result = variable_assumptions([Txn(1, AS_OF, -10000, "Food", category_id="dining")], [], AS_OF)
    assert result.limited
    assert result.months_available == 0


def test_user_total_spending_assumption_replaces_history_instead_of_adding_to_it():
    txns = [Txn(1, AS_OF, -10000, "Food", category_id="dining")]
    result = variable_assumptions(txns, [], AS_OF, overrides={"__total__": 100000})
    assert result.total("base") == 100000
    assert result.total("low") == result.total("high") == 100000
    assert not result.limited


def test_what_if_changes_future_without_mutating_inputs():
    bill = RecurringBill(1, "Rent", "Landlord", "housing", -100000, "monthly", date(2026, 11, 20))
    txns = [Txn(1, AS_OF, 500000, "Salary", category_id="salary")]
    original = deepcopy((bill, txns, NO_SPENDING))
    common = dict(opening_balance=0, opening_date=date(2026, 11, 1), txns=txns,
                  bills=[bill], as_of=AS_OF, horizon_days=10, assumptions=NO_SPENDING)
    baseline = build_forecast(**common)
    alternative = run(**common, adjustments=[Adjustment("cancel_bill", bill_id=1)])
    assert alternative.closing.value - baseline.closing.value == 100000
    assert alternative.balance_now.value == baseline.balance_now.value
    assert (bill, txns, NO_SPENDING) == original


def test_negative_source_refund_becomes_cash_in_under_declared_convention():
    original = Txn(1, date(2026, 11, 1), -100000, "Shop", category_id="shopping")
    result = analyse("date,payee,description,amount\n2026-11-02,Shop,Refund,-250\n",
                     Settings(sign="positive_is_out"), [original], [], AS_OF)
    assert result.can_commit
    row = result.to_import[0]
    assert (row.amount, row.kind, row.category_id) == (25000, "refund", "shopping")


def test_duplicate_candidates_need_explicit_decisions_and_can_be_legitimate():
    csv = "date,payee,amount\n2026-11-01,Cafe,-500\n2026-11-01,Cafe,-500\n"
    unresolved = analyse(csv, Settings(sign="negative_is_out"), [], [], AS_OF)
    assert not unresolved.can_commit
    accepted = analyse(csv, Settings(sign="negative_is_out", decisions={3: "import"}), [], [], AS_OF)
    assert accepted.can_commit
    assert len(accepted.to_import) == 2


def test_import_confirmation_cannot_bypass_unresolved_date():
    csv = "date,payee,amount\n03/04/2026,Cafe,-500\n"
    result = analyse(csv, Settings(sign="negative_is_out", decisions={2: "import"}), [], [], AS_OF)
    assert not result.can_commit, "Import must remain blocked until the ambiguous date is actually resolved"
    assert not result.to_import


def test_bad_rows_must_be_explicitly_skipped_before_partial_import():
    csv = "date,payee,amount\n2026-11-01,Cafe,-500\n2026-11-02,Shop,banana\n"
    assert not analyse(csv, Settings(sign="negative_is_out"), [], [], AS_OF).can_commit
    result = analyse(csv, Settings(sign="negative_is_out", decisions={3: "skip"}), [], [], AS_OF)
    assert result.can_commit
    assert result.counts["skipped"] == 1
    assert len(result.to_import) == 1


def test_error_row_numbers_preserve_blank_lines_in_original_file():
    csv = "date,payee,amount\n\n2026-11-01,Cafe,banana\n"
    result = analyse(csv, Settings(sign="negative_is_out"), [], [], AS_OF)
    assert result.rows[0].row_no == 3


def test_unclosed_csv_quote_is_rejected():
    with pytest.raises(ImportFatal):
        read_csv('date,payee,amount\n2026-11-01,"Cafe,-500\n')


@pytest.fixture
def service_db():
    """SQLite exercises service contracts only; it does not verify MySQL locks."""
    from app.database import Database
    from app.services import seed_database

    db = Database("sqlite:///:memory:", "explicit SQLite service test")
    seed_database(db, AS_OF)
    yield db
    db.engine.dispose()


def owner(db):
    from app.services import make_actor

    with db.read() as conn:
        return make_actor(conn, "ayesha", 1)


def ready_draft(db, actor):
    from app import services as service

    draft_id = service.create_draft(db, actor, "test.csv",
                                  b"date,payee,amount\n2026-11-10,Unique acceptance merchant,-123.45\n")
    service.save_draft_settings(db, actor, draft_id, Settings(sign="negative_is_out"))
    return draft_id


def test_import_failure_rolls_back_records_batch_and_journal(service_db, monkeypatch):
    from sqlalchemy import func, select
    from app import services as service
    from app.database import tables

    actor = owner(service_db)
    draft_id = ready_draft(service_db, actor)
    checked_tables = (tables.transactions, tables.import_batches, tables.import_journal)
    with service_db.read() as conn:
        before = [conn.scalar(select(func.count()).select_from(t)) for t in checked_tables]

    def fail_audit(*args, **kwargs):
        raise RuntimeError("simulated failure after import writes")

    monkeypatch.setattr(service.imports, "audit", fail_audit)  # where commit_draft looks it up
    with pytest.raises(RuntimeError, match="simulated failure"):
        service.commit_draft(service_db, actor, draft_id, AS_OF)
    with service_db.read() as conn:
        assert [conn.scalar(select(func.count()).select_from(t)) for t in checked_tables] == before
        assert service.get_draft(conn, actor, draft_id)["id"] == draft_id


def test_import_undo_preserves_unrelated_later_work(service_db):
    from app import services as service

    actor = owner(service_db)
    batch_id = service.commit_draft(service_db, actor, ready_draft(service_db, actor), AS_OF)
    later_id = service.add_transaction(service_db, actor, {
        "posted_on": AS_OF.isoformat(), "amount": "250", "direction": "out",
        "payee": "Unrelated later purchase", "category_id": "shopping",
    }, AS_OF)
    result = service.undo_import(service_db, actor, batch_id, "full")
    assert result["removed"] == 1
    assert result["status"] == "undone"
    with service_db.read() as conn:
        assert service.get_txn_row(conn, actor, later_id)["amount"] == -25000
        assert service.list_transactions(conn, actor, batch=batch_id).total == 0


def test_edited_import_is_preserved_by_explicit_partial_undo(service_db):
    from app import services as service

    actor = owner(service_db)
    batch_id = service.commit_draft(service_db, actor, ready_draft(service_db, actor), AS_OF)
    with service_db.read() as conn:
        imported = service.list_transactions(conn, actor, batch=batch_id).rows[0]
    service.update_transaction(service_db, actor, imported["id"], {
        "posted_on": "2026-11-10", "amount": "125", "direction": "out",
        "payee": "Corrected purchase", "category_id": "shopping",
    }, imported["version"], AS_OF)
    with service_db.read() as conn:
        assert service.undo_preview(conn, actor, batch_id).needs_choice
    result = service.undo_import(service_db, actor, batch_id, "keep_edited")
    assert result["status"] == "partial"
    assert result["kept"] == 1
    with service_db.read() as conn:
        assert service.get_txn_row(conn, actor, imported["id"])["amount"] == -12500


def test_coach_cannot_edit_or_import(service_db):
    from app import services as service

    with service_db.read() as conn:
        coach = service.make_actor(conn, "coach_sara", 2)
    assert coach is not None
    with pytest.raises(service.Forbidden):
        service.add_transaction(service_db, coach, {}, AS_OF)
    with pytest.raises(service.Forbidden):
        service.create_draft(service_db, coach, "test.csv", b"date,payee,amount\n2026-11-01,Cafe,-500\n")


def test_workspace_cannot_read_another_workspaces_record(service_db):
    from app import services as service

    actor = owner(service_db)
    with service_db.read() as conn:
        household = service.make_actor(conn, "bilal", 2)
        other_id = service.list_transactions(conn, household).rows[0]["id"]
        with pytest.raises(service.NotFound):
            service.get_txn_row(conn, actor, other_id)


def test_edit_to_transfer_updates_ledger_kind(service_db):
    from app import services as service

    actor = owner(service_db)
    form = {"posted_on": AS_OF.isoformat(), "amount": "500", "direction": "out",
            "payee": "Savings allocation", "category_id": "shopping"}
    txn_id = service.add_transaction(service_db, actor, form, AS_OF)
    service.update_transaction(service_db, actor, txn_id,
                               {**form, "category_id": "savings_transfer"}, 1, AS_OF)
    with service_db.read() as conn:
        assert service.get_txn_row(conn, actor, txn_id)["kind"] == "transfer"


def test_payee_validation_respects_mysql_column_limit():
    from app.engine.validation import validate_transaction

    result = validate_transaction({"posted_on": AS_OF.isoformat(), "amount": "500",
                                   "direction": "out", "payee": "A" * 100,
                                   "category_id": "shopping"}, AS_OF)
    assert not result.ok or len(result.values["payee"]) <= 80


def test_default_auto_category_allows_adding_a_transaction(service_db):
    from app import services as service
    from app.engine.categories import CATEGORIES

    actor = owner(service_db)
    txn_id = service.add_transaction(service_db, actor, {
        "posted_on": AS_OF.isoformat(), "amount": "500", "direction": "out",
        "payee": "Cafe", "category_id": "auto",
    }, AS_OF)
    with service_db.read() as conn:
        row = service.get_txn_row(conn, actor, txn_id)
        assert row["category_id"] in CATEGORIES
        assert row["amount"] == -50000


def test_summary_only_coach_cannot_read_transaction_details_through_activity(service_db):
    from app import services as service

    with service_db.read() as conn:
        manager = service.make_actor(conn, "bilal", 2)
    service.set_sharing(service_db, manager, "summary")
    service.add_transaction(service_db, manager, {
        "posted_on": AS_OF.isoformat(), "amount": "123.45", "direction": "out",
        "payee": "PrivateTransactionMarker", "category_id": "shopping",
    }, AS_OF)
    with service_db.read() as conn:
        coach = service.make_actor(conn, "coach_sara", 2)
        assert coach.scope == "summary"
        try:
            activity = service.audit_entries(conn, coach)
        except service.Forbidden:
            return
    assert "PrivateTransactionMarker" not in str(activity)
