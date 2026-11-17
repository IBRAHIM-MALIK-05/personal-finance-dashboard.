"""Budgets: monthly limits per category, with progress and month-end forecast."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..charts.bars import budget_bar_svg
from ..context import get_db, today
from ..engine.categories import BUDGETABLE_IDS, CATEGORIES
from ..view_models.overview import build_view

bp = Blueprint("budgets", __name__)


@bp.get("/budgets")
def index():
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    used = {s.budget.category_id for s in v.statuses}
    notes: dict[str, list[dict]] = {}
    for n in v.snap.notes:
        notes.setdefault(n["target"], []).append(n)
    return render_template("pages/budgets/index.html", v=v, notes=notes,
                           bars={s.budget.category_id: budget_bar_svg(s, f"b-{s.budget.category_id}")
                                 for s in v.statuses},
                           unbudgeted=[c for c in BUDGETABLE_IDS if c not in used],
                           edit_cat=request.args.get("edit"))


@bp.post("/budgets")
def save():
    category = request.form.get("category_id", "")
    try:
        S.set_budget(get_db(), g.actor, category, request.form.get("monthly_limit", ""),
                     request.form.get("version", type=int))
        name = CATEGORIES[category].name if category in CATEGORIES else "Budget"
        flash(f"{name} budget saved.", "success")
    except S.ValidationFailed as e:
        for message in e.errors.values():
            flash(message, "error")
        return redirect(url_for("budgets.index", edit=category) + f"#b-{category}")
    return redirect(url_for("budgets.index") + f"#b-{category}")


@bp.post("/budgets/<cat>/delete")
def remove(cat: str):
    S.delete_budget(get_db(), g.actor, cat)
    flash("Budget removed. Your transactions are unchanged.", "success")
    return redirect(url_for("budgets.index"))
