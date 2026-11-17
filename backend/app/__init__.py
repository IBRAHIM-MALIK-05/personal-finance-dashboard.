"""Personal Finance & Budget Dashboard: Flask application factory.

Backend layout (``backend/app``):

    config.py            paths and settings
    context.py           per-request context: database, today, CSRF, current actor
    errors.py            error pages
    template_helpers.py  Jinja filters and globals
    database/            schema, connection, synthetic seed data
    engine/              pure calculations (no database, no Flask)
    charts/              SVG charts drawn by code
    services/            permission-checked reads and writes
    view_model.py        combines services + engine for a page
    routes/              one Flask blueprint per screen

The HTML templates, CSS and JavaScript live in ``frontend/``.
"""
from __future__ import annotations

from datetime import date

from flask import Flask

from . import config
from . import services as S
from .context import register_request_hooks, storage_failure_simulated
from .database import Database, database_url
from .database.migrations import migrate
from .errors import register_error_handlers
from .routes import register_blueprints
from .template_helpers import register_template_helpers


def create_app(overrides: dict | None = None) -> Flask:
    """Build the app.  ``overrides`` is used by the tests (database URL, fixed date...)."""
    overrides = overrides or {}
    app = Flask(__name__,
                template_folder=str(config.TEMPLATE_DIR), static_folder=str(config.STATIC_DIR))

    if "DATABASE_URL" in overrides:
        url, description = overrides["DATABASE_URL"], overrides.get("DB_DESCRIPTION", "test")
    else:
        url, description = database_url(config.BACKEND_DIR, config.DATABASE_DIR)

    app.config.update(
        SECRET_KEY=overrides.get("SECRET_KEY") or config.secret_key(),
        MAX_CONTENT_LENGTH=config.MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        AS_OF=overrides.get("AS_OF"),
        TESTING=overrides.get("TESTING", False),
        WTF_CSRF_DISABLED=overrides.get("CSRF_DISABLED", False),
    )

    db = Database(url, description)
    db.simulated_failure = storage_failure_simulated
    app.extensions["db"] = db
    app.extensions["db_error"] = None
    app.extensions["schema_changes"] = []
    try:
        if not db.is_initialised():
            S.seed_database(db, app.config["AS_OF"] or date.today())
        app.extensions["schema_changes"] = migrate(db)
    except Exception as exc:  # the app still starts and explains the problem on screen
        app.extensions["db_error"] = f"{type(exc).__name__}: {exc}"

    register_template_helpers(app)
    register_request_hooks(app)
    register_error_handlers(app)
    register_blueprints(app)
    return app
