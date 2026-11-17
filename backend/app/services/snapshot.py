"""Load everything the calculation engine needs for one workspace in one go."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from ..database import tables as T
from ..engine.categories import Rule
from ..engine.models import Budget, Goal, RecurringBill, Txn
from .access import Actor


def txn_from_row(r) -> Txn:
    return Txn(r.id, r.posted_on, r.amount, r.payee, r.description, r.category_id, r.kind,
               r.category_source, r.recurring_id, r.occurrence_date, r.refund_of,
               r.import_batch_id, r.version)


def bill_from_row(r) -> RecurringBill:
    return RecurringBill(r.id, r.name, r.payee, r.category_id, r.amount, r.cadence,
                         r.anchor_date, r.tolerance_pct, r.end_date, bool(r.active), r.version)


@dataclass
class Snapshot:
    """Everything the engine needs for one workspace, loaded in one go."""

    workspace: dict
    txns: list[Txn]
    bills: list[RecurringBill]
    budgets: list[Budget]
    goals: list[Goal]
    rules: list[Rule]
    notes: list[dict] = field(default_factory=list)


def load_snapshot(conn: Connection, actor: Actor) -> Snapshot:
    ws_id = actor.workspace_id
    ws = dict(conn.execute(select(T.workspaces).where(T.workspaces.c.id == ws_id)).first()._mapping)
    txns = [txn_from_row(r) for r in conn.execute(
        select(T.transactions).where(T.transactions.c.workspace_id == ws_id).order_by(T.transactions.c.posted_on))]
    bills = [bill_from_row(r) for r in conn.execute(
        select(T.recurring_bills).where(T.recurring_bills.c.workspace_id == ws_id).order_by(T.recurring_bills.c.name))]
    budgets = [Budget(r.id, r.category_id, r.monthly_limit, r.version) for r in conn.execute(
        select(T.budgets).where(T.budgets.c.workspace_id == ws_id))]
    saved = dict(conn.execute(
        select(T.goal_contributions.c.goal_id, func.sum(T.goal_contributions.c.amount))
        .group_by(T.goal_contributions.c.goal_id)).all())
    goals = [Goal(r.id, r.name, r.target, r.deadline, r.monthly_plan, int(saved.get(r.id) or 0), r.version)
             for r in conn.execute(select(T.goals).where(T.goals.c.workspace_id == ws_id).order_by(T.goals.c.deadline))]
    rules = load_rules(conn, ws_id)
    notes = [dict(r._mapping) for r in conn.execute(
        select(T.coach_notes, T.profiles.c.display_name)
        .join(T.profiles, T.profiles.c.id == T.coach_notes.c.coach_id)
        .where(T.coach_notes.c.workspace_id == ws_id).order_by(T.coach_notes.c.created_at.desc()))]
    return Snapshot(ws, txns, bills, budgets, goals, rules, notes)


def load_rules(conn: Connection, ws_id: int) -> list[Rule]:
    return [Rule(r.id, r.pattern, r.category_id, r.priority, r.source) for r in conn.execute(
        select(T.rules).where(T.rules.c.workspace_id == ws_id))]

