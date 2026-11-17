"""Saved what-if scenarios (plans only: they never change real data)."""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Connection

from ..database import Database
from ..database import tables as T
from ..engine.models import Adjustment
from ..engine.validation import sanitize_text
from .access import Actor, require
from .activity import audit
from .common import now
from .errors import ValidationFailed


def list_scenarios(conn: Connection, actor: Actor) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(
        select(T.scenarios).where(T.scenarios.c.workspace_id == actor.workspace_id).order_by(T.scenarios.c.id))]


def adjustments_from_json(text: str) -> list[Adjustment]:
    out = []
    for a in json.loads(text or "[]"):
        on = a.get("on")
        out.append(Adjustment(
            kind=a["kind"], bill_id=a.get("bill_id"), category_id=a.get("category_id"),
            percent=int(a.get("percent", 0)), amount=int(a.get("amount", 0)),
            on=date.fromisoformat(on) if on else None, cadence=a.get("cadence", "monthly"),
            label=a.get("label", ""),
        ))
    return out


def adjustments_to_json(adjs: Iterable[Adjustment]) -> str:
    return json.dumps([
        {"kind": a.kind, "bill_id": a.bill_id, "category_id": a.category_id, "percent": a.percent,
         "amount": a.amount, "on": a.on.isoformat() if a.on else None, "cadence": a.cadence, "label": a.label}
        for a in adjs
    ])


def save_scenario(db: Database, actor: Actor, name: str, adjs: list[Adjustment]) -> None:
    require(actor, "edit")
    name = sanitize_text(name, 80).value or "Untitled scenario"
    if not adjs:
        raise ValidationFailed({"adjustments": "Add at least one change before saving."})
    with db.write() as conn:
        conn.execute(insert(T.scenarios).values(workspace_id=actor.workspace_id, name=name,
                                                adjustments_json=adjustments_to_json(adjs),
                                                created_by=actor.profile_id, created_at=now()))
        audit(conn, actor, "scenario.save", f"Saved what-if ‘{name}’ ({len(adjs)} changes)")


def delete_scenario(db: Database, actor: Actor, scenario_id: int) -> None:
    require(actor, "edit")
    with db.write() as conn:
        conn.execute(delete(T.scenarios).where(T.scenarios.c.id == scenario_id,
                                               T.scenarios.c.workspace_id == actor.workspace_id))
        audit(conn, actor, "scenario.delete", f"Deleted what-if #{scenario_id}")

