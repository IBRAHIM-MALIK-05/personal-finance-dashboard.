"""Try the demo: open one of the demo personas without an account."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .. import services as S
from ..context import get_db

bp = Blueprint("profiles", __name__)


@bp.get("/profiles")
def index():
    with get_db().read() as conn:
        profiles = S.list_demo_profiles(conn)
    return render_template("pages/profiles.html", profiles=profiles, reason=request.args.get("reason"),
                           role_labels=S.ROLE_LABELS)


@bp.post("/profiles")
def choose():
    profile_id = request.form.get("profile_id", "")
    with get_db().read() as conn:
        if not S.is_demo_profile(conn, profile_id):  # real accounts need their password
            flash("Choose one of the demo profiles, or log in to your own account.", "error")
            return redirect(url_for("profiles.index"))
        actor = S.make_actor(conn, profile_id, None)
    if actor is None:
        flash("That profile has no budget it can open yet. If it is a coach, a household must share first.", "error")
        return redirect(url_for("profiles.index"))
    session.clear()
    session["profile_id"], session["workspace_id"] = profile_id, actor.workspace_id
    return redirect(url_for("coach.index") if actor.role == "coach" else url_for("dashboard.index"))


@bp.post("/workspace")
def switch_workspace():
    session["workspace_id"] = request.form.get("workspace_id", type=int)
    return redirect(url_for("dashboard.index"))
