"""Help: how the numbers are worked out, and the limits of this prototype."""
from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("help", __name__)


@bp.get("/help")
def index():
    return render_template("pages/help/index.html")
