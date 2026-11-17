"""Shared test setup: a fresh app on a throw-away SQLite file for every test."""
import sys

sys.dont_write_bytecode = True  # no __pycache__ folders in the project

from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app import services as S  # noqa: E402
from app.database import Database  # noqa: E402

AS_OF = date(2026, 11, 15)


@pytest.fixture
def app(tmp_path):
    return create_app({
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "DB_DESCRIPTION": "SQLite (test)",
        "AS_OF": AS_OF,
        "SECRET_KEY": "test",
        "CSRF_DISABLED": True,
        "TESTING": True,
    })


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app) -> Database:
    return app.extensions["db"]


def login(client, profile_id):
    client.post("/profiles", data={"profile_id": profile_id})
    return client


def actor(db, profile_id, workspace_id=None):
    with db.read() as conn:
        return S.make_actor(conn, profile_id, workspace_id)
