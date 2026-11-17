"""Service layer against a real database (SQLite here; see test_mysql_integration.py
for the same checks on MySQL).  Covers persistence, atomic import + journal,
whole-import undo with later edits, version conflicts, permissions and the
storage-failure path."""
import pytest
from sqlalchemy import func, select

from app.database import tables as T
from app import services as S
from app.database import StorageError
from app.engine import importer as I

from .conftest import AS_OF, actor

CSV = ("Date,Description,Amount\n"
       "2026-11-10,Mega Mart - POS,2500\n"
       "2026-11-11,QuickBite Delivery,900\n"
       "2026-11-12,REFUND - Mega Mart,-500\n")


def txn_count(db, ws):
    with db.read() as c:
        return c.execute(select(func.count()).select_from(T.transactions)
                         .where(T.transactions.c.workspace_id == ws)).scalar_one()


def import_csv(db, a, text=CSV):
    draft = S.create_draft(db, a, "test.csv", text.encode())
    with db.read() as c:
        d = S.get_draft(c, a, draft)
    s = I.Settings.from_json(d["settings_json"])
    s.sign = "positive_is_out"
    S.save_draft_settings(db, a, draft, s)
    return S.commit_draft(db, a, draft, AS_OF)


def test_seed_has_enough_synthetic_records(db):
    assert txn_count(db, 1) + txn_count(db, 2) >= 600


def test_data_survives_a_new_app_instance(app, tmp_path):
    from app import create_app
    url = app.extensions["db"].url
    a = actor(app.extensions["db"], "bilal")
    S.set_budget(app.extensions["db"], a, "education", "9000", None)
    again = create_app({"DATABASE_URL": url, "AS_OF": AS_OF, "SECRET_KEY": "t", "CSRF_DISABLED": True})
    with again.extensions["db"].read() as c:
        row = c.execute(select(T.budgets).where(T.budgets.c.category_id == "education",
                                                T.budgets.c.workspace_id == 2)).first()
    assert row.monthly_limit == 900000


def test_import_commit_is_atomic_and_journaled(db):
    a = actor(db, "bilal")
    before = txn_count(db, 2)
    batch = import_csv(db, a)
    assert txn_count(db, 2) == before + 3
    with db.read() as c:
        j = c.execute(select(T.import_journal).where(T.import_journal.c.batch_id == batch)).all()
        refund = c.execute(select(T.transactions).where(T.transactions.c.import_batch_id == batch,
                                                        T.transactions.c.kind == "refund")).first()
    assert len(j) == 3 and {x.op for x in j} == {"insert"}
    assert refund.amount == 50000 and refund.refund_of is not None and refund.raw_row


def test_commit_is_refused_while_rows_are_unresolved(db):
    a = actor(db, "bilal")
    draft = S.create_draft(db, a, "bad.csv", b"Date,Description,Amount\n31/02/2026,Bad,100\n2026-11-01,Ok,50\n")
    with db.read() as c:
        d = S.get_draft(c, a, draft)
    s = I.Settings.from_json(d["settings_json"])
    s.sign = "positive_is_out"
    S.save_draft_settings(db, a, draft, s)
    before = txn_count(db, 2)
    with pytest.raises(S.ImportBlocked):
        S.commit_draft(db, a, draft, AS_OF)
    assert txn_count(db, 2) == before


def test_whole_import_undo_and_redo(db):
    a = actor(db, "bilal")
    before = txn_count(db, 2)
    batch = import_csv(db, a)
    res = S.undo_import(db, a, batch, "full")
    assert res == {"removed": 3, "kept": 0, "status": "undone", "already_gone": 0}
    assert txn_count(db, 2) == before
    assert S.redo_import(db, a, batch) == 3
    assert txn_count(db, 2) == before + 3


def test_undo_after_later_edit_is_explicit_partial_or_full(db):
    a = actor(db, "bilal")
    before = txn_count(db, 2)
    batch = import_csv(db, a)
    with db.read() as c:
        t = c.execute(select(T.transactions).where(T.transactions.c.import_batch_id == batch)).first()
    S.recategorise(db, a, t.id, "dining", t.version, apply_to_similar=False)
    with db.read() as c:
        p = S.undo_preview(c, a, batch)
    assert len(p.edited) == 1 and len(p.unchanged) == 2 and p.needs_choice
    res = S.undo_import(db, a, batch, "keep_edited")
    assert res["status"] == "partial" and res["kept"] == 1
    assert txn_count(db, 2) == before + 1
    with db.read() as c:
        assert S.get_batch(c, a, batch)["status"] == "partial"


def test_version_conflict_detected_for_two_editors(db):
    bilal, sana = actor(db, "bilal"), actor(db, "sana")
    with db.read() as c:
        b = c.execute(select(T.budgets).where(T.budgets.c.workspace_id == 2,
                                              T.budgets.c.category_id == "dining")).first()
    S.set_budget(db, sana, "dining", "16000", b.version)          # Sana saves first
    with pytest.raises(S.EditConflict) as e:
        S.set_budget(db, bilal, "dining", "14000", b.version)     # Bilal used the old version
    assert e.value.current["monthly_limit"] == 1600000


def test_recategorise_similar_creates_rule_and_spares_manual_choices(db):
    a = actor(db, "ayesha")
    with db.read() as c:
        cafe = c.execute(select(T.transactions).where(T.transactions.c.workspace_id == 1,
                                                      T.transactions.c.payee == "Book Bazaar Cafe")).all()
    first, second = cafe[0], cafe[1]
    S.recategorise(db, a, second.id, "shopping", second.version, False)   # manual choice
    n = S.recategorise(db, a, first.id, "dining", first.version, True)
    assert n == len(cafe) - 1  # every other one except the manual choice
    with db.read() as c:
        cats = {r.id: (r.category_id, r.category_source) for r in c.execute(
            select(T.transactions).where(T.transactions.c.payee == "Book Bazaar Cafe"))}
        rule = c.execute(select(T.rules).where(T.rules.c.workspace_id == 1, T.rules.c.source == "user")).first()
    assert cats[second.id] == ("shopping", "user")
    assert rule.pattern == "book bazaar cafe" and rule.category_id == "dining"


def test_coach_permissions_follow_consent(db):
    bilal = actor(db, "bilal")
    coach = actor(db, "coach_sara")
    assert coach.workspace_id == 2 and coach.scope == "summary"
    assert not coach.can("view_details") and not coach.can("edit")
    with db.read() as c, pytest.raises(S.Forbidden):
        S.list_transactions(c, coach)
    with pytest.raises(S.Forbidden):
        S.set_budget(db, coach, "dining", "1", None)
    S.set_sharing(db, bilal, "full")
    assert actor(db, "coach_sara").can("view_details")
    S.set_sharing(db, bilal, None)
    assert actor(db, "coach_sara") is None   # revoked: no workspace at all


def test_owner_cannot_reach_other_workspace(db):
    a = actor(db, "ayesha", workspace_id=2)   # tries to open the household
    assert a.workspace_id == 1                # falls back to her own


def test_storage_failure_saves_nothing(db):
    a = actor(db, "bilal")
    before = txn_count(db, 2)
    S.set_meta(db, "simulate_storage_failure", "1")
    with pytest.raises(StorageError):
        S.add_transaction(db, a, {"posted_on": "2026-11-14", "amount": "500", "payee": "Test", "direction": "out"}, AS_OF)
    with pytest.raises(StorageError):
        import_csv(db, a)
    S.set_meta(db, "simulate_storage_failure", "0")
    assert txn_count(db, 2) == before


def test_clear_workspace_shows_empty_state_without_touching_others(db):
    a = actor(db, "ayesha")
    other = txn_count(db, 2)
    S.clear_workspace(db, a)
    assert txn_count(db, 1) == 0 and txn_count(db, 2) == other
