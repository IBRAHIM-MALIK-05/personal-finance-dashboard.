"""CSV files: the import pipeline (draft -> analyse -> commit -> undo / redo, with a journal)
and formula-injection-safe exports.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database, tables as T
from ..engine import importer
from ..engine.categories import CATEGORIES
from ..engine.money import format_rupees_plain
from ..engine.validation import csv_safe, sanitize_text
from .core import Actor, ImportBlocked, NotFound, ValidationFailed, audit, now, require
from .ledger import load_rules, txn_from_row


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


def discard_draft(db: Database, actor: Actor, draft_id: int) -> None:
    require(actor, "import")
    with db.write() as conn:
        get_draft(conn, actor, draft_id)
        conn.execute(delete(T.import_drafts).where(T.import_drafts.c.id == draft_id))


def analyse_draft(conn: Connection, actor: Actor, draft: dict, today: date) -> importer.Analysis:
    existing = [txn_from_row(r) for r in conn.execute(select(T.transactions).where(
        T.transactions.c.workspace_id == actor.workspace_id))]
    return importer.analyse(draft["content"], importer.Settings.from_json(draft["settings_json"]),
                            existing, load_rules(conn, actor.workspace_id), today)


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

