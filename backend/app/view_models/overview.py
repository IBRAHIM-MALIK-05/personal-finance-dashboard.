"""Overview view model: load one snapshot, run the engine once, and share the
results between the dashboard, budgets, bills, goals, forecast and what-if pages."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from flask import url_for
from sqlalchemy.engine import Connection

from .. import services as S
from ..engine import budgets as B
from ..engine import goals as G
from ..engine.forecast import Forecast, build_forecast, variable_assumptions
from ..engine.recurring import Occurrence, match_occurrences
from ..engine.money import format_pkr
from ..engine.dates import human


@dataclass
class View:
    snap: S.Snapshot
    as_of: date
    fc: Forecast
    statuses: list[B.BudgetStatus]
    free: object
    goals: list[G.GoalProjection]
    upcoming: list[Occurrence]
    suggested: list[Occurrence]
    variance: list[B.VarianceRow]
    monthly: list[dict]
    uncategorised: int
    attention: list[dict] = field(default_factory=list)


def build_view(conn: Connection, actor: S.Actor, as_of: date, horizon: int = 30,
               overrides: dict[str, int] | None = None) -> View:
    snap = S.load_snapshot(conn, actor)
    ws = snap.workspace
    txns = [t for t in snap.txns if t.posted_on <= as_of]
    assumptions = variable_assumptions(txns, snap.bills, as_of, overrides=overrides)
    fc = build_forecast(opening_balance=ws["opening_balance"], opening_date=ws["opening_date"],
                        txns=txns, bills=snap.bills, as_of=as_of, horizon_days=horizon,
                        assumptions=assumptions)
    statuses = [B.budget_status(b, txns, snap.bills, assumptions, as_of) for b in snap.budgets]
    statuses.sort(key=lambda s: ({"over": 0, "heading_over": 1, "watch": 2, "ok": 3}[s.status], s.category_name))
    goals = [G.project(g, as_of) for g in snap.goals]
    occ = match_occurrences(snap.bills, txns, as_of - timedelta(days=7), as_of + timedelta(days=30), as_of)
    upcoming = [o for o in occ if o.status in ("upcoming", "overdue", "suggested")]
    suggested = [o for o in occ if o.status == "suggested"]
    free = B.free_to_spend(fc.balance_now.value, txns, snap.bills, snap.goals, as_of)
    v = View(snap, as_of, fc, statuses, free, goals, upcoming, suggested,
             B.variance_table(txns, as_of), B.monthly_totals(txns, as_of),
             sum(1 for t in txns if t.category_id == "uncategorised"))
    v.attention = attention(v, actor)
    return v


def attention(v: View, actor: S.Actor) -> list[dict]:
    """Things worth a look, most important first.  ``level`` is warn or info."""
    def item(level: str, text: str, href: str) -> dict:
        return {"level": level, "text": text, "href": href}

    budgets = url_for("budgets.index")
    items = []
    for s in v.statuses:
        if s.status == "over":
            items.append(item("warn", f"{s.category_name} is over budget by {format_pkr(-s.remaining.value)}.",
                              f"{budgets}#b-{s.budget.category_id}"))
        elif s.status == "heading_over":
            items.append(item("warn", f"{s.category_name} is likely to go over budget this month.",
                              f"{budgets}#b-{s.budget.category_id}"))
    for o in [o for o in v.upcoming if o.status == "overdue"][:2]:
        items.append(item("warn", f"{o.bill.name} was due on {human(o.due)} and has not appeared yet.",
                          url_for("bills.index")))
    if v.suggested:
        items.append(item("info", f"{len(v.suggested)} payment(s) look like bills. "
                                  "Confirm them so the forecast is accurate.", url_for("bills.index") + "#suggested"))
    if v.uncategorised and actor.can("view_details"):
        items.append(item("info", f"{v.uncategorised} transaction(s) need a category.",
                          url_for("transactions.index", category="uncategorised")))
    if v.fc.limited:
        items.append(item("info", "The forecast is a limited estimate (little history). Check the spending figure.",
                          url_for("forecast.index")))
    for g in v.goals:
        if not g.on_track and not g.done:
            items.append(item("info", f"Goal ‘{g.goal.name}’ is behind plan.", url_for("goals.index")))
    return items
