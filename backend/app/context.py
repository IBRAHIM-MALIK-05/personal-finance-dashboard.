"""Per-request context: the database, "today", CSRF protection and the current actor.

``load_request_context`` runs before every request.  It checks the CSRF token on
every POST, then loads the signed-in account (or demo persona) into ``g.actor``
with their preferences, or sends the visitor to the log-in page.
"""
from __future__ import annotations

import secrets
from datetime import date

from flask import Flask, abort, current_app, g, has_request_context, redirect, request, session, url_for

from . import services as S
from .database import Database

# Pages anyone can open without choosing a profile first.
PUBLIC_ENDPOINTS = {"auth.login", "auth.signup", "auth.logout", "profiles.index", "profiles.choose",
                    "system.database_unavailable", "help.index", "static"}
STORAGE_SWITCH = "simulate_storage_failure"  # session key: this browser's storage-failure test
DEFAULT_PREFS = {"lens": "standard", "theme": "system", "reduced_motion": "system", "horizon": 30}


def storage_failure_simulated() -> bool:
    """True when this browser session switched on the storage-failure test (Settings)."""
    return has_request_context() and bool(session.get(STORAGE_SWITCH))


def get_db() -> Database:
    return current_app.extensions["db"]


def today() -> date:
    """The app's notion of today (tests pin it with the ``AS_OF`` setting)."""
    return current_app.config.get("AS_OF") or date.today()


def _check_csrf(app: Flask) -> None:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    if request.method == "POST" and not app.config["WTF_CSRF_DISABLED"]:
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not sent or not secrets.compare_digest(sent, session["csrf"]):
            abort(400, description="This form expired or came from another site. "
                                   "Go back, reload the page and try again.")


def register_request_hooks(app: Flask) -> None:
    @app.before_request
    def load_request_context():
        if app.extensions["db_error"] and request.endpoint not in ("system.database_unavailable", "static"):
            return redirect(url_for("system.database_unavailable"))
        _check_csrf(app)
        g.actor = None
        g.prefs = dict(DEFAULT_PREFS)
        if request.endpoint in ("system.database_unavailable", "static"):
            return None

        profile_id = session.get("profile_id")
        if profile_id:
            with get_db().read() as conn:
                g.actor = S.make_actor(conn, profile_id, session.get("workspace_id"))
                if g.actor:
                    g.prefs = S.get_prefs(conn, profile_id, g.actor.role)
                    g.workspaces = S.accessible_workspaces(conn, profile_id)
                    g.storage_off = storage_failure_simulated() or S.get_meta(conn, STORAGE_SWITCH) == "1"

        if g.actor is None and profile_id:
            with get_db().read() as conn:
                demo = S.is_demo_profile(conn, profile_id)
            if not demo:  # the account was deleted (perhaps in another browser): sign out
                session.clear()
                if request.endpoint not in PUBLIC_ENDPOINTS:
                    return redirect(url_for("auth.login"))
        if g.actor is None and request.endpoint not in PUBLIC_ENDPOINTS:
            if profile_id:  # e.g. a coach whose only access was revoked
                session.pop("workspace_id", None)
                return redirect(url_for("profiles.index", reason="no_access"))
            return redirect(url_for("auth.login"))
        return None
