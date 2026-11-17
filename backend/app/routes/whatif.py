"""What-if scenarios: try changes against the forecast without touching real data.

The draft being edited lives in the session; saved scenarios live in the database.
"""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from .. import services as S
from ..charts.cashflow import cashflow_svg, chart_points_json
from ..context import get_db, today
from ..engine import scenarios as SC
from ..engine.categories import BUDGETABLE_IDS, CATEGORIES
from ..engine.dates import DateParseError, parse_flexible
from ..engine.models import Adjustment
from ..engine.money import AmountError, parse_amount
from ..engine.validation import sanitize_text
from ..view_model import build_view
from .helpers import first_error, horizon

bp = Blueprint("whatif", __name__)

CADENCES = ("weekly", "monthly", "quarterly", "yearly")


# ----------------------------------------------------------- session draft --

def _draft_key() -> str:
    return f"whatif-{g.actor.workspace_id}"


def _draft() -> dict:
    return session.get(_draft_key()) or {"name": "", "adjustments": "[]"}


def _adjustments() -> list[Adjustment]:
    return S.adjustments_from_json(_draft()["adjustments"])


def _store(adjs: list[Adjustment], name: str | None = None) -> None:
    d = _draft()
    d["adjustments"] = S.adjustments_to_json(adjs)
    if name is not None:
        d["name"] = name
    session[_draft_key()] = d


# ------------------------------------------------------------------ pages --

@bp.get("/whatif")
def index():
    adjs, days = _adjustments(), horizon()
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), days)
        saved = S.list_scenarios(conn, g.actor)
    alt = delta = None
    if adjs:
        ws = v.snap.workspace
        alt = SC.run(opening_balance=ws["opening_balance"], opening_date=ws["opening_date"],
                     txns=v.snap.txns, bills=v.snap.bills, as_of=today(), horizon_days=days,
                     assumptions=v.fc.assumption_set, adjustments=adjs)
        delta = SC.compare(v.fc, alt, adjs, v.snap.bills)
    chart = cashflow_svg(v.fc, compare=alt, chart_id="wi", title="Forecast with and without your changes") \
        if v.snap.txns else ""
    bills = {b.id: b for b in v.snap.bills}
    return render_template("pages/whatif.html", v=v, alt=alt, delta=delta, chart=chart, saved=saved,
                           adjs=[(i, SC.describe(a, bills)) for i, a in enumerate(adjs)],
                           draft=_draft(), horizon=days, categories=list(BUDGETABLE_IDS),
                           chart_data=chart_points_json(v.fc, alt) if chart else "{}")


def _future_date(raw: str, message: str):
    on = parse_flexible(raw, "DMY").value
    if on is None or on <= today():
        raise ValueError(message)
    return on


def _signed(form, amount: int) -> int:
    return amount if form.get("direction") == "in" else -amount


def _adjustment_from(form) -> Adjustment:
    """Build one adjustment from a posted form, or raise ValueError with a friendly message."""
    kind = form.get("kind")
    if kind == "cancel_bill":
        return Adjustment("cancel_bill", bill_id=form.get("bill_id", type=int))
    if kind == "change_bill":
        return Adjustment("change_bill", bill_id=form.get("bill_id", type=int),
                          amount=abs(parse_amount(form.get("amount"))))
    if kind == "scale_category":
        pct = int(form.get("percent", "0"))
        if not -100 <= pct <= 300:
            raise ValueError("Use a change between -100% and +300%.")
        if form.get("category_id") not in CATEGORIES:
            raise ValueError("Choose a category.")
        return Adjustment("scale_category", category_id=form.get("category_id"), percent=pct)
    if kind == "one_off":
        amount = abs(parse_amount(form.get("amount")))
        on = _future_date(form.get("on", ""), "Choose a date after today.")
        return Adjustment("one_off", amount=_signed(form, amount), on=on,
                          label=sanitize_text(form.get("label", ""), 60).value or "One-off")
    if kind == "add_recurring":
        amount = abs(parse_amount(form.get("amount")))
        on = _future_date(form.get("on", ""), "Choose a start date after today.")
        cadence = form.get("cadence") if form.get("cadence") in CADENCES else "monthly"
        return Adjustment("add_recurring", amount=_signed(form, amount), on=on, cadence=cadence,
                          label=sanitize_text(form.get("label", ""), 60).value or "New item")
    raise ValueError("Choose what kind of change to make.")


@bp.post("/whatif/add")
def add():
    try:
        adjustment = _adjustment_from(request.form)
    except (ValueError, AmountError, DateParseError) as e:
        flash(str(e), "error")
        return redirect(url_for("whatif.index"))
    _store(_adjustments() + [adjustment])
    return redirect(url_for("whatif.index") + "#result")


@bp.post("/whatif/remove/<int:index>")
def remove(index: int):
    adjs = _adjustments()
    if 0 <= index < len(adjs):
        adjs.pop(index)
    _store(adjs)
    return redirect(url_for("whatif.index"))


@bp.post("/whatif/clear")
def clear():
    session.pop(_draft_key(), None)
    flash("What-if changes cleared.", "info")
    return redirect(url_for("whatif.index"))


@bp.post("/whatif/load/<int:scenario_id>")
def load(scenario_id: int):
    with get_db().read() as conn:
        s = next((x for x in S.list_scenarios(conn, g.actor) if x["id"] == scenario_id), None)
    if s is None:
        raise S.NotFound("That saved scenario no longer exists.")
    session[_draft_key()] = {"name": s["name"], "adjustments": s["adjustments_json"]}
    return redirect(url_for("whatif.index") + "#result")


@bp.post("/whatif/save")
def save():
    try:
        S.save_scenario(get_db(), g.actor, request.form.get("name", ""), _adjustments())
        flash("Scenario saved. It is only a plan; your transactions are unchanged.", "success")
    except S.ValidationFailed as e:
        flash(first_error(e), "error")
    return redirect(url_for("whatif.index"))


@bp.post("/whatif/delete/<int:scenario_id>")
def delete(scenario_id: int):
    S.delete_scenario(get_db(), g.actor, scenario_id)
    flash("Saved scenario deleted.", "success")
    return redirect(url_for("whatif.index"))
