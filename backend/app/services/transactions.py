"""Transactions: list, add, edit, delete and category corrections."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..engine.categories import CATEGORIES, categorise, normalise_payee
from ..engine.dates import days_in_month
from ..engine.money import format_pkr
from ..engine.validation import sanitize_text, validate_transaction
from .access import Actor, require
from .activity import audit
from .common import now, versioned_update
from .errors import EditConflict, NotFound, ValidationFailed
from .snapshot import load_rules


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

