"""Small helpers shared by several route modules."""
from __future__ import annotations

from datetime import date

from flask import g, request

from ..config import FORECAST_HORIZONS
from ..engine.dates import add_months


def safe_next(url: str | None, default: str) -> str:
    """Only follow same-site relative redirects (no open redirect)."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default


def horizon() -> int:
    """Forecast length in days, from ``?horizon=`` or the person's preference."""
    h = request.args.get("horizon", type=int) or g.prefs.get("horizon", 30)
    return h if h in FORECAST_HORIZONS else 30


def recent_months(as_of: date, n: int) -> list[str]:
    """The last ``n`` months as ``YYYY-MM``, newest first."""
    first = as_of.replace(day=1)
    return [f"{d.year:04d}-{d.month:02d}" for d in (add_months(first, -i) for i in range(n))]


def first_error(e) -> str:
    return next(iter(e.errors.values()))
