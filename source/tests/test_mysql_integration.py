"""The same critical checks against a real MySQL test schema.

Runs only when TEST_DATABASE_URL is set (for example in source/.env):

    TEST_DATABASE_URL=mysql+pymysql://hci_app:YOUR_PASSWORD@localhost:3306/hci_finance_test?charset=utf8mb4

WARNING: the test schema is dropped and recreated on every run. Never point
this at the real hci_finance schema.
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from finance import db as T
from finance import services as S
from finance.db import Database, _load_dotenv
from finance.engine import importer as I

from .conftest import AS_OF

_load_dotenv(Path(__file__).resolve().parent.parent / ".env")
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
            "SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()").all()
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
