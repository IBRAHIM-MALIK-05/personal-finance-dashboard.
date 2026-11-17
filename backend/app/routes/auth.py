"""Accounts: sign up, log in and log out."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from .. import services as S
from ..context import get_db, today

bp = Blueprint("auth", __name__)


def _start_session(profile_id: str) -> None:
    """A fresh session for the signed-in person (prevents session fixation)."""
    session.clear()
    with get_db().read() as conn:
        actor = S.make_actor(conn, profile_id, None)
    session["profile_id"] = profile_id
    session["workspace_id"] = actor.workspace_id if actor else None


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.get("actor") and not g.actor.is_demo:
        return redirect(url_for("dashboard.index"))
    errors, values = {}, {"opening_date": today().isoformat()}
    if request.method == "POST":
        try:
            profile_id = S.create_account(get_db(), request.form.to_dict(), today())
            _start_session(profile_id)
            flash("Welcome! Your budget is ready. Start by adding your transactions.", "success")
            return redirect(url_for("dashboard.index"))
        except S.ValidationFailed as e:
            errors = e.errors
            values = {k: v for k, v in request.form.items() if "password" not in k}
            values["opening_date"] = values.get("opening_date") or today().isoformat()
    return render_template("pages/auth/signup.html", errors=errors, values=values), (400 if errors else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("actor") and not g.actor.is_demo:
        return redirect(url_for("dashboard.index"))
    error, email = None, ""
    if request.method == "POST":
        email = request.form.get("email", "")
        with get_db().read() as conn:
            profile_id = S.authenticate(conn, email, request.form.get("password", ""))
        if profile_id:
            _start_session(profile_id)
            return redirect(url_for("dashboard.index"))
        error = "That email and password don’t match an account. Check them and try again."
    return render_template("pages/auth/login.html", error=error, email=email), (401 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have logged out.", "info")
    return redirect(url_for("auth.login"))
