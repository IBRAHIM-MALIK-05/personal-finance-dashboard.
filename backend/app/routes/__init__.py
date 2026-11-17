"""HTTP routes: one blueprint per screen.  Each one renders ``pages/<screen>/...``."""
from __future__ import annotations

from flask import Flask

from . import (activity, auth, bills, budgets, coach, dashboard, exports, forecast, goals, help, imports, profiles,
               settings, system, transactions, whatif)

BLUEPRINTS = (auth, profiles, dashboard, transactions, budgets, bills, goals, forecast, whatif, imports, exports,
              activity, settings, coach, help, system)


def register_blueprints(app: Flask) -> None:
    for module in BLUEPRINTS:
        app.register_blueprint(module.bp)
