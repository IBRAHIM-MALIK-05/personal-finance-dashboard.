"""Jinja filters and the variables every template can use."""
from __future__ import annotations

from flask import Flask, current_app, g, session
from markupsafe import Markup

from .context import today
from .engine.categories import CATEGORIES, name_of
from .engine.dates import human
from .engine.explain import KIND_LABELS
from .engine.money import format_pkr


def _initials(name: str) -> str:
    parts = [p for p in name.replace("Coach ", "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def register_template_helpers(app: Flask) -> None:
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True

    @app.template_filter("pkr")
    def pkr(value, signed=False):
        return format_pkr(int(value), signed=signed)

    @app.template_filter("human")
    def human_date(d):
        return human(d) if d else "—"

    @app.template_filter("cat")
    def category_name(category_id):
        return name_of(category_id)

    @app.template_filter("initials")
    def initials(name):
        return _initials(name or "")

    @app.context_processor
    def inject_globals():
        return {
            "actor": g.get("actor"),
            "prefs": g.get("prefs"),
            "workspaces": g.get("workspaces", []),
            "storage_off": g.get("storage_off", False),
            "csrf_field": lambda: Markup(f'<input type="hidden" name="_csrf" value="{session.get("csrf", "")}">'),
            "csrf_token": session.get("csrf", ""),
            "CATEGORIES": CATEGORIES,
            "KIND_LABELS": KIND_LABELS,
            "db_description": current_app.extensions["db"].description,
            "today": today(),
        }
