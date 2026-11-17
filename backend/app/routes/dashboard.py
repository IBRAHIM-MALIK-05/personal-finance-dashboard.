"""Dashboard: the at-a-glance overview of one budget."""
from __future__ import annotations

from flask import Blueprint, g, render_template

from ..charts.bars import budget_bar_svg, variance_svg
from ..charts.cashflow import cashflow_svg, chart_points_json
from ..context import get_db, today
from ..view_model import build_view

bp = Blueprint("dashboard", __name__)


@bp.get("/")
def index():
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    has_data = bool(v.snap.txns)
    return render_template(
        "pages/dashboard.html", v=v,
        chart=cashflow_svg(v.fc, chart_id="dash", compact=True) if has_data else "",
        chart_data=chart_points_json(v.fc, compact=True) if has_data else "{}",
        bars={s.budget.category_id: budget_bar_svg(s, f"bb-{s.budget.category_id}") for s in v.statuses},
        variance_chart=variance_svg(v.variance[:8], "dash-var"),
    )
