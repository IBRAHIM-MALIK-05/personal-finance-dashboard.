"""Audit log: every change is recorded with who did it and when."""
from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection

from ..database import tables as T
from .access import Actor
from .common import now

REDACTED = {
    "transaction": "A transaction was added, edited or deleted (details not shared)",
    "category": "A category was corrected (details not shared)",
    "import": "Transactions were imported or an import was undone (details not shared)",
    "goal": "A savings goal was updated (details not shared)",
    "export": "Data was exported",
}


def audit(conn: Connection, actor: Actor, action: str, detail: str) -> None:
    conn.execute(insert(T.audit_log).values(
        workspace_id=actor.workspace_id, profile_id=actor.profile_id, at=now(),
        action=action, detail=detail[:300],
    ))


def audit_entries(conn: Connection, actor: Actor, limit: int = 100) -> list[dict]:
    q = (select(T.audit_log, T.profiles.c.display_name)
         .join(T.profiles, T.profiles.c.id == T.audit_log.c.profile_id)
         .where(T.audit_log.c.workspace_id == actor.workspace_id)
         .order_by(T.audit_log.c.id.desc()).limit(limit))
    rows = [dict(r._mapping) for r in conn.execute(q)]
    if not actor.can("view_details"):
        # Summary-only viewers see that something happened, never transaction details.
        for r in rows:
            r["detail"] = REDACTED.get(r["action"].split(".")[0], "Change recorded (details not shared)")
    return rows

