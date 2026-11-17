"""The same critical checks against a real MySQL test schema.

Runs only when TEST_DATABASE_URL is set (for example in backend/.env):

    TEST_DATABASE_URL=mysql+pymysql://hci_app:YOUR_PASSWORD@localhost:3306/hci_finance_test?charset=utf8mb4

WARNING: the test schema is dropped and recreated on every run. Never point
this at the real hci_finance schema.
"""
from pathlib import Path
import os

from sqlalchemy import func, select, text
import pytest

from app import services as S
from app.database import Database, load_dotenv
from app.database import tables as T
from app.engine import importer as I

from conftest import AS_OF


# ================================================================================================
# The same critical checks against a real MySQL test schema.
# ================================================================================================


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


URL = os.environ.get("TEST_DATABASE_URL", "")


pytestmark = pytest.mark.skipif(not URL.startswith("mysql"), reason="TEST_DATABASE_URL (MySQL) not configured")


@pytest.fixture
def mdb():
    assert "test" in URL.rsplit("/", 1)[-1], "TEST_DATABASE_URL must point to a *test* schema"
    d = Database(URL, "MySQL test")
    d.drop_all()
    S.seed_database(d, AS_OF)
    yield d
    d.drop_all()


def _actor(d, pid):
    with d.read() as c:
        return S.make_actor(c, pid, None)


def _count(d):
    with d.read() as c:
        return c.execute(select(func.count()).select_from(T.transactions)).scalar_one()


def _import(d, a):
    draft = S.create_draft(d, a, "m.csv", b"Date,Description,Amount\n2026-11-10,Mega Mart,2500\n2026-11-11,QuickBite,900\n")
    with d.read() as c:
        s = I.Settings.from_json(S.get_draft(c, a, draft)["settings_json"])
    s.sign = "positive_is_out"
    S.save_draft_settings(d, a, draft, s)
    return S.commit_draft(d, a, draft, AS_OF)


def test_mysql_engine_is_innodb_utf8mb4(mdb):
    with mdb.read() as c:
        rows = c.exec_driver_sql(
            "SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'").all()  # views have no engine
    assert rows and all(r[0] == "InnoDB" and r[1].startswith("utf8mb4") for r in rows)


def test_mysql_import_and_undo_with_edit(mdb):
    a = _actor(mdb, "bilal")
    before = _count(mdb)
    batch = _import(mdb, a)
    assert _count(mdb) == before + 2
    with mdb.read() as c:
        t = c.execute(select(T.transactions).where(T.transactions.c.import_batch_id == batch)).first()
    S.recategorise(mdb, a, t.id, "dining", t.version, False)
    res = S.undo_import(mdb, a, batch, "keep_edited")
    assert res["status"] == "partial" and _count(mdb) == before + 1


def test_mysql_version_conflict(mdb):
    bilal, sana = _actor(mdb, "bilal"), _actor(mdb, "sana")
    with mdb.read() as c:
        b = c.execute(select(T.budgets).where(T.budgets.c.workspace_id == 2, T.budgets.c.category_id == "dining")).first()
    S.set_budget(mdb, sana, "dining", "16000", b.version)
    with pytest.raises(S.EditConflict):
        S.set_budget(mdb, bilal, "dining", "14000", b.version)


def test_mysql_permissions(mdb):
    coach = _actor(mdb, "coach_sara")
    with mdb.read() as c, pytest.raises(S.Forbidden):
        S.list_transactions(c, coach)


def test_mysql_unicode_roundtrip(mdb):
    a = _actor(mdb, "ayesha")
    S.add_transaction(mdb, a, {"posted_on": "2026-11-14", "amount": "350", "payee": "☕ چائے کارنر 😀",
                               "direction": "out"}, AS_OF)
    with mdb.read() as c:
        assert c.execute(select(T.transactions.c.payee).where(T.transactions.c.payee.like("%چائے%"))).scalar() == "☕ چائے کارنر 😀"


def test_soft_delete_keeps_the_email_on_mysql(mdb):
    """MySQL applies UPDATE assignments left to right; the email must survive being moved aside."""
    pw = "correct horse 42"
    pid = S.create_account(mdb, {"name": "Mysql User", "email": "my@example.com", "password": pw,
                                 "confirm_password": pw}, AS_OF)
    S.delete_account(mdb, _actor(mdb, pid), pw, "DELETE")
    with mdb.read() as c:
        row = c.execute(select(T.profiles).where(T.profiles.c.id == pid)).first()
    assert row.email is None and row.deleted_email == "my@example.com" and row.deleted_at is not None


def _soft_deleted_account(d, email, pw="correct horse 42"):
    pid = S.create_account(d, {"name": "Gone User", "email": email, "password": pw, "confirm_password": pw}, AS_OF)
    S.add_transaction(d, _actor(d, pid), {"posted_on": AS_OF.isoformat(), "amount": "500", "payee": "Mega Mart",
                                          "direction": "out", "category_id": "auto"}, AS_OF)
    S.delete_account(d, _actor(d, pid), pw, "DELETE")
    return pid


def _call(d, sql):
    with d.engine.begin() as c:
        return c.exec_driver_sql(sql).fetchall()


ADMIN_OBJECTS = ("VIEW accounts_overview", "VIEW deleted_account_transactions",
                 "PROCEDURE restore_account", "PROCEDURE purge_account", "PROCEDURE purge_account_rows",
                 "PROCEDURE purge_deleted_accounts")


@pytest.fixture
def admin_db(mdb):
    from app.database.migrations import install_admin_sql
    assert install_admin_sql(mdb) > 0
    yield mdb
    with mdb.engine.begin() as c:
        for obj in ADMIN_OBJECTS:
            c.exec_driver_sql(f"DROP {obj.split()[0]} IF EXISTS {obj.split()[1]}")


def test_deleted_accounts_table_lists_soft_deleted_accounts(admin_db):
    pid = _soft_deleted_account(admin_db, "view@example.com")
    with admin_db.read() as c:
        kind = c.execute(text("SELECT TABLE_TYPE FROM information_schema.TABLES "
                              "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'deleted_accounts'")).scalar()
        row = c.execute(text("SELECT * FROM deleted_accounts WHERE profile_id = :p"), {"p": pid}).mappings().first()
        status = c.execute(text("SELECT status FROM accounts_overview WHERE profile_id = :p"), {"p": pid}).scalar()
        kept = c.execute(text("SELECT COUNT(*) FROM deleted_account_transactions WHERE profile_id = :p"),
                         {"p": pid}).scalar()
    assert kind == "BASE TABLE" and row["email"] == "view@example.com" and row["deleted_at"] is not None
    assert status == "deleted" and kept == 1


def _listed(d, pid):
    with d.read() as c:
        return c.execute(text("SELECT COUNT(*) FROM deleted_accounts WHERE profile_id = :p"), {"p": pid}).scalar()


def test_old_deleted_accounts_view_becomes_a_table(mdb):
    """A database from the previous version had a VIEW called deleted_accounts; start-up replaces it."""
    from app.database.migrations import migrate
    pid = _soft_deleted_account(mdb, "legacy@example.com")
    with mdb.engine.begin() as c:
        c.exec_driver_sql("DROP TABLE deleted_accounts")
        c.exec_driver_sql("CREATE VIEW deleted_accounts AS SELECT id AS profile_id FROM profiles WHERE is_deleted = 1")
    changes = migrate(mdb)
    assert "added table deleted_accounts" in changes and _listed(mdb, pid) == 1
    with mdb.engine.begin() as c:
        for obj in ("VIEW accounts_overview", "VIEW deleted_account_transactions", "PROCEDURE restore_account",
                    "PROCEDURE purge_account", "PROCEDURE purge_account_rows", "PROCEDURE purge_deleted_accounts"):
            c.exec_driver_sql(f"DROP {obj.split()[0]} IF EXISTS {obj.split()[1]}")


def test_restore_account_procedure(admin_db):
    pid = _soft_deleted_account(admin_db, "back@example.com")
    assert _listed(admin_db, pid) == 1
    assert "Restored" in _call(admin_db, f"CALL restore_account('{pid}')")[0][0]
    assert _listed(admin_db, pid) == 0  # no longer listed as deleted
    actor = _actor(admin_db, pid)
    assert actor is not None and actor.email == "back@example.com"
    with pytest.raises(Exception, match="No soft-deleted account"):
        _call(admin_db, f"CALL restore_account('{pid}')")  # already active


def test_purge_refuses_active_and_demo_accounts(admin_db):
    pw = "correct horse 42"
    active = S.create_account(admin_db, {"name": "Still Here", "email": "here@example.com", "password": pw,
                                         "confirm_password": pw}, AS_OF)
    for pid in (active, "bilal"):
        with pytest.raises(Exception, match="Only soft-deleted accounts"):
            _call(admin_db, f"CALL purge_account('{pid}')")
    assert _actor(admin_db, active) is not None and _actor(admin_db, "bilal") is not None


def test_purge_account_removes_everything_and_only_that(admin_db):
    pid = _soft_deleted_account(admin_db, "purge@example.com")
    with admin_db.read() as c:
        ws = c.execute(text("SELECT workspace_id FROM memberships WHERE profile_id = :p"), {"p": pid}).scalar()
        demo_before = c.execute(text("SELECT COUNT(*) FROM transactions WHERE workspace_id <> :w"), {"w": ws}).scalar()
    assert "Permanently deleted" in _call(admin_db, f"CALL purge_account('{pid}')")[0][0]
    with admin_db.read() as c:
        left = [c.execute(text(q), {"p": pid, "w": ws}).scalar() for q in (
            "SELECT COUNT(*) FROM profiles WHERE id = :p", "SELECT COUNT(*) FROM workspaces WHERE id = :w",
            "SELECT COUNT(*) FROM transactions WHERE workspace_id = :w", "SELECT COUNT(*) FROM rules WHERE workspace_id = :w",
            "SELECT COUNT(*) FROM memberships WHERE profile_id = :p",
            "SELECT COUNT(*) FROM deleted_accounts WHERE profile_id = :p",
            "SELECT COUNT(*) FROM audit_log WHERE profile_id = :p OR workspace_id = :w")]
        demo_after = c.execute(text("SELECT COUNT(*) FROM transactions")).scalar()
    assert left == [0, 0, 0, 0, 0, 0, 0] and demo_after == demo_before


def test_purge_deleted_accounts_respects_the_age_limit(admin_db):
    old = _soft_deleted_account(admin_db, "old@example.com")
    new = _soft_deleted_account(admin_db, "new@example.com")
    with admin_db.engine.begin() as c:
        c.execute(text("UPDATE profiles SET deleted_at = NOW() - INTERVAL 40 DAY WHERE id = :p"), {"p": old})
        c.execute(text("UPDATE deleted_accounts SET deleted_at = NOW() - INTERVAL 40 DAY WHERE profile_id = :p"),
                  {"p": old})
    assert "1 soft-deleted" in _call(admin_db, "CALL purge_deleted_accounts(30)")[0][0]
    with admin_db.read() as c:
        ids = {r[0] for r in c.execute(text("SELECT id FROM profiles WHERE NOT is_demo"))}
    assert old not in ids and new in ids
    with pytest.raises(Exception, match="0 or more"):
        _call(admin_db, "CALL purge_deleted_accounts(-1)")
