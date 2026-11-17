"""Coach screens: the client list, and notes a coach leaves on a shared budget."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db
from .helpers import first_error, safe_next

bp = Blueprint("coach", __name__)


@bp.get("/coach")
def index():
    if g.actor.role != "coach":
        return redirect(url_for("dashboard.index"))
    with get_db().read() as conn:
        clients = S.coach_clients(conn, g.actor.profile_id)
    return render_template("pages/coach.html", clients=clients)


@bp.post("/notes")
def add_note():
    try:
        S.add_note(get_db(), g.actor, request.form.get("target", "general"), request.form.get("text", ""))
        flash("Note added. The household will see it on their Budgets page.", "success")
    except S.ValidationFailed as e:
        flash(first_error(e), "error")
    return redirect(safe_next(request.form.get("next"), url_for("budgets.index")))
