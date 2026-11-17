"""The application end to end on a throw-away SQLite database: services, pages, roles,
accounts, soft delete, and the independent acceptance cases from the handbook.
"""
from copy import deepcopy
from datetime import date
import re

from sqlalchemy import func, select
from sqlalchemy import select
import pytest

from app import create_app
from app import services as S
from app.database import StorageError
from app.database import tables as T
from app.engine import importer as I
from app.engine.budgets import spend_in
from app.engine.dates import DateParseError, parse_flexible
from app.engine.forecast import AssumptionSet, build_forecast, variable_assumptions
from app.engine.importer import ImportFatal, Settings, analyse, read_csv
from app.engine.models import Adjustment, RecurringBill, Txn
from app.engine.money import AmountError, parse_amount
from app.engine.recurring import occurrences
from app.engine.scenarios import run

from conftest import AS_OF, actor, login


# ================================================================================================
# Service layer against a real database (SQLite here; see test_mysql.py
# ================================================================================================


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


# ================================================================================================
# End-to-end through the Flask test client: journeys, roles, CSRF, states.
# ================================================================================================


PAGES = ["/", "/budgets", "/bills", "/goals", "/forecast", "/forecast?horizon=90", "/whatif",
         "/export", "/history", "/settings", "/help"]


@pytest.mark.parametrize("profile", ["ayesha", "bilal", "sana", "coach_sara"])
@pytest.mark.parametrize("lens", ["summary", "standard", "detailed"])
def test_every_page_renders_for_every_role_and_lens(client, profile, lens):
    login(client, profile)
    client.post("/prefs", data={"lens": lens, "next": "/"})
    for url in PAGES:
        r = client.get(url)
        assert r.status_code == 200, url
        assert b'lang="en"' in r.data and b"skip-link" in r.data


def test_switch_profile_page_lists_profiles_when_already_signed_in(client):
    login(client, "bilal")
    page = client.get("/profiles").get_data(as_text=True)
    assert 'name="profile_id" value="ayesha"' in page and 'value="coach_sara"' in page


def test_unauthenticated_requests_go_to_profiles(client):
    assert client.get("/").status_code == 302


def test_coach_is_read_only(client):
    login(client, "coach_sara")
    assert client.get("/transactions").status_code == 403
    assert client.get("/import").status_code == 403
    r = client.post("/budgets", data={"category_id": "dining", "monthly_limit": "1"})
    assert r.status_code == 403


def test_csrf_blocks_forged_posts(tmp_path):
    app = create_app({"DATABASE_URL": f"sqlite:///{(tmp_path / 'c.db').as_posix()}", "AS_OF": AS_OF,
                      "SECRET_KEY": "x"})
    c = app.test_client()
    c.get("/profiles")
    assert c.post("/profiles", data={"profile_id": "bilal"}).status_code == 400
    page = c.get("/profiles").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    assert c.post("/profiles", data={"profile_id": "bilal", "_csrf": token}).status_code == 302


def test_core_journey_correct_category_then_explain(client):
    login(client, "ayesha")
    r = client.get("/transactions?q=book+bazaar+cafe")
    tid = int(re.search(rb'id="t-(\d+)"', r.data).group(1))
    ver = re.search(rb'name="version" value="(\d+)"', r.data).group(1).decode()
    r = client.post(f"/transactions/{tid}/category", data={"category_id": "dining", "version": ver})
    assert b"Change the category" in r.data  # asks about similar transactions first
    r = client.post(f"/transactions/{tid}/category",
                    data={"category_id": "dining", "version": ver, "apply": "similar"}, follow_redirects=True)
    assert b"future ones will use this category" in r.data
    dash = client.get("/").data
    assert b"How is this worked out?" in dash and b"Formula:" in dash


def test_import_journey_with_messy_sample_then_undo(client):
    login(client, "bilal")
    r = client.post("/import", data={"sample": "1"})
    draft = int(re.search(r"/import/(\d+)/map", r.headers["Location"]).group(1))
    r = client.post(f"/import/{draft}/map", data={"col_date": "0", "col_description": "1", "col_amount": "2",
                                                  "col_category": "3", "sign": "positive_is_out", "date_order": "DMY"})
    assert r.headers["Location"].endswith(f"/import/{draft}/review")
    page = client.get(f"/import/{draft}/review?filter=all").data
    assert b"Not ready to import yet" in page
    client.post(f"/import/{draft}/bulk", data={"action": "skip_errors"})
    client.post(f"/import/{draft}/bulk", data={"action": "skip_exact_duplicates"})
    client.post(f"/import/{draft}/bulk", data={"action": "keep_in_file_duplicates"})
    page = client.get(f"/import/{draft}/review?filter=needs_decision").get_data(as_text=True)
    for row in set(re.findall(r'id="row-(\d+)"', page)):
        client.post(f"/import/{draft}/row/{row}", data={"action": "skip"})
    r = client.post(f"/import/{draft}/commit")
    assert r.status_code == 302 and "/import/batch/" in r.headers["Location"]
    batch_page = client.get(r.headers["Location"]).data
    assert b"transactions imported" in batch_page and b"Skipped rows" in batch_page
    bid = int(r.headers["Location"].split("/")[-1].split("?")[0])
    assert b"Undo the import" in client.get(f"/import/batch/{bid}/undo").data
    r = client.post(f"/import/batch/{bid}/undo", data={"mode": "full"}, follow_redirects=True)
    assert b"Import undone" in r.data


def test_bad_upload_shows_recovery_message(client):
    login(client, "bilal")
    from io import BytesIO
    r = client.post("/import", data={"file": (BytesIO("x".encode("utf-16")), "bank.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 422 and b"CSV UTF-8" in r.data


def test_invalid_manual_entry_keeps_input_and_explains(client):
    login(client, "ayesha")
    r = client.post("/transactions/new", data={"posted_on": "2026-11-14", "amount": "abc", "payee": "Chai",
                                               "direction": "out"})
    assert r.status_code == 400 and b"Please fix" in r.data and b'value="Chai"' in r.data


def test_storage_failure_page(client):
    login(client, "bilal")
    client.post("/settings/storage", data={"simulate": "1"})
    r = client.post("/budgets", data={"category_id": "dining", "monthly_limit": "15000"})
    assert r.status_code == 503 and b"was not saved" in r.data
    client.post("/settings/storage", data={"simulate": "0"})


def test_empty_state(client):
    login(client, "ayesha")
    client.post("/settings/clear", data={"confirm": "CLEAR"})
    dash = client.get("/").data
    assert b"Get started" in dash and b"Add your transactions" in dash and b"0 of 4 done" in dash
    assert b"Nothing to forecast yet" in client.get("/forecast").data


def test_what_if_journey(client):
    login(client, "bilal")
    client.post("/whatif/load/1")
    page = client.get("/whatif").data
    assert b"Difference on" in page and b"What-if (dotted)" in page
    client.post("/whatif/save", data={"name": "Copy"})
    assert b"Copy" in client.get("/whatif").data


def test_export_csv_is_formula_safe(client):
    login(client, "bilal")
    client.post("/transactions/new", data={"posted_on": "2026-11-14", "amount": "10", "payee": "=cmd|' /C calc'!A0",
                                           "direction": "out"})
    r = client.post("/export", data={"kind": "transactions"})
    assert r.mimetype == "text/csv" and b"'=cmd" in r.data
    assert r.data.startswith("﻿".encode())  # UTF-8 byte-order mark for Excel


# ================================================================================================
# Real accounts: sign up, log in / out, privacy between accounts and from the demo.
# ================================================================================================


PASSWORD = "correct horse 42"


def signup(client, name="Zara Ahmed", email="zara@example.com", password=PASSWORD, **extra):
    data = {"name": name, "email": email, "password": password, "confirm_password": password,
            "opening_balance": "25,000", "opening_date": AS_OF.isoformat(), **extra}
    return client.post("/signup", data=data)


def add_txn(client, payee, amount="500"):
    return client.post("/transactions/new", data={"posted_on": AS_OF.isoformat(), "amount": amount,
                                                  "payee": payee, "direction": "out"})


def test_signup_creates_a_private_empty_budget(client, db):
    r = signup(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    page = client.get("/").get_data(as_text=True)
    assert "Zara’s budget" in page and "Get started" in page and "0 of 4 done" in page
    assert "Rs 25,000" in page  # starting balance is today's balance
    assert "Log out" in page and "Switch profile" not in page
    with db.read() as conn:
        row = conn.execute(select(T.profiles).where(T.profiles.c.email == "zara@example.com")).first()
    assert row.password_hash and PASSWORD not in row.password_hash and not row.is_demo


def test_new_budget_categorises_automatically(client):
    signup(client)
    add_txn(client, "Mega Mart")
    assert "Groceries" in client.get("/transactions").get_data(as_text=True)


def test_signup_validation_keeps_input_but_never_the_password(client):
    r = signup(client, name="", email="not-an-email", password="short")
    page = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "Enter your name." in page and "valid email" in page and "at least 8 characters" in page
    assert 'value="not-an-email"' in page and "short" not in page.split("<form", 1)[1].split("</form>")[0].replace(
        "8 characters", "")
    r = client.post("/signup", data={"name": "Zara", "email": "z@example.com", "password": PASSWORD,
                                     "confirm_password": "something else"})
    assert r.status_code == 400 and "don’t match" in r.get_data(as_text=True)


def test_duplicate_email_is_rejected(client, app):
    signup(client)
    other = app.test_client()
    r = signup(other, name="Someone Else", email="ZARA@example.com ")
    assert r.status_code == 400 and "already exists" in r.get_data(as_text=True)


def test_login_and_logout(client, app):
    signup(client)
    client.post("/logout")
    assert client.get("/").status_code == 302  # back to the log-in page
    r = client.post("/login", data={"email": "zara@example.com", "password": "wrong password"})
    assert r.status_code == 401 and "don’t match" in r.get_data(as_text=True)
    r = client.post("/login", data={"email": " Zara@Example.com", "password": PASSWORD})
    assert r.status_code == 302
    assert "Zara’s budget" in client.get("/").get_data(as_text=True)


def test_accounts_cannot_see_each_other(client, app):
    signup(client)
    add_txn(client, "Private Payee One")
    other = app.test_client()
    signup(other, name="Omar", email="omar@example.com")
    page = other.get("/transactions").get_data(as_text=True)
    assert "Private Payee One" not in page and "0 in this budget" in page


def test_real_accounts_are_not_reachable_through_the_demo(client, app, db):
    signup(client)
    with db.read() as conn:
        zara_id = conn.execute(select(T.profiles.c.id).where(T.profiles.c.email == "zara@example.com")).scalar()
    visitor = app.test_client()
    assert "Zara" not in visitor.get("/profiles").get_data(as_text=True)
    r = visitor.post("/profiles", data={"profile_id": zara_id})
    assert r.status_code == 302 and visitor.get("/").status_code == 302  # refused, still signed out
    login(visitor, "coach_sara")
    assert "Zara" not in visitor.get("/coach").get_data(as_text=True)


def test_real_accounts_cannot_share_with_the_demo_coach(client):
    signup(client)
    assert "Sharing with a financial coach" not in client.get("/settings").get_data(as_text=True)
    assert client.post("/settings/sharing", data={"scope": "full"}).status_code == 403


def test_demo_reset_keeps_real_accounts(client, app):
    signup(client)
    add_txn(client, "Kept After Reset")
    demo = app.test_client()
    login(demo, "bilal")
    assert demo.post("/settings/reset", data={"confirm": "RESET"}).status_code == 302
    assert "Kept After Reset" in client.get("/transactions").get_data(as_text=True)
    assert client.post("/settings/reset", data={"confirm": "RESET"}).status_code == 403  # not for real budgets


def test_storage_failure_test_only_affects_one_browser(client, app):
    signup(client)
    demo = app.test_client()
    login(demo, "bilal")
    demo.post("/settings/storage", data={"simulate": "1"})
    assert add_txn(demo, "Fails").status_code == 503
    assert add_txn(client, "Still Saves").status_code == 302


def test_budget_details_can_be_corrected(client):
    signup(client)
    r = client.post("/settings/budget", data={"name": "Home money", "opening_balance": "40000",
                                              "opening_date": AS_OF.isoformat()})
    assert r.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Home money" in page and "Rs 40,000" in page
    r = client.post("/settings/budget", data={"name": "", "opening_balance": "abc", "opening_date": "2099-01-01"})
    assert r.status_code == 400


def delete(client, password=PASSWORD, confirm="DELETE"):
    return client.post("/settings/account/delete", data={"password": password, "confirm": confirm})


def test_delete_account_needs_password_and_confirmation(client):
    signup(client)
    r = delete(client, password="wrong password", confirm="delete please")
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "password is not right" in page and "Type DELETE" in page
    assert client.get("/").status_code == 200  # still signed in, nothing changed


def test_delete_account_is_soft(client, app, db):
    signup(client)
    add_txn(client, "Kept For Records")
    r = delete(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert client.get("/").status_code == 302  # logged out
    with db.read() as conn:
        p = conn.execute(select(T.profiles).where(T.profiles.c.deleted_email == "zara@example.com")).first()
        ws = conn.execute(select(T.workspaces).join(T.memberships, T.memberships.c.workspace_id == T.workspaces.c.id)
                          .where(T.memberships.c.profile_id == p.id)).first()
        payees = [r.payee for r in conn.execute(select(T.transactions.c.payee).where(T.transactions.c.workspace_id == ws.id))]
    assert p.is_deleted and p.deleted_at is not None and p.email is None and p.password_hash  # kept, login removed
    assert ws.is_deleted and ws.deleted_at is not None and "Kept For Records" in payees  # data kept
    with db.read() as conn:
        listed = conn.execute(select(T.deleted_accounts).where(T.deleted_accounts.c.profile_id == p.id)).first()
    assert listed.email == "zara@example.com" and listed.workspace_id == ws.id and listed.name == "Zara Ahmed"
    r = client.post("/login", data={"email": "zara@example.com", "password": PASSWORD})
    assert r.status_code == 401  # can't log in any more


def test_deleted_email_can_sign_up_again_with_an_empty_budget(client, app):
    signup(client)
    add_txn(client, "Old Life")
    delete(client)
    fresh = app.test_client()
    assert signup(fresh).status_code == 302
    assert "Old Life" not in fresh.get("/transactions").get_data(as_text=True)


def test_deleting_signs_out_other_browsers(client, app):
    signup(client)
    other = app.test_client()
    other.post("/login", data={"email": "zara@example.com", "password": PASSWORD})
    assert other.get("/").status_code == 200
    delete(client)
    r = other.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


def test_demo_profiles_cannot_be_deleted(client):
    login(client, "bilal")
    assert "Delete account" not in client.get("/settings").get_data(as_text=True)
    assert delete(client, password="", confirm="DELETE").status_code == 403


def _old_database(tmp_path):
    """A database from before soft delete existed: no is_deleted / deleted_* columns."""
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE profiles (id VARCHAR(32) PRIMARY KEY, display_name VARCHAR(80) NOT NULL, "
                       "role VARCHAR(16) NOT NULL, blurb VARCHAR(200) NOT NULL, email VARCHAR(254), "
                       "password_hash VARCHAR(255), is_demo BOOLEAN NOT NULL, created_at DATETIME)"))
        c.execute(text("INSERT INTO profiles VALUES ('u_1', 'Old User', 'owner', '', 'old@example.com', 'h', 0, NULL)"))
    eng.dispose()
    return url


def test_upgrade_adds_new_columns_without_losing_data(tmp_path):
    from sqlalchemy import inspect, text

    from app.database import Database
    from app.database.migrations import upgrade_schema
    db = Database(_old_database(tmp_path))
    added = upgrade_schema(db)
    assert {"table deleted_accounts", "profiles.is_deleted", "profiles.deleted_at",
            "profiles.deleted_email"} <= set(added)
    assert {"is_deleted", "deleted_at", "deleted_email"} <= {c["name"] for c in inspect(db.engine).get_columns("profiles")}
    with db.read() as c:
        row = c.execute(text("SELECT display_name, is_deleted FROM profiles")).first()
    assert row.display_name == "Old User" and row.is_deleted == 0  # existing accounts start as active
    assert upgrade_schema(db) == []  # running again changes nothing


def test_upgrade_backfills_is_deleted_for_older_soft_deletes(tmp_path):
    """Accounts soft-deleted before the flag existed (deleted_at only) are marked is_deleted = 1."""
    from sqlalchemy import create_engine, text

    from app.database import Database
    from app.database.migrations import upgrade_schema
    url = _old_database(tmp_path)
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE profiles ADD COLUMN deleted_at DATETIME"))
        c.execute(text("ALTER TABLE profiles ADD COLUMN deleted_email VARCHAR(254)"))
        c.execute(text("INSERT INTO profiles VALUES ('u_2', 'Gone', 'owner', '', NULL, 'h', 0, NULL, "
                       "'2026-11-01 10:00:00', 'gone@example.com')"))
    eng.dispose()
    db = Database(url)
    upgrade_schema(db)
    with db.read() as c:
        flags = dict(c.execute(text("SELECT id, is_deleted FROM profiles")).all())
        listed = c.execute(text("SELECT profile_id, email FROM deleted_accounts")).all()
    assert flags == {"u_1": 0, "u_2": 1}
    assert [tuple(r) for r in listed] == [("u_2", "gone@example.com")]  # recorded in the new table


def test_admin_sql_script_splits_into_whole_statements():
    """The Workbench script (with DELIMITER) is also what the app installs, so it must parse cleanly."""
    from app.database.migrations import ADMIN_SQL, split_sql_script
    statements = split_sql_script(ADMIN_SQL.read_text(encoding="utf-8"))
    creates = [s.split("(")[0].split()[-1] for s in statements if s.startswith("CREATE PROCEDURE")]
    views = [s.split()[4] for s in statements if s.startswith("CREATE OR REPLACE VIEW")]
    assert views == ["accounts_overview", "deleted_account_transactions"]  # deleted_accounts is a real table
    assert creates == ["restore_account", "purge_account_rows", "purge_account", "purge_deleted_accounts"]
    assert all(s.rstrip().endswith("END") for s in statements if s.startswith("CREATE PROCEDURE"))
    assert not any(s.startswith(("DELIMITER", "USE")) for s in statements)


# ================================================================================================
# Independent acceptance cases from the handbook and architecture agreements.
# ================================================================================================


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

    monkeypatch.setattr(service.csv_files, "audit", fail_audit)  # where commit_draft looks it up
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
