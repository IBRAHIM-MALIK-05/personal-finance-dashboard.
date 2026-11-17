"""System pages: shown when the database cannot be reached at start-up."""
from __future__ import annotations

from flask import Blueprint, current_app, redirect, render_template, url_for

bp = Blueprint("system", __name__)


@bp.get("/db-problem")
def database_unavailable():
    error = current_app.extensions["db_error"]
    if not error:
        return redirect(url_for("dashboard.index"))
    return render_template("pages/errors/database_unavailable.html", error=error), 503
