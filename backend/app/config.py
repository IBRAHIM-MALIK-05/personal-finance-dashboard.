"""Paths and settings.  Nothing here touches Flask or the database."""
from __future__ import annotations

import secrets
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent      # backend/
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"              # frontend/
TEMPLATE_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
INSTANCE_DIR = BACKEND_DIR / "instance"                     # local runtime data (git-ignored)

MAX_UPLOAD_BYTES = 2_500_000
FORECAST_HORIZONS = (30, 60, 90)


def secret_key(instance: Path = INSTANCE_DIR) -> str:
    """A random key created on first run and kept in ``instance/secret_key``."""
    instance.mkdir(exist_ok=True)
    f = instance / "secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()
