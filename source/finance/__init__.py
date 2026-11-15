"""Personal Finance & Budget Dashboard - Flask application factory."""
from __future__ import annotations

import secrets
from datetime import date
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.exceptions import RequestEntityTooLarge

from . import services as S
from .db import Database, StorageError, database_url
from .engine.categories import CATEGORIES, name_of
from .engine.dates import human
from .engine.explain import KIND_LABELS
from .engine.money import format_pkr
from .i18n import translate

SOURCE_DIR = Path(__file__).resolve().parent.parent


def _secret_key(instance: Path) -> str:
    instance.mkdir(exist_ok=True)
    f = instance / "secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


def create_app(config: dict | None = None) -> Flask:
    config = config or {}
    instance = SOURCE_DIR / "instance"
    app = Flask(__name__, instance_path=str(instance))
    url, desc = (config["DATABASE_URL"], config.get("DB_DESCRIPTION", "test")) if "DATABASE_URL" in config \
        else database_url(SOURCE_DIR)
    app.config.update(
        SECRET_KEY=config.get("SECRET_KEY") or _secret_key(instance),
        MAX_CONTENT_LENGTH=2_500_000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        AS_OF=config.get("AS_OF"),
        TESTING=config.get("TESTING", False),
        WTF_CSRF_DISABLED=config.get("CSRF_DISABLED", False),
    )
    db = Database(url, desc)
    app.extensions["db"] = db
    app.extensions["db_error"] = None
    try:
        if not db.is_initialised():
            S.seed_database(db, app.config["AS_OF"] or date.today())
    except Exception as exc:  # the app still starts and explains the problem
        app.extensions["db_error"] = f"{type(exc).__name__}: {exc}"

    _register_template_helpers(app)
    _register_request_hooks(app)
    _register_errors(app)

    from .web import imports, views
    app.register_blueprint(views.bp)
    app.register_blueprint(imports.bp)
    return app


def today() -> date:
    from flask import current_app
    return current_app.config.get("AS_OF") or date.today()


def get_db() -> Database:
    from flask import current_app
    return current_app.extensions["db"]


# ----------------------------------------------------------------- hooks --

PUBLIC_ENDPOINTS = {"views.profiles", "views.choose_profile", "static", "views.db_problem"}


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def load_context():
        if app.extensions["db_error"] and request.endpoint not in ("views.db_problem", "static"):
            return redirect(url_for("views.db_problem"))
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        if request.method == "POST" and not app.config["WTF_CSRF_DISABLED"]:
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session["csrf"]):
                abort(400, description="This form expired or came from another site. Go back, reload the page and try again.")
        g.actor = None
        g.prefs = {"lens": "standard", "theme": "system", "language": "en", "reduced_motion": "system", "horizon": 30}
        if request.endpoint in ("views.db_problem", "static"):
            return None
        pid = session.get("profile_id")
        if pid:
            with get_db().read() as conn:
                g.actor = S.make_actor(conn, pid, session.get("workspace_id"))
                if g.actor:
                    g.prefs = S.get_prefs(conn, pid, g.actor.role)
                    g.workspaces = S.accessible_workspaces(conn, pid)
                    g.storage_off = S.get_meta(conn, "simulate_storage_failure") == "1"
        if g.actor is None and request.endpoint not in PUBLIC_ENDPOINTS:
            if pid:  # e.g. a coach whose only access was revoked
                session.pop("workspace_id", None)
                return redirect(url_for("views.profiles", reason="no_access"))
            return redirect(url_for("views.profiles"))
        return None


def _register_template_helpers(app: Flask) -> None:
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    @app.template_filter("pkr")
    def pkr(v, signed=False):
        return format_pkr(int(v), signed=signed)

    @app.template_filter("human")
    def human_date(d):
        return human(d) if d else "—"

    @app.template_filter("cat")
    def cat(cid):
        return name_of(cid, g.prefs.get("language", "en") if g.get("prefs") else "en")

    @app.context_processor
    def inject():
        lang = g.prefs["language"] if g.get("prefs") else "en"
        return {
            "actor": g.get("actor"),
            "prefs": g.get("prefs"),
            "workspaces": g.get("workspaces", []),
            "storage_off": g.get("storage_off", False),
            "t": lambda key: translate(key, lang),
            "csrf_field": lambda: Markup(f'<input type="hidden" name="_csrf" value="{session.get("csrf", "")}">'),
            "csrf_token": session.get("csrf", ""),
            "CATEGORIES": CATEGORIES,
            "KIND_LABELS": KIND_LABELS,
            "db_description": app.extensions["db"].description,
            "today": today(),
        }


def _register_errors(app: Flask) -> None:
    def page(code, title, message, actions=None):
        return render_template("error.html", code=code, title=title, message=message,
                               actions=actions or []), code

    @app.errorhandler(S.Forbidden)
    def forbidden(e):
        return page(403, "You can’t do that here", str(e), [("Back to dashboard", url_for("views.dashboard"))])

    @app.errorhandler(S.NotFound)
    def not_found(e):
        return page(404, "Not found", str(e), [("Back to dashboard", url_for("views.dashboard"))])

    @app.errorhandler(404)
    def http_404(e):
        return page(404, "Page not found", "That address doesn’t match any page. Check the link or use the menu.",
                    [("Go to dashboard", url_for("views.dashboard"))])

    @app.errorhandler(400)
    def http_400(e):
        return page(400, "Something about that request wasn’t right", e.description,
                    [("Back to dashboard", url_for("views.dashboard"))])

    @app.errorhandler(StorageError)
    def storage(e):
        return page(503, "Your change was not saved", f"{e} Nothing was changed: your data is as it was before. "
                    "You can try again in a moment.",
                    [("Try again", request.referrer or url_for("views.dashboard")),
                     ("Storage settings", url_for("views.settings") + "#storage")])

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(e):
        return page(413, "That file is too large", "Files up to 2 MB can be imported. Export a shorter date range "
                    "from your bank and try again.", [("Back to import", url_for("imports.start"))])

    @app.errorhandler(S.EditConflict)
    def conflict(e):
        return render_template("conflict.html", e=e), 409
