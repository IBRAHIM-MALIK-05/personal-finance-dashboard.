"""Application services: the only code that reads and writes the database.

Every public function takes an :class:`Actor` and checks permission *on the
server* before touching data.  Writes go through ``db.write()`` (one atomic
transaction each).  Demo profiles are not real authentication; this module
is where real authorisation would plug in.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from . import db as T
from .db import Database
from .engine import importer
from .engine.categories import CATEGORIES, SYSTEM_RULES, Rule, categorise, normalise_payee
from .engine.models import Adjustment, Budget, Goal, RecurringBill, Txn
from .engine.validation import csv_safe, sanitize_text, validate_transaction
from .engine.money import format_pkr, format_rupees_plain
from .seed import PROFILES, SeedData, generate

ROLE_LABELS = {
    "owner": "Personal budget owner",
    "manager": "Household manager",
    "coach": "Financial coach (read-only)",
}
DEFAULT_LENS = {"owner": "summary", "manager": "standard", "coach": "detailed"}


# ------------------------------------------------------------------ errors --

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


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ------------------------------------------------------------------ actors --

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
        if action == "manage_sharing":
            return self.access == "edit" and self.workspace_kind == "household"
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


def list_profiles(conn: Connection) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(select(T.profiles).order_by(T.profiles.c.role))]


def accessible_workspaces(conn: Connection, profile_id: str) -> list[dict]:
    """Workspaces a profile may open: memberships plus active coach grants."""
    out = []
    q = (select(T.workspaces, T.memberships.c.permission)
         .join(T.memberships, T.memberships.c.workspace_id == T.workspaces.c.id)
         .where(T.memberships.c.profile_id == profile_id))
    for r in conn.execute(q):
        out.append({**r._mapping, "access": r.permission, "scope": None})
    q = (select(T.workspaces, T.sharing_grants.c.scope, T.sharing_grants.c.granted_at)
         .join(T.sharing_grants, T.sharing_grants.c.workspace_id == T.workspaces.c.id)
         .where(T.sharing_grants.c.coach_id == profile_id, T.sharing_grants.c.revoked_at.is_(None)))
    for r in conn.execute(q):
        out.append({**r._mapping, "access": "view"})
    return out


def make_actor(conn: Connection, profile_id: str, workspace_id: int | None) -> Actor | None:
    prof = conn.execute(select(T.profiles).where(T.profiles.c.id == profile_id)).first()
    if prof is None:
        return None
    options = accessible_workspaces(conn, profile_id)
    if not options:
        return None
    chosen = next((w for w in options if w["id"] == workspace_id), None) or options[0]
    return Actor(prof.id, prof.display_name, prof.role, chosen["id"], chosen["name"],
                 chosen["kind"], chosen["access"], chosen.get("scope"))


def coach_clients(conn: Connection, coach_id: str) -> list[dict]:
    """For the coach home page: every household, and whether it is shared."""
    rows = []
    for ws in conn.execute(select(T.workspaces).where(T.workspaces.c.kind == "household")):
        g = conn.execute(
            select(T.sharing_grants)
            .where(T.sharing_grants.c.workspace_id == ws.id, T.sharing_grants.c.coach_id == coach_id)
            .order_by(T.sharing_grants.c.granted_at.desc())
        ).first()
        rows.append({"workspace": dict(ws._mapping), "grant": dict(g._mapping) if g else None})
    return rows


# ---------------------------------------------------------------- audit ----

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


REDACTED = {
    "transaction": "A transaction was added, edited or deleted (details not shared)",
    "category": "A category was corrected (details not shared)",
    "import": "Transactions were imported or an import was undone (details not shared)",
    "goal": "A savings goal was updated (details not shared)",
    "export": "Data was exported",
}


# ---------------------------------------------------------- loading data --

def _txn(r) -> Txn:
    return Txn(r.id, r.posted_on, r.amount, r.payee, r.description, r.category_id, r.kind,
               r.category_source, r.recurring_id, r.occurrence_date, r.refund_of,
               r.import_batch_id, r.version)


def _bill(r) -> RecurringBill:
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
    txns = [_txn(r) for r in conn.execute(
        select(T.transactions).where(T.transactions.c.workspace_id == ws_id).order_by(T.transactions.c.posted_on))]
    bills = [_bill(r) for r in conn.execute(
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


def get_txn_row(conn: Connection, actor: Actor, txn_id: int) -> dict:
    r = conn.execute(select(T.transactions).where(
        T.transactions.c.id == txn_id, T.transactions.c.workspace_id == actor.workspace_id)).first()
    if r is None:
        raise NotFound("That transaction no longer exists. It may have been deleted or undone.")
    return dict(r._mapping)


# ----------------------------------------------------------- transactions --

@dataclass
class TxnPage:
    rows: list[dict]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


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
            from .engine.dates import days_in_month
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
        _versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version, {
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


def _versioned_update(conn: Connection, table, record_id: int, ws_id: int, version: int, values: dict, attempted: dict):
    res = conn.execute(update(table).where(
        table.c.id == record_id, table.c.workspace_id == ws_id, table.c.version == version,
    ).values(**values, version=table.c.version + 1))
    if res.rowcount == 0:
        cur = conn.execute(select(table).where(table.c.id == record_id, table.c.workspace_id == ws_id)).first()
        raise EditConflict(table.name, record_id, dict(cur._mapping) if cur else None, attempted)


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
        _versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version,
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


# ------------------------------------------------------------------ bills --

def _bill_values(form: dict) -> dict:
    from .engine.dates import DateParseError, parse_flexible
    from .engine.validation import validate_money_field
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
        _versioned_update(conn, T.recurring_bills, bill_id, actor.workspace_id, version, v, attempted=v)
        audit(conn, actor, "bill.edit", f"Edited recurring {v['name']}")


def set_bill_active(db: Database, actor: Actor, bill_id: int, active: bool, version: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        _versioned_update(conn, T.recurring_bills, bill_id, actor.workspace_id, version, {"active": active},
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
        _versioned_update(conn, T.transactions, txn_id, actor.workspace_id, version,
                          {"recurring_id": bill_id, "occurrence_date": occurrence, "updated_at": now()},
                          attempted={"link": bill_id})
        audit(conn, actor, "bill.confirm_payment", f"Confirmed {bill.name} due {occurrence} as paid by #{txn_id}")


# ---------------------------------------------------------------- budgets --

def set_budget(db: Database, actor: Actor, category_id: str, limit_text: str, version: int | None) -> None:
    from .engine.validation import validate_money_field
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
            _versioned_update(conn, T.budgets, cur.id, actor.workspace_id,
                              version if version is not None else cur.version,
                              {"monthly_limit": limit}, attempted={"monthly_limit": limit})
            audit(conn, actor, "budget.edit",
                  f"{CATEGORIES[category_id].name} budget {format_pkr(cur.monthly_limit)} → {format_pkr(limit)}")


def delete_budget(db: Database, actor: Actor, category_id: str) -> None:
    require(actor, "edit")
    with db.write() as conn:
        conn.execute(delete(T.budgets).where(T.budgets.c.workspace_id == actor.workspace_id,
                                             T.budgets.c.category_id == category_id))
        audit(conn, actor, "budget.delete", f"Removed {CATEGORIES.get(category_id).name} budget")


# ------------------------------------------------------------------ goals --

def add_goal(db: Database, actor: Actor, form: dict, today: date) -> None:
    from .engine.dates import DateParseError, parse_flexible
    from .engine.validation import validate_money_field
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
    from .engine.validation import validate_money_field
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


# -------------------------------------------------------------- scenarios --

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


# ---------------------------------------------------------------- sharing --

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


# ------------------------------------------------------------ preferences --

def get_prefs(conn: Connection, profile_id: str, role: str) -> dict:
    r = conn.execute(select(T.preferences).where(T.preferences.c.profile_id == profile_id)).first()
    if r:
        return dict(r._mapping)
    return {"profile_id": profile_id, "lens": DEFAULT_LENS[role], "theme": "system", "language": "en",
            "reduced_motion": "system", "horizon": 30}


def set_prefs(db: Database, profile_id: str, role: str, **changes) -> dict:
    allowed = {"lens": ("summary", "standard", "detailed"), "theme": ("system", "light", "dark", "contrast"),
               "language": ("en", "ur"), "reduced_motion": ("system", "on", "off"), "horizon": (30, 60, 90)}
    with db.engine.begin() as conn:  # preferences still save during the storage-failure demo
        prefs = get_prefs(conn, profile_id, role)
        for k, v in changes.items():
            if k == "horizon":
                v = int(v) if str(v).isdigit() else 30
            if k in allowed and v in allowed[k]:
                prefs[k] = v
        exists = conn.execute(select(T.preferences.c.profile_id).where(T.preferences.c.profile_id == profile_id)).first()
        values = {k: prefs[k] for k in ("lens", "theme", "language", "reduced_motion", "horizon")}
        if exists:
            conn.execute(update(T.preferences).where(T.preferences.c.profile_id == profile_id).values(**values))
        else:
            conn.execute(insert(T.preferences).values(profile_id=profile_id, **values))
    return prefs


def get_meta(conn: Connection, key: str, default: str = "") -> str:
    v = conn.execute(select(T.meta.c.value).where(T.meta.c.key == key)).scalar()
    return default if v is None else v


def set_meta(db: Database, key: str, value: str) -> None:
    with db.engine.begin() as conn:  # bypasses the failure simulation on purpose
        if conn.execute(select(T.meta.c.key).where(T.meta.c.key == key)).first():
            conn.execute(update(T.meta).where(T.meta.c.key == key).values(value=value))
        else:
            conn.execute(insert(T.meta).values(key=key, value=value))


# ----------------------------------------------------------------- import --

def create_draft(db: Database, actor: Actor, file_name: str, content: bytes) -> int:
    require(actor, "import")
    header, body = importer.read_csv(content)  # raises ImportFatal with a recovery message
    text = content.decode("utf-8-sig")
    settings = importer.Settings(mapping=importer.guess_mapping(header))
    with db.write() as conn:
        conn.execute(delete(T.import_drafts).where(T.import_drafts.c.workspace_id == actor.workspace_id,
                                                   T.import_drafts.c.profile_id == actor.profile_id))
        draft_id = conn.execute(insert(T.import_drafts).values(
            workspace_id=actor.workspace_id, profile_id=actor.profile_id,
            file_name=sanitize_text(file_name, 200).value or "upload.csv", content=text,
            settings_json=settings.to_json(), created_at=now())).inserted_primary_key[0]
        audit(conn, actor, "import.upload", f"Uploaded {file_name} ({len(body)} rows) for review")
    return draft_id


def get_draft(conn: Connection, actor: Actor, draft_id: int) -> dict:
    require(actor, "import")
    r = conn.execute(select(T.import_drafts).where(
        T.import_drafts.c.id == draft_id, T.import_drafts.c.workspace_id == actor.workspace_id,
        T.import_drafts.c.profile_id == actor.profile_id)).first()
    if r is None:
        raise NotFound("This import draft has expired or was already imported. Start the import again.")
    return dict(r._mapping)


def save_draft_settings(db: Database, actor: Actor, draft_id: int, settings: importer.Settings) -> None:
    require(actor, "import")
    with db.write() as conn:
        get_draft(conn, actor, draft_id)
        conn.execute(update(T.import_drafts).where(T.import_drafts.c.id == draft_id)
                     .values(settings_json=settings.to_json()))


def analyse_draft(conn: Connection, actor: Actor, draft: dict, today: date) -> importer.Analysis:
    existing = [_txn(r) for r in conn.execute(select(T.transactions).where(
        T.transactions.c.workspace_id == actor.workspace_id))]
    return importer.analyse(draft["content"], importer.Settings.from_json(draft["settings_json"]),
                            existing, load_rules(conn, actor.workspace_id), today)


class ImportBlocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__(" ".join(reasons))
        self.reasons = reasons


def commit_draft(db: Database, actor: Actor, draft_id: int, today: date) -> int:
    """Insert every ready row, the batch record and the journal in ONE
    transaction.  If anything fails, nothing is imported."""
    require(actor, "import")
    with db.write() as conn:
        draft = get_draft(conn, actor, draft_id)
        analysis = analyse_draft(conn, actor, draft, today)  # re-checked inside the transaction
        if not analysis.can_commit:
            raise ImportBlocked(analysis.blocking)
        ts = now()
        rows = analysis.to_import
        skipped = [r for r in analysis.rows if r.status == "skipped"]
        summary = {
            "imported": len(rows), "skipped": len(skipped),
            "skipped_rows": [{"row": r.row_no, "raw": r.raw, "reasons": [i.message for i in r.issues
                              if i.severity in ("error", "decision")]} for r in skipped],
            "refunds": sum(1 for r in rows if r.kind == "refund"),
            "kept_duplicates": sum(1 for r in rows if r.duplicate_of or r.duplicate_of_row),
            "sign": analysis.sign, "date_order": analysis.date_order,
        }
        batch_id = conn.execute(insert(T.import_batches).values(
            workspace_id=actor.workspace_id, profile_id=actor.profile_id, file_name=draft["file_name"],
            imported_at=ts, row_count=len(rows), skipped_count=len(skipped), status="committed",
            summary_json=json.dumps(summary))).inserted_primary_key[0]
        mapping_json = json.dumps({"columns": {f: analysis.header[i] for f, i in analysis.mapping.items()
                                               if i < len(analysis.header)},
                                   "sign": analysis.sign, "date_order": analysis.date_order})
        new_ids: dict[int, int] = {}
        for r in rows:
            new_id = conn.execute(insert(T.transactions).values(
                workspace_id=actor.workspace_id, posted_on=r.posted_on, amount=r.amount, payee=r.payee or r.description[:80],
                description=r.description or r.payee, category_id=r.category_id,
                category_source="import" if r.category_source == "import" else "rule",
                kind=r.kind, refund_of=r.refund_of, import_batch_id=batch_id,
                raw_row=json.dumps({"row": r.row_no, "values": r.raw}, ensure_ascii=False),
                source_mapping=mapping_json, note="", created_by=actor.profile_id,
                created_at=ts, updated_at=ts, version=1)).inserted_primary_key[0]
            new_ids[r.row_no] = new_id
            conn.execute(insert(T.import_journal).values(batch_id=batch_id, table_name="transactions",
                                                         record_id=new_id, op="insert", after_version=1, at=ts))
        for r in rows:  # refunds whose original purchase was in the same file
            if r.refund_of_row and r.refund_of_row in new_ids:
                conn.execute(update(T.transactions).where(T.transactions.c.id == new_ids[r.row_no])
                             .values(refund_of=new_ids[r.refund_of_row]))
        conn.execute(delete(T.import_drafts).where(T.import_drafts.c.id == draft_id))
        audit(conn, actor, "import.commit",
              f"Imported {len(rows)} transactions from {draft['file_name']} ({len(skipped)} rows skipped)")
    return batch_id


def list_batches(conn: Connection, actor: Actor) -> list[dict]:
    rows = conn.execute(select(T.import_batches).where(T.import_batches.c.workspace_id == actor.workspace_id)
                        .order_by(T.import_batches.c.id.desc())).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        d["summary"] = json.loads(d["summary_json"])
        out.append(d)
    return out


def get_batch(conn: Connection, actor: Actor, batch_id: int) -> dict:
    r = conn.execute(select(T.import_batches).where(T.import_batches.c.id == batch_id,
                                                    T.import_batches.c.workspace_id == actor.workspace_id)).first()
    if r is None:
        raise NotFound("That import does not exist in this budget.")
    d = dict(r._mapping)
    d["summary"] = json.loads(d["summary_json"])
    return d


@dataclass
class UndoPreview:
    batch: dict
    unchanged: list[dict]
    edited: list[dict]
    missing: list[int]

    @property
    def needs_choice(self) -> bool:
        return bool(self.edited)


def undo_preview(conn: Connection, actor: Actor, batch_id: int, lock: bool = False) -> UndoPreview:
    require(actor, "undo")
    batch = get_batch(conn, actor, batch_id)
    if batch["status"] == "undone":
        raise ValidationFailed({"batch": "This import has already been undone."})
    j = conn.execute(select(T.import_journal).where(T.import_journal.c.batch_id == batch_id,
                                                    T.import_journal.c.op == "insert")).all()
    ids = [x.record_id for x in j]
    expected = {x.record_id: x.after_version for x in j}
    q = select(T.transactions).where(T.transactions.c.id.in_(ids), T.transactions.c.workspace_id == actor.workspace_id)
    if lock:
        q = q.with_for_update()
    current = {r.id: dict(r._mapping) for r in conn.execute(q)} if ids else {}
    unchanged = [current[i] for i in ids if i in current and current[i]["version"] == expected[i]]
    edited = [current[i] for i in ids if i in current and current[i]["version"] != expected[i]]
    missing = [i for i in ids if i not in current]
    return UndoPreview(batch, unchanged, edited, missing)


def undo_import(db: Database, actor: Actor, batch_id: int, mode: str) -> dict:
    """mode = 'full' (remove everything, including edited rows - an explicit
    choice) or 'keep_edited' (a *partial* reversal, labelled as such).
    Versions are re-checked under row locks inside the same transaction."""
    require(actor, "undo")
    if mode not in ("full", "keep_edited"):
        raise ValidationFailed({"mode": "Choose how to undo."})
    with db.write() as conn:
        p = undo_preview(conn, actor, batch_id, lock=True)
        targets = p.unchanged + (p.edited if mode == "full" else [])
        ts = now()
        for row in targets:
            conn.execute(insert(T.import_journal).values(
                batch_id=batch_id, table_name="transactions", record_id=row["id"], op="delete",
                before_json=json.dumps(row, default=str, ensure_ascii=False), after_version=row["version"], at=ts))
        if targets:
            conn.execute(delete(T.transactions).where(T.transactions.c.id.in_([r["id"] for r in targets])))
        partial = mode == "keep_edited" and bool(p.edited)
        status = "partial" if partial else "undone"
        conn.execute(update(T.import_batches).where(T.import_batches.c.id == batch_id)
                     .values(status=status, undone_at=ts))
        audit(conn, actor, "import.undo",
              f"{'Partial' if partial else 'Full'} undo of {p.batch['file_name']}: removed {len(targets)}"
              + (f", kept {len(p.edited)} edited" if partial else ""))
    return {"removed": len(targets), "kept": len(p.edited) if partial else 0, "status": status,
            "already_gone": len(p.missing)}


def redo_import(db: Database, actor: Actor, batch_id: int) -> int:
    """Put back rows removed by an undo, exactly as they were."""
    require(actor, "undo")
    with db.write() as conn:
        batch = get_batch(conn, actor, batch_id)
        if batch["status"] not in ("undone", "partial"):
            raise ValidationFailed({"batch": "Only an undone import can be restored."})
        last_undo = conn.execute(select(func.max(T.import_journal.c.at)).where(
            T.import_journal.c.batch_id == batch_id, T.import_journal.c.op == "delete")).scalar()
        entries = conn.execute(select(T.import_journal).where(
            T.import_journal.c.batch_id == batch_id, T.import_journal.c.op == "delete",
            T.import_journal.c.at == last_undo)).all()
        restored = 0
        for e in entries:
            row = json.loads(e.before_json)
            if conn.execute(select(T.transactions.c.id).where(T.transactions.c.id == row["id"])).first():
                continue
            for k in ("posted_on", "occurrence_date"):
                if row.get(k):
                    row[k] = date.fromisoformat(row[k])
            for k in ("created_at", "updated_at"):
                row[k] = datetime.fromisoformat(row[k])
            conn.execute(insert(T.transactions).values(**row))
            conn.execute(insert(T.import_journal).values(batch_id=batch_id, table_name="transactions",
                                                         record_id=row["id"], op="restore",
                                                         after_version=row["version"], at=now()))
            restored += 1
        conn.execute(update(T.import_batches).where(T.import_batches.c.id == batch_id)
                     .values(status="committed", undone_at=None))
        audit(conn, actor, "import.redo", f"Restored {restored} transactions from {batch['file_name']}")
    return restored


# ----------------------------------------------------------------- export --

def export_transactions_csv(conn: Connection, actor: Actor, start: date | None, end: date | None) -> str:
    import csv
    import io
    require(actor, "view_details")
    t = T.transactions
    cond = [t.c.workspace_id == actor.workspace_id]
    if start:
        cond.append(t.c.posted_on >= start)
    if end:
        cond.append(t.c.posted_on <= end)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "payee", "description", "category", "kind", "amount_pkr", "source"])
    for r in conn.execute(select(t).where(and_(*cond)).order_by(t.c.posted_on)):
        w.writerow([r.posted_on.isoformat(), csv_safe(r.payee), csv_safe(r.description),
                    CATEGORIES[r.category_id].name, r.kind, format_rupees_plain(r.amount),
                    f"import #{r.import_batch_id}" if r.import_batch_id else "manual/seed"])
    with_audit = out.getvalue()
    return with_audit


def export_summary_csv(rows: list[dict]) -> str:
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["month", "income_pkr", "spending_pkr", "net_pkr", "partial_month"])
    for r in rows:
        w.writerow([r["label"], format_rupees_plain(r["income"]), format_rupees_plain(r["spending"]),
                    format_rupees_plain(r["net"]), "yes" if r["partial"] else "no"])
    return out.getvalue()


def log_export(db: Database, actor: Actor, what: str) -> None:
    with db.engine.begin() as conn:
        audit(conn, actor, "export", what)


# ------------------------------------------------------------ seed / reset --

def seed_database(db: Database, as_of: date, data: SeedData | None = None) -> None:
    """Create tables and load the synthetic demo data.  DDL runs here, never
    inside a business transaction (MySQL commits DDL implicitly)."""
    db.create_all()
    data = data or generate(as_of)
    ts = now()
    with db.engine.begin() as conn:
        conn.execute(insert(T.categories), [
            {"id": c.id, "name": c.name, "name_ur": c.name_ur, "grp": c.group} for c in CATEGORIES.values()])
        conn.execute(insert(T.profiles), PROFILES)
        conn.execute(insert(T.workspaces), [{**w, "currency": "PKR", "created_at": ts} for w in data.workspaces])
        conn.execute(insert(T.memberships), data.memberships)
        conn.execute(insert(T.sharing_grants), [{**g, "granted_at": ts} for g in data.grants])
        conn.execute(insert(T.rules), [
            {"workspace_id": w["id"], "pattern": p, "category_id": c, "priority": 0, "source": "system",
             "created_at": ts}
            for w in data.workspaces for p, c in SYSTEM_RULES])
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
        conn.execute(insert(T.meta), [{"key": "seeded", "value": as_of.isoformat()},
                                      {"key": "simulate_storage_failure", "value": "0"}])


def reset_database(db: Database, as_of: date) -> None:
    db.drop_all()
    seed_database(db, as_of)


def clear_workspace(db: Database, actor: Actor) -> None:
    """Remove every record from one workspace (to demonstrate empty states)."""
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
