"""Household-to-coach sharing (consent grants) and coach notes."""
from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..engine.validation import sanitize_text
from .access import Actor, require
from .activity import audit
from .common import now
from .errors import ValidationFailed


def coach_clients(conn: Connection, coach_id: str) -> list[dict]:
    """For the demo coach's home page: the demo households, and whether each is shared."""
    rows = []
    for ws in conn.execute(select(T.workspaces).where(T.workspaces.c.kind == "household", T.workspaces.c.is_demo)):
        g = conn.execute(
            select(T.sharing_grants)
            .where(T.sharing_grants.c.workspace_id == ws.id, T.sharing_grants.c.coach_id == coach_id)
            .order_by(T.sharing_grants.c.granted_at.desc())
        ).first()
        rows.append({"workspace": dict(ws._mapping), "grant": dict(g._mapping) if g else None})
    return rows


def current_grant(conn: Connection, workspace_id: int, coach_id: str = "coach_sara") -> dict | None:
    r = conn.execute(select(T.sharing_grants).where(
        T.sharing_grants.c.workspace_id == workspace_id, T.sharing_grants.c.coach_id == coach_id,
        T.sharing_grants.c.revoked_at.is_(None))).first()
    return dict(r._mapping) if r else None


def set_sharing(db: Database, actor: Actor, scope: str | None, coach_id: str = "coach_sara") -> None:
    """scope = 'summary' | 'full' | None (revoke).  Each change is a new grant
    record, so the history of consent is kept."""
    require(actor, "manage_sharing")
    if scope not in (None, "summary", "full"):
        raise ValidationFailed({"scope": "Choose what to share."})
    with db.write() as conn:
        conn.execute(update(T.sharing_grants).where(
            T.sharing_grants.c.workspace_id == actor.workspace_id, T.sharing_grants.c.coach_id == coach_id,
            T.sharing_grants.c.revoked_at.is_(None)).values(revoked_at=now()))
        if scope:
            conn.execute(insert(T.sharing_grants).values(workspace_id=actor.workspace_id, coach_id=coach_id,
                                                         scope=scope, granted_by=actor.profile_id, granted_at=now()))
        audit(conn, actor, "sharing.change", f"Coach access: {scope or 'revoked'}")


def add_note(db: Database, actor: Actor, target: str, text: str) -> None:
    require(actor, "note")
    clean = sanitize_text(text, 600).value
    if not clean:
        raise ValidationFailed({"text": "Write a note first."})
    with db.write() as conn:
        conn.execute(insert(T.coach_notes).values(workspace_id=actor.workspace_id, coach_id=actor.profile_id,
                                                  target=sanitize_text(target, 40).value or "general",
                                                  text=clean, created_at=now()))
        audit(conn, actor, "note.add", f"Coach note on {target}")

