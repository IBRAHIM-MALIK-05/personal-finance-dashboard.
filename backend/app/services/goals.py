"""Savings goals and contributions."""
from __future__ import annotations

from datetime import date

from sqlalchemy import delete, insert, select

from ..database import Database
from ..database import tables as T
from ..engine.dates import DateParseError, parse_flexible
from ..engine.money import format_pkr
from ..engine.validation import sanitize_text, validate_money_field
from .access import Actor, require
from .activity import audit
from .common import now
from .errors import NotFound, ValidationFailed


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

