"""CSV exports (formula-injection safe) and their audit entries."""
from __future__ import annotations

import csv
import io
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..engine.categories import CATEGORIES
from ..engine.money import format_rupees_plain
from ..engine.validation import csv_safe
from .access import Actor, require
from .activity import audit


def export_transactions_csv(conn: Connection, actor: Actor, start: date | None, end: date | None) -> str:
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

