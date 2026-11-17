"""Small helpers shared by every service module."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from .errors import EditConflict


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def versioned_update(conn: Connection, table, record_id: int, ws_id: int, version: int, values: dict, attempted: dict):
    res = conn.execute(update(table).where(
        table.c.id == record_id, table.c.workspace_id == ws_id, table.c.version == version,
    ).values(**values, version=table.c.version + 1))
    if res.rowcount == 0:
        cur = conn.execute(select(table).where(table.c.id == record_id, table.c.workspace_id == ws_id)).first()
        raise EditConflict(table.name, record_id, dict(cur._mapping) if cur else None, attempted)

