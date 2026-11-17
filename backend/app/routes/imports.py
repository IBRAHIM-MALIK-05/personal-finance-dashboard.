"""CSV import wizard: upload -> check columns -> resolve issues -> import, then undo / redo."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db, today
from ..database.seed import messy_csv
from ..engine import importer
from ..engine.dates import DateParseError, parse_flexible

bp = Blueprint("imports", __name__)

FILTERS = {
    "all": ("All rows", lambda r: True),
    "error": ("Needs fixing", lambda r: r.status == "error"),
    "needs_decision": ("Needs a decision", lambda r: r.status == "needs_decision"),
    "duplicates": ("Duplicates", lambda r: any(i.code.startswith(("DUPLICATE", "POSSIBLE_DUP")) for i in r.issues)),
    "dates": ("Dates to check", lambda r: any(i.code.startswith("DATE") for i in r.issues)),
    "refunds": ("Refunds", lambda r: r.kind == "refund" or r.has("REFUND_SIGN")),
    "ready": ("Ready", lambda r: r.status == "ready"),
    "skipped": ("Skipped", lambda r: r.status == "skipped"),
}


@bp.get("/import")
def start():
    S.require(g.actor, "import")
    with get_db().read() as conn:
        batches = S.list_batches(conn, g.actor)
    return render_template("pages/imports/start.html", batches=batches)


@bp.post("/import")
def upload():
    S.require(g.actor, "import")
    if request.form.get("sample") == "1":
        content = sample_bytes()
        name = "sample_bank_export_messy.csv"
    else:
        f = request.files.get("file")
        if f is None or not f.filename:
            flash("Choose a CSV file first, or use the sample file.", "error")
            return redirect(url_for("imports.start"))
        if not f.filename.lower().endswith((".csv", ".txt")):
            flash("That file isn’t a CSV. Export your statement as CSV (comma-separated) and try again.", "error")
            return redirect(url_for("imports.start"))
        content, name = f.read(), f.filename
    try:
        draft_id = S.create_draft(get_db(), g.actor, name, content)
    except importer.ImportFatal as e:
        return render_template("pages/imports/start.html", batches=[], fatal=str(e), file_name=name), 422
    return redirect(url_for("imports.mapping", draft_id=draft_id))


def sample_bytes() -> bytes:
    with get_db().read() as conn:
        snap = S.load_snapshot(conn, g.actor)
    existing = [{"posted_on": t.posted_on, "amount": t.amount, "payee": t.payee, "kind": t.kind} for t in snap.txns]
    return messy_csv(existing, today()).encode("utf-8")


@bp.get("/import/sample.csv")
def sample_download():
    S.require(g.actor, "import")
    return Response(sample_bytes(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_bank_export_messy.csv"})


def _load(draft_id: int):
    with get_db().read() as conn:
        draft = S.get_draft(conn, g.actor, draft_id)
        analysis = S.analyse_draft(conn, g.actor, draft, today())
    return draft, importer.Settings.from_json(draft["settings_json"]), analysis


@bp.route("/import/<int:draft_id>/map", methods=["GET", "POST"])
def mapping(draft_id: int):
    S.require(g.actor, "import")
    draft, settings, analysis = _load(draft_id)
    if request.method == "POST":
        m = {}
        for f in importer.FIELDS:
            v = request.form.get(f"col_{f}", "")
            if v.isdigit():
                m[f] = int(v)
        settings.mapping = m
        sign = request.form.get("sign") or None
        settings.sign = sign if sign in ("negative_is_out", "positive_is_out") else None
        order = request.form.get("date_order") or None
        settings.date_order = order if order in ("DMY", "MDY") else None
        S.save_draft_settings(get_db(), g.actor, draft_id, settings)
        _, _, analysis = _load(draft_id)
        if analysis.missing or (analysis.sign is None):
            flash("Some required choices are still missing. See the highlighted items.", "error")
            return redirect(url_for("imports.mapping", draft_id=draft_id))
        return redirect(url_for("imports.review", draft_id=draft_id))
    preview = []
    if not analysis.missing and analysis.sign:
        preview = analysis.rows[:6]
    return render_template("pages/imports/map_columns.html", draft=draft, settings=settings, a=analysis,
                           fields=importer.FIELDS, labels=importer.FIELD_LABELS, preview=preview,
                           sample_rows=analysis.rows[:5])


@bp.get("/import/<int:draft_id>/review")
def review(draft_id: int):
    S.require(g.actor, "import")
    draft, settings, a = _load(draft_id)
    if a.missing or a.sign is None:
        return redirect(url_for("imports.mapping", draft_id=draft_id))
    key = request.args.get("filter", "")
    if key not in FILTERS:
        key = "error" if a.counts["error"] else ("needs_decision" if a.counts["needs_decision"] else "all")
    rows = [r for r in a.rows if FILTERS[key][1](r)]
    counts = {k: sum(1 for r in a.rows if fn(r)) for k, (_, fn) in FILTERS.items()}
    return render_template("pages/imports/review.html", draft=draft, a=a, rows=rows, filters=FILTERS,
                           current=key, counts=counts, settings=settings)


@bp.post("/import/<int:draft_id>/row/<int:row_no>")
def decide_row(draft_id: int, row_no: int):
    S.require(g.actor, "import")
    draft, settings, _ = _load(draft_id)
    f = request.form
    action = f.get("action")
    if action in ("import", "skip"):
        settings.decisions[row_no] = action
    elif action == "undecide":
        settings.decisions.pop(row_no, None)
    elif action == "date":
        iso = f.get("date_choice", "")
        try:
            date.fromisoformat(iso)
            settings.row_dates[row_no] = iso
        except ValueError:
            flash("Choose one of the dates.", "error")
    elif action == "fix":
        ov = {}
        for field in ("date", "description", "amount"):
            if f.get(field) is not None:
                ov[field] = f.get(field, "")
        if "date" in ov:
            try:
                p = parse_flexible(ov["date"], settings.date_order)
                if p.value:
                    ov["date"] = p.value.isoformat()
            except DateParseError:
                pass  # leave as typed; the re-analysis will show the error again
        settings.overrides[row_no] = ov
        settings.decisions.pop(row_no, None)
    S.save_draft_settings(get_db(), g.actor, draft_id, settings)
    return redirect(url_for("imports.review", draft_id=draft_id, filter=f.get("filter", "")) + f"#row-{row_no}")


@bp.post("/import/<int:draft_id>/bulk")
def bulk(draft_id: int):
    S.require(g.actor, "import")
    draft, settings, a = _load(draft_id)
    act = request.form.get("action", "")
    if act == "skip_errors":
        settings.decisions.update(importer.skip_errors(a))
        msg = "All rows with errors will be skipped. They are listed in the import summary."
    elif act == "skip_exact_duplicates":
        settings.decisions.update(importer.bulk_decide(a, "DUPLICATE_EXISTING", "skip"))
        msg = "Rows identical to existing transactions will be skipped."
    elif act == "keep_in_file_duplicates":
        settings.decisions.update(importer.bulk_decide(a, "DUPLICATE_IN_FILE", "import"))
        msg = "Repeated rows inside the file will be imported as separate transactions."
    elif act == "skip_in_file_duplicates":
        settings.decisions.update(importer.bulk_decide(a, "DUPLICATE_IN_FILE", "skip"))
        msg = "Repeated rows inside the file will be skipped."
    else:
        msg = "Nothing changed."
    S.save_draft_settings(get_db(), g.actor, draft_id, settings)
    flash(msg, "info")
    return redirect(url_for("imports.review", draft_id=draft_id))


@bp.post("/import/<int:draft_id>/commit")
def commit(draft_id: int):
    try:
        batch_id = S.commit_draft(get_db(), g.actor, draft_id, today())
    except S.ImportBlocked as e:
        for r in e.reasons:
            flash(r, "error")
        return redirect(url_for("imports.review", draft_id=draft_id))
    return redirect(url_for("imports.batch", batch_id=batch_id, new=1))


@bp.post("/import/<int:draft_id>/discard")
def discard(draft_id: int):
    S.discard_draft(get_db(), g.actor, draft_id)
    flash("Import cancelled. Nothing was imported.", "info")
    return redirect(url_for("imports.start"))


@bp.get("/import/batch/<int:batch_id>")
def batch(batch_id: int):
    S.require(g.actor, "view_details")
    with get_db().read() as conn:
        b = S.get_batch(conn, g.actor, batch_id)
    return render_template("pages/imports/result.html", b=b, new=request.args.get("new"))


@bp.get("/import/batch/<int:batch_id>/undo")
def undo_preview(batch_id: int):
    with get_db().read() as conn:
        p = S.undo_preview(conn, g.actor, batch_id)
    return render_template("pages/imports/undo.html", p=p)


@bp.post("/import/batch/<int:batch_id>/undo")
def undo(batch_id: int):
    res = S.undo_import(get_db(), g.actor, batch_id, request.form.get("mode", ""))
    if res["status"] == "partial":
        flash(f"Partial undo: {res['removed']} transactions removed; {res['kept']} that you edited were kept.", "success")
    else:
        flash(f"Import undone: {res['removed']} transactions removed. You can restore them from this page.", "success")
    return redirect(url_for("imports.batch", batch_id=batch_id))


@bp.post("/import/batch/<int:batch_id>/redo")
def redo(batch_id: int):
    n = S.redo_import(get_db(), g.actor, batch_id)
    flash(f"Import restored: {n} transactions are back.", "success")
    return redirect(url_for("imports.batch", batch_id=batch_id))
