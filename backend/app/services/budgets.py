"""Monthly budget limits per spending category."""
from __future__ import annotations

from sqlalchemy import delete, insert, select

from ..database import Database
from ..database import tables as T
from ..engine.categories import CATEGORIES
from ..engine.money import format_pkr
from ..engine.validation import validate_money_field
from .access import Actor, require
from .activity import audit
from .common import versioned_update
from .errors import ValidationFailed


def set_budget(db: Database, actor: Actor, category_id: str, limit_text: str, version: int | None) -> None:
    require(actor, "edit")
    if category_id not in CATEGORIES or CATEGORIES[category_id].group != "expense":
        raise ValidationFailed({"category_id": "Choose a spending category."})
    limit, err = validate_money_field(limit_text, "Monthly limit")
    if err:
        raise ValidationFailed({"monthly_limit": err}, {"monthly_limit": limit_text, "category_id": category_id})
    with db.write() as conn:
        cur = conn.execute(select(T.budgets).where(
            T.budgets.c.workspace_id == actor.workspace_id, T.budgets.c.category_id == category_id)).first()
        if cur is None:
            conn.execute(insert(T.budgets).values(workspace_id=actor.workspace_id, category_id=category_id,
                                                  monthly_limit=limit, version=1))
            audit(conn, actor, "budget.add", f"{CATEGORIES[category_id].name} budget {format_pkr(limit)}")
        else:
            versioned_update(conn, T.budgets, cur.id, actor.workspace_id,
                              version if version is not None else cur.version,
                              {"monthly_limit": limit}, attempted={"monthly_limit": limit})
            audit(conn, actor, "budget.edit",
                  f"{CATEGORIES[category_id].name} budget {format_pkr(cur.monthly_limit)} → {format_pkr(limit)}")


def delete_budget(db: Database, actor: Actor, category_id: str) -> None:
    require(actor, "edit")
    if category_id not in CATEGORIES:
        raise ValidationFailed({"category_id": "That budget category does not exist."})
    with db.write() as conn:
        conn.execute(delete(T.budgets).where(T.budgets.c.workspace_id == actor.workspace_id,
                                             T.budgets.c.category_id == category_id))
        audit(conn, actor, "budget.delete", f"Removed {CATEGORIES[category_id].name} budget")

