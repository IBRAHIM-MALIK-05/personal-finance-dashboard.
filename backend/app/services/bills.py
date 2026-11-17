"""Recurring bills and income, and confirming which payment paid a bill."""
from __future__ import annotations

from datetime import date

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..engine.categories import CATEGORIES
from ..engine.dates import DateParseError, parse_flexible
from ..engine.money import format_pkr
from ..engine.validation import sanitize_text, validate_money_field
from .access import Actor, require
from .activity import audit
from .common import now, versioned_update
from .errors import NotFound, ValidationFailed


def _bill_values(form: dict) -> dict:
    errors: dict[str, str] = {}
    name = sanitize_text(form.get("name", ""), 80).value
    payee = sanitize_text(form.get("payee", "") or name, 80).value
    if not name:
        errors["name"] = "Give the bill a name, for example ‘Electricity’."
    amount, err = validate_money_field(form.get("amount"), "Amount")
    if err:
        errors["amount"] = err
    cadence = form.get("cadence", "monthly")
    if cadence not in ("weekly", "monthly", "quarterly", "yearly"):
        errors["cadence"] = "Choose how often it repeats."
    try:
        p = parse_flexible(form.get("anchor_date", ""), "DMY")
        anchor = p.value
        if anchor is None:
            raise DateParseError("Date is ambiguous.")
    except DateParseError as exc:
        errors["anchor_date"] = str(exc)
        anchor = None
    cat = form.get("category_id", "")
    if cat not in CATEGORIES:
        errors["category_id"] = "Choose a category."
    try:
        tol = int(form.get("tolerance_pct", 10))
        if not 0 <= tol <= 100:
            raise ValueError
    except ValueError:
        errors["tolerance_pct"] = "Variation must be a whole number between 0 and 100."
        tol = 10
    direction = form.get("direction", "out")
    if errors:
        raise ValidationFailed(errors, form)
    return {"name": name, "payee": payee, "amount": amount if direction == "in" else -amount,
            "cadence": cadence, "anchor_date": anchor, "category_id": cat, "tolerance_pct": tol}


def get_bill(conn: Connection, actor: Actor, bill_id: int) -> dict:
    r = conn.execute(select(T.recurring_bills).where(
        T.recurring_bills.c.id == bill_id, T.recurring_bills.c.workspace_id == actor.workspace_id)).first()
    if r is None:
        raise NotFound("That recurring item no longer exists.")
    return dict(r._mapping)


def add_bill(db: Database, actor: Actor, form: dict) -> int:
    require(actor, "edit")
    v = _bill_values(form)
    with db.write() as conn:
        new_id = conn.execute(insert(T.recurring_bills).values(
            workspace_id=actor.workspace_id, active=True, created_at=now(), version=1, **v)).inserted_primary_key[0]
        audit(conn, actor, "bill.add", f"Added recurring {v['name']} {format_pkr(v['amount'])} {v['cadence']}")
    return new_id


def update_bill(db: Database, actor: Actor, bill_id: int, form: dict, version: int) -> None:
    require(actor, "edit")
    v = _bill_values(form)
    with db.write() as conn:
        versioned_update(conn, T.recurring_bills, bill_id, actor.workspace_id, version, v, attempted=v)
        audit(conn, actor, "bill.edit", f"Edited recurring {v['name']}")


def set_bill_active(db: Database, actor: Actor, bill_id: int, active: bool, version: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        versioned_update(conn, T.recurring_bills, bill_id, actor.workspace_id, version, {"active": active},
                          attempted={"active": active})
        audit(conn, actor, "bill.pause" if not active else "bill.resume", f"Bill #{bill_id} {'resumed' if active else 'paused'}")


def delete_bill(db: Database, actor: Actor, bill_id: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        r = conn.execute(select(T.recurring_bills).where(
            T.recurring_bills.c.id == bill_id, T.recurring_bills.c.workspace_id == actor.workspace_id)).first()
        if r is None:
            raise NotFound("That recurring item no longer exists.")
        conn.execute(update(T.transactions).where(T.transactions.c.recurring_id == bill_id)
                     .values(recurring_id=None, occurrence_date=None))
        conn.execute(delete(T.recurring_bills).where(T.recurring_bills.c.id == bill_id))
        audit(conn, actor, "bill.delete", f"Deleted recurring {r.name} (past payments kept)")


def confirm_payment(db: Database, actor: Actor, bill_id: int, occurrence: date, txn_id: int, version: int) -> None:
    """Link a transaction to a bill occurrence, so the forecast stops projecting it."""
    require(actor, "edit")
    with db.write() as conn:
        bill = conn.execute(select(T.recurring_bills).where(
            T.recurring_bills.c.id == bill_id, T.recurring_bills.c.workspace_id == actor.workspace_id)).first()
        if bill is None:
            raise NotFound("That recurring item no longer exists.")
        versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version,
                          {"recurring_id": bill_id, "occurrence_date": occurrence, "updated_at": now()},
                          attempted={"link": bill_id})
        audit(conn, actor, "bill.confirm_payment", f"Confirmed {bill.name} due {occurrence} as paid by #{txn_id}")

