"""Per-profile display preferences and app-wide settings (the ``meta`` table)."""
from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T

DEFAULT_LENS = {"owner": "summary", "manager": "standard", "coach": "detailed"}


def get_prefs(conn: Connection, profile_id: str, role: str) -> dict:
    r = conn.execute(select(T.preferences).where(T.preferences.c.profile_id == profile_id)).first()
    if r:
        return dict(r._mapping)
    return {"profile_id": profile_id, "lens": DEFAULT_LENS[role], "theme": "system",
            "reduced_motion": "system", "horizon": 30}


def set_prefs(db: Database, profile_id: str, role: str, **changes) -> dict:
    allowed = {"lens": ("summary", "standard", "detailed"), "theme": ("system", "light", "dark", "contrast"),
               "reduced_motion": ("system", "on", "off"), "horizon": (30, 60, 90)}
    with db.engine.begin() as conn:  # preferences still save during the storage-failure demo
        prefs = get_prefs(conn, profile_id, role)
        for k, v in changes.items():
            if k == "horizon":
                v = int(v) if str(v).isdigit() else 30
            if k in allowed and v in allowed[k]:
                prefs[k] = v
        exists = conn.execute(select(T.preferences.c.profile_id).where(T.preferences.c.profile_id == profile_id)).first()
        values = {k: prefs[k] for k in ("lens", "theme", "reduced_motion", "horizon")}
        if exists:
            conn.execute(update(T.preferences).where(T.preferences.c.profile_id == profile_id).values(**values))
        else:
            conn.execute(insert(T.preferences).values(profile_id=profile_id, **values))
    return prefs


def get_meta(conn: Connection, key: str, default: str = "") -> str:
    v = conn.execute(select(T.meta.c.value).where(T.meta.c.key == key)).scalar()
    return default if v is None else v


def set_meta(db: Database, key: str, value: str) -> None:
    with db.engine.begin() as conn:  # bypasses the failure simulation on purpose
        if conn.execute(select(T.meta.c.key).where(T.meta.c.key == key)).first():
            conn.execute(update(T.meta).where(T.meta.c.key == key).values(value=value))
        else:
            conn.execute(insert(T.meta).values(key=key, value=value))

