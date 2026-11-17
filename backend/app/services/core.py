"""Shared building blocks: errors, the acting person and their permissions, the audit log.

Every service checks permission on the server through :func:`require`.  Real accounts log in
with email and password (``accounts.py``); demo personas can be opened by anyone and only
ever see the demo budgets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from ..database import tables as T


class Forbidden(Exception):
    """The current profile may not do this.  Shown as a 403 page."""


class NotFound(Exception):
    pass


class ValidationFailed(Exception):
    def __init__(self, errors: dict[str, str], values: dict | None = None):
        super().__init__("; ".join(errors.values()))
        self.errors = errors
        self.values = values or {}


class EditConflict(Exception):
    """Someone else saved this record after you opened it (E3 hook)."""

    def __init__(self, table: str, record_id: int, current: dict | None, attempted: dict):
        super().__init__(f"{table} {record_id} changed since you opened it")
        self.table, self.record_id, self.current, self.attempted = table, record_id, current, attempted


class ImportBlocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__(" ".join(reasons))
        self.reasons = reasons


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def versioned_update(conn: Connection, table, record_id: int, ws_id: int, version: int, values: dict, attempted: dict):
    res = conn.execute(update(table).where(
        table.c.id == record_id, table.c.workspace_id == ws_id, table.c.version == version,
    ).values(**values, version=table.c.version + 1))
    if res.rowcount == 0:
        cur = conn.execute(select(table).where(table.c.id == record_id, table.c.workspace_id == ws_id)).first()
        raise EditConflict(table.name, record_id, dict(cur._mapping) if cur else None, attempted)


ROLE_LABELS = {
    "owner": "Personal budget owner",
    "manager": "Household manager",
    "coach": "Financial coach (read-only)",
}


@dataclass
class Actor:
    profile_id: str
    name: str
    role: str
    workspace_id: int
    workspace_name: str
    workspace_kind: str
    access: str               # edit | view
    scope: str | None = None  # coach grant scope: summary | full
    is_demo: bool = False     # a demo budget (resettable, coach sharing allowed)
    email: str | None = None  # set for real accounts

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    def can(self, action: str) -> bool:
        if action == "view":
            return True
        if action == "view_details":
            return self.access == "edit" or self.scope == "full"
        if action in ("edit", "import", "undo"):
            return self.access == "edit"
        if action == "manage_sharing":  # the coach is a demo persona, so only demo households can share
            return self.access == "edit" and self.workspace_kind == "household" and self.is_demo
        if action == "note":
            return self.role == "coach"
        return False


def require(actor: Actor, action: str) -> None:
    if not actor.can(action):
        raise Forbidden(
            {
                "view_details": "Transaction details are not shared with you. The household shared summaries only.",
                "edit": "You have read-only access to this budget.",
                "import": "Only people who can edit this budget can import files.",
                "undo": "Only people who can edit this budget can undo imports.",
                "manage_sharing": "Only household managers can change sharing.",
                "note": "Only coaches can leave coach notes.",
            }.get(action, "You do not have permission to do that.")
        )


def list_demo_profiles(conn: Connection) -> list[dict]:
    """Demo personas for the "Try the demo" page.  Real accounts are never listed."""
    return [dict(r._mapping) for r in conn.execute(
        select(T.profiles).where(T.profiles.c.is_demo).order_by(T.profiles.c.role))]


def accessible_workspaces(conn: Connection, profile_id: str) -> list[dict]:
    """Workspaces a profile may open: memberships plus active coach grants."""
    out = []
    q = (select(T.workspaces, T.memberships.c.permission)
         .join(T.memberships, T.memberships.c.workspace_id == T.workspaces.c.id)
         .where(T.memberships.c.profile_id == profile_id, ~T.workspaces.c.is_deleted))
    for r in conn.execute(q):
        out.append({**r._mapping, "access": r.permission, "scope": None})
    q = (select(T.workspaces, T.sharing_grants.c.scope, T.sharing_grants.c.granted_at)
         .join(T.sharing_grants, T.sharing_grants.c.workspace_id == T.workspaces.c.id)
         .where(T.sharing_grants.c.coach_id == profile_id, T.sharing_grants.c.revoked_at.is_(None),
                ~T.workspaces.c.is_deleted))
    for r in conn.execute(q):
        out.append({**r._mapping, "access": "view"})
    return out


def make_actor(conn: Connection, profile_id: str, workspace_id: int | None) -> Actor | None:
    prof = conn.execute(select(T.profiles).where(T.profiles.c.id == profile_id)).first()
    if prof is None or prof.is_deleted:  # soft-deleted accounts can't act
        return None
    options = accessible_workspaces(conn, profile_id)
    if not options:
        return None
    chosen = next((w for w in options if w["id"] == workspace_id), None) or options[0]
    return Actor(prof.id, prof.display_name, prof.role, chosen["id"], chosen["name"],
                 chosen["kind"], chosen["access"], chosen.get("scope"), bool(chosen["is_demo"]), prof.email)


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

