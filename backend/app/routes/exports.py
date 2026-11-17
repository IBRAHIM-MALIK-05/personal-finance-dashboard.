"""Export: download transactions or a monthly summary as CSV."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, Response, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db, today
from ..view_model import build_view

bp = Blueprint("exports", __name__)

UTF8_BOM = "﻿"  # byte-order mark: lets Excel open the CSV as UTF-8


def _date_range() -> tuple[date | None, date | None]:
    start = date.fromisoformat(request.form["start"]) if request.form.get("start") else None
    end = date.fromisoformat(request.form["end"]) if request.form.get("end") else None
    return start, end


@bp.get("/export")
def index():
    return render_template("pages/exports.html")


@bp.post("/export")
def download():
    try:
        start, end = _date_range()
    except ValueError:
        flash("Dates must be real dates.", "error")
        return redirect(url_for("exports.index"))
    if start and end and start > end:
        flash("The start date is after the end date.", "error")
        return redirect(url_for("exports.index"))

    stamp = today().isoformat()
    with get_db().read() as conn:
        if request.form.get("kind") == "summary":
            body = S.export_summary_csv(build_view(conn, g.actor, today(), 30).monthly)
            name = f"monthly_summary_{stamp}.csv"
        else:
            body = S.export_transactions_csv(conn, g.actor, start, end)
            name = f"transactions_{stamp}.csv"
    S.log_export(get_db(), g.actor, f"Exported {name}")
    return Response(UTF8_BOM + body, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})
