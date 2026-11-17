"""Paths and settings.  Nothing here touches Flask or the database."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent      # backend/
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"              # frontend/
DATABASE_DIR = BACKEND_DIR.parent / "database"              # database/: SQL scripts (+ local SQLite file)
TEMPLATE_DIR = FRONTEND_DIR / "templates"
STATIC_DIR = FRONTEND_DIR / "static"
ENV_FILE = BACKEND_DIR / ".env"                             # private settings (git-ignored)

MAX_UPLOAD_BYTES = 2_500_000
FORECAST_HORIZONS = (30, 60, 90)


def secret_key(env_file: Path = ENV_FILE) -> str:
    """The key that signs log-in sessions: ``SECRET_KEY`` from ``.env``.
    On the very first run it is generated and added to ``.env``, so it survives restarts."""
    key = os.environ.get("SECRET_KEY", "").strip()
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("SECRET_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        key = secrets.token_hex(32)
        existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        separator = "" if not existing or existing.endswith("\n") else "\n"
        env_file.write_text(f"{existing}{separator}SECRET_KEY={key}\n", encoding="utf-8")
        os.environ["SECRET_KEY"] = key
    return key
