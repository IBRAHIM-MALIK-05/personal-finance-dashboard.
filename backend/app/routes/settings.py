"""Settings: display, account and budget details, coach sharing (demo), storage test and data."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from .. import services as S
from ..context import STORAGE_SWITCH, get_db, today
from .helpers import safe_next

bp = Blueprint("settings", __name__)

PREFERENCE_FIELDS = ("lens", "theme", "reduced_motion", "horizon")
SHARING_MESSAGES = {
    "summary": "Coach Sara can now see summaries only (no transaction details).",
    "full": "Coach Sara can now see full details, including transactions.",
    None: "Sharing stopped. Coach Sara can no longer see this budget.",
}


def _render(errors: dict | None = None, values: dict | None = None, status: int = 200,
            delete_errors: dict | None = None):
    with get_db().read() as conn:
        grant = S.current_grant(conn, g.actor.workspace_id) if g.actor.can("manage_sharing") else None
        budget = S.budget_details(conn, g.actor)
    if values is None:
        values = {"name": budget["name"], "opening_balance": f"{budget['opening_balance'] / 100:g}",
                  "opening_date": budget["opening_date"].isoformat()}
    return render_template("pages/settings.html", grant=grant, budget=budget,
                           errors=errors or {}, values=values, delete_errors=delete_errors or {}), status


@bp.get("/settings")
def index():
    return _render()


@bp.post("/settings/budget")
def budget_details():
    try:
        S.update_budget_details(get_db(), g.actor, request.form.to_dict(), today())
    except S.ValidationFailed as e:
        return _render(e.errors, request.form.to_dict(), 400)
    flash("Budget details saved. The balance and forecast now use the new starting balance.", "success")
    return redirect(url_for("settings.index") + "#budget")


@bp.post("/prefs")
def save_preferences():
    changes = {k: request.form[k] for k in PREFERENCE_FIELDS if k in request.form}
    S.set_prefs(get_db(), g.actor.profile_id, g.actor.role, **changes)
    return redirect(safe_next(request.form.get("next"), url_for("dashboard.index")))


@bp.post("/settings/sharing")
def sharing():
    scope = request.form.get("scope") or None
    S.set_sharing(get_db(), g.actor, scope)
    flash(SHARING_MESSAGES[scope], "success")
    return redirect(url_for("settings.index") + "#sharing")


@bp.post("/settings/storage")
def storage():
    S.require(g.actor, "edit")
    on = request.form.get("simulate") == "1"
    session[STORAGE_SWITCH] = on  # only this browser is affected
    flash("Storage failure simulation is ON: saving will fail until you switch it off." if on
          else "Storage failure simulation is off. Saving works again.", "info")
    return redirect(url_for("settings.index") + "#storage")


@bp.post("/settings/reset")
def reset():
    S.require(g.actor, "edit")
    if not g.actor.is_demo:
        raise S.Forbidden("Only the demo budgets can be reset.")
    if request.form.get("confirm") != "RESET":
        flash("Type RESET to confirm. Nothing was changed.", "error")
        return redirect(url_for("settings.index") + "#data")
    S.reset_demo_data(get_db(), today())
    session.clear()
    flash("The demo budgets were reset to the original synthetic data. Real accounts were not touched.", "success")
    return redirect(url_for("profiles.index"))


@bp.post("/settings/account/delete")
def delete_account():
    try:
        S.delete_account(get_db(), g.actor, request.form.get("password", ""), request.form.get("confirm", ""))
    except S.ValidationFailed as e:
        return _render(delete_errors=e.errors, status=400)
    session.clear()
    flash("Your account was deleted. You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.post("/settings/clear")
def clear():
    if request.form.get("confirm") != "CLEAR":
        flash("Type CLEAR to confirm. Nothing was changed.", "error")
        return redirect(url_for("settings.index") + "#data")
    S.clear_workspace(get_db(), g.actor)
    flash("This budget is now empty." + (" Use Settings → Reset to restore the demo data." if g.actor.is_demo
                                          else ""), "success")
    return redirect(url_for("dashboard.index"))
