"""Database connection: one :class:`Database` object per app.

``read()`` gives a connection for queries; ``write()`` wraps one atomic
transaction and turns driver failures into :class:`StorageError`, which the UI
explains with a retry.  The storage-failure simulation (Settings) is checked here.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from .tables import meta, metadata


class StorageError(RuntimeError):
    """A write could not be saved.  The UI keeps the user's input and offers a retry."""


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE lines) so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def database_url(base_dir: Path) -> tuple[str, str]:
    """Return (url, description).  MySQL from ``.env`` if configured, else a local
    SQLite file - the UI shows which one is in use."""
    load_dotenv(base_dir / ".env")
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        kind = "MySQL" if url.startswith("mysql") else url.split(":", 1)[0]
        return url, kind
    inst = base_dir / "instance"
    inst.mkdir(exist_ok=True)
    return f"sqlite:///{(inst / 'finance.db').as_posix()}", "SQLite (fallback: no DATABASE_URL in .env)"


class Database:
    def __init__(self, url: str, description: str = ""):
        self.url = url
        self.description = description or url.split(":", 1)[0]
        # Optional extra "is the failure simulation on?" check.  The web app plugs in a
        # per-browser-session switch, so one visitor's test never blocks anyone else.
        self.simulated_failure: Callable[[], bool] = lambda: False
        kwargs = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

    @property
    def is_mysql(self) -> bool:
        return self.url.startswith("mysql")

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    def drop_all(self) -> None:
        metadata.drop_all(self.engine)

    def is_initialised(self) -> bool:
        try:
            with self.engine.connect() as c:
                return c.execute(select(meta.c.value).where(meta.c.key == "seeded")).first() is not None
        except SQLAlchemyError:
            return False

    @contextmanager
    def read(self):
        with self.engine.connect() as conn:
            yield conn

    @contextmanager
    def write(self):
        """One atomic transaction.  Every service write goes through here, so a
        failure never leaves half a change behind."""
        try:
            with self.engine.begin() as conn:
                flag = conn.execute(
                    select(meta.c.value).where(meta.c.key == "simulate_storage_failure")
                ).scalar()
                if flag == "1" or self.simulated_failure():
                    raise StorageError(
                        "Saving is switched off (storage-failure simulation is on in Settings)."
                    )
                yield conn
        except OperationalError as exc:
            raise StorageError(
                "The database did not accept the change (it may be offline, locked or full)."
            ) from exc
