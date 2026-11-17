"""Planning: monthly budget limits, savings goals and saved what-if scenarios.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from ..database import Database, tables as T
from ..engine.categories import CATEGORIES
from ..engine.dates import DateParseError, parse_flexible
from ..engine.models import Adjustment
from ..engine.money import format_pkr
from ..engine.validation import sanitize_text, validate_money_field
from .core import Actor, NotFound, ValidationFailed, audit, now, require, versioned_update


def set_budget(db: Database, actor: Actor, category_id: str, limit_text: str, version: int | None) -> None:
    require(actor, "edit")
    if category_id not in CATEGORIES or CATEGORIES[category_id].group != "expense":
        raise ValidationFailed({"category_id": "Choose a spending category."})
    limit, err = validate_money_field(limit_text, "Monthly limit")
    if err:
        raise ValidationFailed({"monthly_limit": err}, {"monthly_limit": limit_text, "category_id": category_id})
    with db.write() as conn:
        cur = conn.execute(select(T.budgets).where(
            T.budgets.c.workspace_id == actor.workspace_id, T.budgets.c.category_id == category_id)).first()
        if cur is None:
            conn.execute(insert(T.budgets).values(workspace_id=actor.workspace_id, category_id=category_id,
                                                  monthly_limit=limit, version=1))
            audit(conn, actor, "budget.add", f"{CATEGORIES[category_id].name} budget {format_pkr(limit)}")
        else:
            versioned_update(conn, T.budgets, cur.id, actor.workspace_id,
                              version if version is not None else cur.version,
                              {"monthly_limit": limit}, attempted={"monthly_limit": limit})
            audit(conn, actor, "budget.edit",
                  f"{CATEGORIES[category_id].name} budget {format_pkr(cur.monthly_limit)} → {format_pkr(limit)}")


def delete_budget(db: Database, actor: Actor, category_id: str) -> None:
    require(actor, "edit")
    if category_id not in CATEGORIES:
        raise ValidationFailed({"category_id": "That budget category does not exist."})
    with db.write() as conn:
        conn.execute(delete(T.budgets).where(T.budgets.c.workspace_id == actor.workspace_id,
                                             T.budgets.c.category_id == category_id))
        audit(conn, actor, "budget.delete", f"Removed {CATEGORIES[category_id].name} budget")


def add_goal(db: Database, actor: Actor, form: dict, today: date) -> None:
    require(actor, "edit")
    errors = {}
    name = sanitize_text(form.get("name", ""), 80).value
    if not name:
        errors["name"] = "Name the goal, for example ‘New laptop’."
    target, e1 = validate_money_field(form.get("target"), "Target")
    plan, e2 = validate_money_field(form.get("monthly_plan") or "0", "Monthly plan", allow_zero=True)
    if e1:
        errors["target"] = e1
    if e2:
        errors["monthly_plan"] = e2
    try:
        deadline = parse_flexible(form.get("deadline", ""), "DMY").value
        if deadline is None or deadline <= today:
            errors["deadline"] = "Choose a deadline after today."
    except DateParseError as exc:
        errors["deadline"] = str(exc)
        deadline = None
    if errors:
        raise ValidationFailed(errors, form)
    with db.write() as conn:
        conn.execute(insert(T.goals).values(workspace_id=actor.workspace_id, name=name, target=target,
                                            deadline=deadline, monthly_plan=plan, created_on=today, version=1))
        audit(conn, actor, "goal.add", f"Goal {name} {format_pkr(target)} by {deadline}")


def contribute(db: Database, actor: Actor, goal_id: int, form: dict, today: date) -> None:
    require(actor, "edit")
    amount, err = validate_money_field(form.get("amount"), "Amount")
    if err:
        raise ValidationFailed({"amount": err}, form)
    with db.write() as conn:
        g = conn.execute(select(T.goals).where(T.goals.c.id == goal_id,
                                               T.goals.c.workspace_id == actor.workspace_id)).first()
        if g is None:
            raise NotFound("That goal no longer exists.")
        transfer_id = None
        if form.get("record_transfer") == "1":
            ts = now()
            transfer_id = conn.execute(insert(T.transactions).values(
                workspace_id=actor.workspace_id, posted_on=today, amount=-amount, payee="Savings Account",
                description=f"Transfer to savings: {g.name}", category_id="savings_transfer",
                category_source="user", kind="transfer", note="", created_by=actor.profile_id,
                created_at=ts, updated_at=ts, version=1)).inserted_primary_key[0]
        conn.execute(insert(T.goal_contributions).values(goal_id=goal_id, on_date=today, amount=amount,
                                                         note=sanitize_text(form.get("note", ""), 200).value,
                                                         transfer_txn_id=transfer_id))
        audit(conn, actor, "goal.contribute",
              f"{format_pkr(amount)} to {g.name}" + (" with transfer" if transfer_id else " (allocation only)"))


def delete_goal(db: Database, actor: Actor, goal_id: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        g = conn.execute(select(T.goals).where(T.goals.c.id == goal_id,
                                               T.goals.c.workspace_id == actor.workspace_id)).first()
        if g is None:
            raise NotFound("That goal no longer exists.")
        conn.execute(delete(T.goal_contributions).where(T.goal_contributions.c.goal_id == goal_id))
        conn.execute(delete(T.goals).where(T.goals.c.id == goal_id))
        audit(conn, actor, "goal.delete", f"Deleted goal {g.name} (transfers already recorded are kept)")


def list_scenarios(conn: Connection, actor: Actor) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(
        select(T.scenarios).where(T.scenarios.c.workspace_id == actor.workspace_id).order_by(T.scenarios.c.id))]


def adjustments_from_json(text: str) -> list[Adjustment]:
    out = []
    for a in json.loads(text or "[]"):
        on = a.get("on")
        out.append(Adjustment(
            kind=a["kind"], bill_id=a.get("bill_id"), category_id=a.get("category_id"),
            percent=int(a.get("percent", 0)), amount=int(a.get("amount", 0)),
            on=date.fromisoformat(on) if on else None, cadence=a.get("cadence", "monthly"),
            label=a.get("label", ""),
        ))
    return out


def adjustments_to_json(adjs: Iterable[Adjustment]) -> str:
    return json.dumps([
        {"kind": a.kind, "bill_id": a.bill_id, "category_id": a.category_id, "percent": a.percent,
         "amount": a.amount, "on": a.on.isoformat() if a.on else None, "cadence": a.cadence, "label": a.label}
        for a in adjs
    ])


def save_scenario(db: Database, actor: Actor, name: str, adjs: list[Adjustment]) -> None:
    require(actor, "edit")
    name = sanitize_text(name, 80).value or "Untitled scenario"
    if not adjs:
        raise ValidationFailed({"adjustments": "Add at least one change before saving."})
    with db.write() as conn:
        conn.execute(insert(T.scenarios).values(workspace_id=actor.workspace_id, name=name,
                                                adjustments_json=adjustments_to_json(adjs),
                                                created_by=actor.profile_id, created_at=now()))
        audit(conn, actor, "scenario.save", f"Saved what-if ‘{name}’ ({len(adjs)} changes)")


def delete_scenario(db: Database, actor: Actor, scenario_id: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        conn.execute(delete(T.scenarios).where(T.scenarios.c.id == scenario_id,
                                               T.scenarios.c.workspace_id == actor.workspace_id))
        audit(conn, actor, "scenario.delete", f"Deleted what-if #{scenario_id}")

