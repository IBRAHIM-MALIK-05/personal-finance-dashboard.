"""Money records: loading a workspace for the engine, transactions and recurring bills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database, tables as T
from ..engine.categories import CATEGORIES, Rule, categorise, normalise_payee
from ..engine.dates import DateParseError, days_in_month, parse_flexible
from ..engine.models import Budget, Goal, RecurringBill, Txn
from ..engine.money import format_pkr
from ..engine.validation import sanitize_text, validate_money_field, validate_transaction
from .core import Actor, EditConflict, NotFound, ValidationFailed, audit, now, require, versioned_update


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


@dataclass
class TxnPage:
    rows: list[dict]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


def get_txn_row(conn: Connection, actor: Actor, txn_id: int) -> dict:
    r = conn.execute(select(T.transactions).where(
        T.transactions.c.id == txn_id, T.transactions.c.workspace_id == actor.workspace_id)).first()
    if r is None:
        raise NotFound("That transaction no longer exists. It may have been deleted or undone.")
    return dict(r._mapping)


def list_transactions(conn: Connection, actor: Actor, *, q: str = "", category: str = "", month: str = "",
                      batch: int | None = None, page: int = 1, per_page: int = 25) -> TxnPage:
    require(actor, "view_details")
    t = T.transactions
    cond = [t.c.workspace_id == actor.workspace_id]
    q = sanitize_text(q, 60).value
    if q:
        needle = q.lower()
        cond.append(func.lower(t.c.payee).contains(needle, autoescape=True)
                    | func.lower(t.c.description).contains(needle, autoescape=True))
    if category in CATEGORIES:
        cond.append(t.c.category_id == category)
    if len(month) == 7 and month[4] == "-" and month.replace("-", "").isdigit():
        y, m = int(month[:4]), int(month[5:])
        if 1 <= m <= 12:
            cond.append(t.c.posted_on.between(date(y, m, 1), date(y, m, days_in_month(y, m))))
    if batch:
        cond.append(t.c.import_batch_id == batch)
    total = conn.execute(select(func.count()).select_from(t).where(and_(*cond))).scalar_one()
    page = max(1, min(page, max(1, -(-total // per_page))))
    rows = conn.execute(
        select(t).where(and_(*cond)).order_by(t.c.posted_on.desc(), t.c.id.desc())
        .limit(per_page).offset((page - 1) * per_page)).all()
    return TxnPage([dict(r._mapping) for r in rows], total, page, per_page)


def add_transaction(db: Database, actor: Actor, form: dict, today: date) -> int:
    require(actor, "edit")
    res = validate_transaction(form, today)
    if not res.ok:
        raise ValidationFailed(res.errors, form)
    v = res.values
    with db.write() as conn:
        source = "user"
        if v["category_id"] == "auto":
            v["category_id"], _, _ = categorise(v["payee"], v["description"], load_rules(conn, actor.workspace_id))
            source = "rule"
        kind = kind_for(v["category_id"], v["amount"])
        ts = now()
        new_id = conn.execute(insert(T.transactions).values(
            workspace_id=actor.workspace_id, posted_on=v["posted_on"], amount=v["amount"], payee=v["payee"],
            description=v["description"] or v["payee"], category_id=v["category_id"], category_source=source,
            kind=kind, note=v["note"], created_by=actor.profile_id, created_at=ts, updated_at=ts, version=1,
        )).inserted_primary_key[0]
        audit(conn, actor, "transaction.add", f"Added {v['payee']} {format_pkr(v['amount'], signed=True)}")
    return new_id


def update_transaction(db: Database, actor: Actor, txn_id: int, form: dict, version: int, today: date) -> None:
    require(actor, "edit")
    res = validate_transaction(form, today)
    if not res.ok:
        raise ValidationFailed(res.errors, form)
    v = res.values
    with db.write() as conn:
        source = "user"
        if v["category_id"] == "auto":
            v["category_id"], _, _ = categorise(v["payee"], v["description"], load_rules(conn, actor.workspace_id))
            source = "rule"
        versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version, {
            "posted_on": v["posted_on"], "amount": v["amount"], "payee": v["payee"],
            "description": v["description"] or v["payee"], "category_id": v["category_id"],
            "category_source": source, "kind": kind_for(v["category_id"], v["amount"]),
            "note": v["note"], "updated_at": now(),
        }, attempted=v)
        audit(conn, actor, "transaction.edit", f"Edited transaction #{txn_id} ({v['payee']})")


def kind_for(category_id: str, amount: int) -> str:
    """Ledger kind follows the category: transfers are excluded from income and
    spending totals, and money coming back in a spending category is a refund."""
    if category_id == "savings_transfer":
        return "transfer"
    if amount > 0 and CATEGORIES[category_id].group == "expense":
        return "refund"
    return "normal"


def delete_transaction(db: Database, actor: Actor, txn_id: int, version: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        row = get_txn_row(conn, actor, txn_id)
        if row["version"] != version:
            raise EditConflict("transactions", txn_id, row, {"delete": True})
        conn.execute(delete(T.transactions).where(T.transactions.c.id == txn_id))
        audit(conn, actor, "transaction.delete", f"Deleted {row['payee']} {format_pkr(row['amount'])} ({row['posted_on']})")


def similar_transactions(conn: Connection, actor: Actor, txn: dict) -> list[int]:
    """Other transactions from the same payee whose category was set by a rule
    (never ones the person chose by hand)."""
    key = normalise_payee(txn["payee"])
    rows = conn.execute(select(T.transactions.c.id, T.transactions.c.payee, T.transactions.c.category_source)
                        .where(T.transactions.c.workspace_id == actor.workspace_id,
                               T.transactions.c.id != txn["id"])).all()
    return [r.id for r in rows if normalise_payee(r.payee) == key and r.category_source != "user"]


def recategorise(db: Database, actor: Actor, txn_id: int, category_id: str, version: int,
                 apply_to_similar: bool) -> int:
    """Correct a category.  Optionally remember it as a rule for this payee and
    update earlier rule-categorised transactions.  Returns how many changed."""
    require(actor, "edit")
    if category_id not in CATEGORIES:
        raise ValidationFailed({"category_id": "Choose a category from the list."})
    with db.write() as conn:
        row = get_txn_row(conn, actor, txn_id)
        versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version,
                          {"category_id": category_id, "category_source": "user", "updated_at": now()},
                          attempted={"category_id": category_id})
        changed = 1
        if apply_to_similar:
            pattern = normalise_payee(row["payee"])
            exists = conn.execute(select(T.rules.c.id).where(
                T.rules.c.workspace_id == actor.workspace_id, T.rules.c.pattern == pattern,
                T.rules.c.source == "user")).first()
            if exists:
                conn.execute(update(T.rules).where(T.rules.c.id == exists.id).values(category_id=category_id))
            else:
                conn.execute(insert(T.rules).values(workspace_id=actor.workspace_id, pattern=pattern,
                                                    category_id=category_id, priority=10, source="user",
                                                    created_at=now()))
            ids = similar_transactions(conn, actor, row)
            if ids:
                conn.execute(update(T.transactions).where(T.transactions.c.id.in_(ids)).values(
                    category_id=category_id, category_source="rule", updated_at=now(),
                    version=T.transactions.c.version + 1))
            changed += len(ids)
        audit(conn, actor, "category.correct",
              f"{row['payee']}: {CATEGORIES[row['category_id']].name} → {CATEGORIES[category_id].name}"
              + (f" (+{changed - 1} similar, rule saved)" if apply_to_similar else ""))
    return changed


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

