"""Savings goals: progress, required monthly amount and contributions."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db, today
from ..engine.goals import status_sentence
from ..view_models.overview import build_view
from .helpers import first_error

bp = Blueprint("goals", __name__)


def _render(errors: dict | None = None, values: dict | None = None, status: int = 200):
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    return render_template("pages/goals/index.html", v=v, status_sentence=status_sentence,
                           errors=errors or {}, values=values or {}), status


@bp.get("/goals")
def index():
    return _render()


@bp.post("/goals/new")
def new():
    try:
        S.add_goal(get_db(), g.actor, request.form.to_dict(), today())
    except S.ValidationFailed as e:
        return _render(e.errors, request.form.to_dict(), 400)
    flash("Goal added.", "success")
    return redirect(url_for("goals.index"))


@bp.post("/goals/<int:goal_id>/contribute")
def contribute(goal_id: int):
    try:
        S.contribute(get_db(), g.actor, goal_id, request.form.to_dict(), today())
        transfer = request.form.get("record_transfer") == "1"
        flash("Contribution recorded." + (" A transfer to savings was added to your transactions." if transfer
                                          else " Your balance is unchanged (allocation only)."), "success")
    except S.ValidationFailed as e:
        flash(first_error(e), "error")
    return redirect(url_for("goals.index") + f"#g-{goal_id}")


@bp.post("/goals/<int:goal_id>/delete")
def delete(goal_id: int):
    S.delete_goal(get_db(), g.actor, goal_id)
    flash("Goal deleted.", "success")
    return redirect(url_for("goals.index"))
