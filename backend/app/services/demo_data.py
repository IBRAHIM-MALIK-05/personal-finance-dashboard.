"""Create the database, load the synthetic demo budgets, reset them, and clear a budget.

Real accounts are never touched by a demo reset: only workspaces and profiles marked
``is_demo`` are deleted and reloaded.
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..database.seed import PROFILES, SeedData, generate
from ..engine.categories import CATEGORIES, SYSTEM_RULES
from .access import Actor, require
from .activity import audit
from .common import now


def system_rules(workspace_id: int) -> list[dict]:
    """The built-in categorisation rules every budget starts with."""
    ts = now()
    return [{"workspace_id": workspace_id, "pattern": p, "category_id": c, "priority": 0, "source": "system",
             "created_at": ts} for p, c in SYSTEM_RULES]


def _insert_demo_budgets(conn: Connection, data: SeedData) -> None:
    ts = now()
    conn.execute(insert(T.workspaces), [{**w, "currency": "PKR", "is_demo": True, "created_at": ts}
                                        for w in data.workspaces])
    conn.execute(insert(T.memberships), data.memberships)
    conn.execute(insert(T.sharing_grants), [{**g, "granted_at": ts} for g in data.grants])
    conn.execute(insert(T.rules), [r for w in data.workspaces for r in system_rules(w["id"])])
    conn.execute(insert(T.recurring_bills), [{**b, "created_at": ts, "version": 1} for b in data.bills])
    conn.execute(insert(T.transactions), [
        {**t, "note": "", "created_by": "seed", "created_at": ts, "updated_at": ts, "version": 1}
        for t in data.transactions])
    conn.execute(insert(T.budgets), [{**b, "version": 1} for b in data.budgets])
    conn.execute(insert(T.goals), [{**g, "version": 1} for g in data.goals])
    conn.execute(insert(T.goal_contributions), data.contributions)
    conn.execute(insert(T.scenarios), [
        {"workspace_id": s["workspace_id"], "name": s["name"], "created_by": s["created_by"],
         "adjustments_json": json.dumps(s["adjustments"]), "created_at": ts} for s in data.scenarios])
    conn.execute(insert(T.coach_notes), [{**n, "created_at": ts} for n in data.notes])


def seed_database(db: Database, as_of: date, data: SeedData | None = None) -> None:
    """Create the tables, the reference data and the demo budgets.  DDL runs here,
    never inside a business transaction (MySQL commits DDL implicitly)."""
    db.create_all()
    with db.engine.begin() as conn:
        conn.execute(insert(T.categories), [
            {"id": c.id, "name": c.name, "grp": c.group} for c in CATEGORIES.values()])
        conn.execute(insert(T.profiles), [{**p, "is_demo": True} for p in PROFILES])
        _insert_demo_budgets(conn, data or generate(as_of))
        conn.execute(insert(T.meta), [{"key": "seeded", "value": as_of.isoformat()},
                                      {"key": "simulate_storage_failure", "value": "0"}])


def reset_demo_data(db: Database, as_of: date) -> None:
    """Put the demo budgets back to their original state.  Real accounts are kept."""
    with db.write() as conn:
        demo_ws = [r.id for r in conn.execute(select(T.workspaces.c.id).where(T.workspaces.c.is_demo))]
        demo_profiles = [r.id for r in conn.execute(select(T.profiles.c.id).where(T.profiles.c.is_demo))]
        if demo_ws:
            conn.execute(delete(T.audit_log).where(T.audit_log.c.workspace_id.in_(demo_ws)))
            conn.execute(delete(T.workspaces).where(T.workspaces.c.id.in_(demo_ws)))  # children cascade
        if demo_profiles:
            conn.execute(delete(T.preferences).where(T.preferences.c.profile_id.in_(demo_profiles)))
        _insert_demo_budgets(conn, generate(as_of))


def reset_database(db: Database, as_of: date) -> None:
    """Drop EVERYTHING (including real accounts) and start again.  Developer tool only."""
    db.drop_all()
    seed_database(db, as_of)


def clear_workspace(db: Database, actor: Actor) -> None:
    """Remove every record from the current budget (the budget and account stay)."""
    require(actor, "edit")
    ws = actor.workspace_id
    with db.write() as conn:
        goal_ids = [r.id for r in conn.execute(select(T.goals.c.id).where(T.goals.c.workspace_id == ws))]
        if goal_ids:
            conn.execute(delete(T.goal_contributions).where(T.goal_contributions.c.goal_id.in_(goal_ids)))
        batch_ids = [r.id for r in conn.execute(select(T.import_batches.c.id).where(T.import_batches.c.workspace_id == ws))]
        if batch_ids:
            conn.execute(delete(T.import_journal).where(T.import_journal.c.batch_id.in_(batch_ids)))
        for table in (T.transactions, T.recurring_bills, T.budgets, T.goals, T.scenarios,
                      T.import_drafts, T.import_batches, T.coach_notes):
            conn.execute(delete(table).where(table.c.workspace_id == ws))
        audit(conn, actor, "workspace.clear", "Cleared all data in this budget")
