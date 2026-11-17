"""Cash-flow forecast with its assumptions, likely range and table view."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, g, render_template, request

from ..charts.cashflow import cashflow_svg, chart_points_json
from ..context import get_db, today
from ..engine.money import AmountError, parse_amount
from ..view_model import build_view
from .helpers import horizon

bp = Blueprint("forecast", __name__)


def _as_of() -> date:
    raw = request.args.get("as_of", "")
    if raw:
        try:
            chosen = date.fromisoformat(raw)
            if chosen <= today():
                return chosen
        except ValueError:
            flash("That ‘as of’ date was not valid, so today is used.", "error")
    return today()


def _spending_override() -> dict[str, int] | None:
    monthly = request.args.get("monthly_spending", "")
    if not monthly:
        return None
    try:
        value = parse_amount(monthly)
        return {"__total__": value} if value > 0 else None
    except AmountError as e:
        flash(str(e), "error")
        return None


@bp.get("/forecast")
def index():
    as_of, overrides, days = _as_of(), _spending_override(), horizon()
    with get_db().read() as conn:
        v = build_view(conn, g.actor, as_of, days, overrides=overrides)
    chart = cashflow_svg(v.fc, chart_id="fc") if v.snap.txns else ""
    return render_template("pages/forecast.html", v=v, chart=chart, horizon=days,
                           chart_data=chart_points_json(v.fc) if chart else "{}",
                           as_of=as_of, overrides=overrides)
