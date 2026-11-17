"""Activity: the audit log of every change to this budget."""
from __future__ import annotations

from flask import Blueprint, g, render_template

from .. import services as S
from ..context import get_db

bp = Blueprint("activity", __name__)


@bp.get("/history")
def index():
    with get_db().read() as conn:
        entries = S.audit_entries(conn, g.actor)
    return render_template("pages/activity/index.html", entries=entries)
