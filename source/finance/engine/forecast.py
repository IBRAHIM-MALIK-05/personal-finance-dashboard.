"""Cash-flow forecasting engine (the project's technical hurdle).

Model, in one sentence: *balance today* (dated opening balance + every posted
transaction up to the cut-off) + *scheduled* recurring income and bills that
have not been posted yet + an explicit *variable-spending* assumption.

Uncertainty is shown honestly as three labelled assumptions taken from the
person's own history - not as a statistical confidence band:

    best  = spending like your lowest month of the last three
    base  = spending like your typical (median) month
    worst = spending like your highest month

With less than two full months of history the forecast is flagged *limited*
and the UI asks the person to confirm or type the monthly figure.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Literal

from .categories import group_of, name_of
from .dates import human, month_label, previous_full_months
from .explain import Explained, Input, money_input
from .models import RecurringBill, Txn
from .money import format_pkr
from .recurring import OVERDUE_GRACE_DAYS, match_occurrences, matched_txn_ids

DAYS_PER_MONTH = 365 / 12


def to_rupee(paisa: float) -> int:
    """Forecasts are estimates: show them to the nearest rupee, never paisa."""
    return int(round(paisa / 100.0)) * 100


# ------------------------------------------------------- variable spending --

@dataclass
class VariableAssumption:
    category_id: str
    low: int        # monthly spending, positive paisa
    base: int
    high: int
    months_used: int
    source: Literal["history", "partial", "user"] = "history"


@dataclass
class AssumptionSet:
    items: list[VariableAssumption]
    months_available: int
    period_labels: list[str]
    limited: bool

    def total(self, which: Literal["low", "base", "high"]) -> int:
        return sum(getattr(a, which) for a in self.items)

    def by_category(self) -> dict[str, VariableAssumption]:
        return {a.category_id: a for a in self.items}


def is_variable_spend(t: Txn) -> bool:
    return group_of(t.category_id) == "expense" and t.kind != "transfer"


def variable_assumptions(
    txns: list[Txn],
    bills: list[RecurringBill],
    as_of: date,
    months: int = 3,
    overrides: dict[str, int] | None = None,
) -> AssumptionSet:
    """Monthly variable spending per category from the last ``months`` full
    calendar months, excluding anything explained by a recurring bill."""
    periods = previous_full_months(as_of, months)
    posted = [t for t in txns if t.posted_on <= as_of]
    earliest = min((t.posted_on for t in posted), default=as_of)
    covered = [(s, e) for (s, e) in periods if earliest <= s + timedelta(days=3)]
    recurring_ids = matched_txn_ids(bills, posted, periods[-1][0], as_of)

    totals: dict[str, list[int]] = {}
    for idx, (s, e) in enumerate(covered):
        for t in posted:
            if s <= t.posted_on <= e and is_variable_spend(t) and id(t) not in recurring_ids:
                totals.setdefault(t.category_id, [0] * len(covered))[idx] += -t.amount

    items: list[VariableAssumption] = []
    if covered:
        for cat, values in totals.items():
            values = [max(v, 0) for v in values]
            items.append(
                VariableAssumption(
                    cat, min(values), int(statistics.median(values)), max(values), len(covered)
                )
            )
    else:
        # No complete month: scale the last 30 days (or fewer) to a month.
        window_start = max(earliest, as_of - timedelta(days=29))
        days = (as_of - window_start).days + 1
        partial: dict[str, int] = {}
        for t in posted:
            if t.posted_on >= window_start and is_variable_spend(t) and id(t) not in recurring_ids:
                partial[t.category_id] = partial.get(t.category_id, 0) - t.amount
        for cat, v in partial.items():
            monthly = max(0, round(v * DAYS_PER_MONTH / days))
            items.append(VariableAssumption(cat, monthly, monthly, monthly, 0, "partial"))

    if overrides and "__total__" in overrides:
        t = overrides["__total__"]
        items = [VariableAssumption("uncategorised", t, t, t, 0, "user")]
    elif overrides:
        by_cat = {a.category_id: a for a in items}
        for cat, monthly in overrides.items():
            by_cat[cat] = VariableAssumption(cat, monthly, monthly, monthly, 0, "user")
        items = list(by_cat.values())

    items.sort(key=lambda a: -a.base)
    return AssumptionSet(
        items=items,
        months_available=len(covered),
        period_labels=[month_label(s) for s, _ in reversed(covered)],
        limited=len(covered) < 2 and not overrides,
    )


# ---------------------------------------------------------------- forecast --

@dataclass
class Event:
    day: date
    label: str
    amount: int
    source: Literal["recurring", "overdue", "what-if"]
    bill_id: int | None = None


@dataclass
class Point:
    day: date
    base: int
    best: int
    worst: int
    events: list[Event] = field(default_factory=list)


@dataclass
class Forecast:
    as_of: date
    end: date
    horizon_days: int
    balance_now: Explained
    history: list[tuple[date, int]]
    points: list[Point]
    events: list[Event]
    closing: Explained
    closing_range: Explained
    lowest: Explained
    scheduled_in: Explained
    scheduled_out: Explained
    variable: Explained
    assumptions: list[str]
    assumption_set: AssumptionSet
    limited: bool

    @property
    def lowest_point(self) -> Point:
        return min(self.points, key=lambda p: p.base)


def balance_on(opening_balance: int, opening_date: date, txns: list[Txn], day: date) -> int:
    """Balance at the *end* of ``day``.  The opening balance is the balance at
    the start of ``opening_date``; earlier transactions are already inside it."""
    return opening_balance + sum(t.amount for t in txns if opening_date <= t.posted_on <= day)


def build_forecast(
    *,
    opening_balance: int,
    opening_date: date,
    txns: list[Txn],
    bills: list[RecurringBill],
    as_of: date,
    horizon_days: int = 30,
    assumptions: AssumptionSet | None = None,
    extra_events: list[Event] | None = None,
    history_days: int = 60,
) -> Forecast:
    posted = [t for t in txns if t.posted_on <= as_of]
    if assumptions is None:
        assumptions = variable_assumptions(posted, bills, as_of)
    end = as_of + timedelta(days=horizon_days)

    # -- balance today and recent history (actual) --
    now = balance_on(opening_balance, opening_date, posted, as_of)
    hist_start = max(opening_date, as_of - timedelta(days=history_days))
    daily: dict[date, int] = {}
    for t in posted:
        if t.posted_on >= opening_date:
            daily[t.posted_on] = daily.get(t.posted_on, 0) + t.amount
    running = balance_on(opening_balance, opening_date, posted, hist_start - timedelta(days=1))
    history: list[tuple[date, int]] = []
    d = hist_start
    while d <= as_of:
        running += daily.get(d, 0)
        history.append((d, running))
        d += timedelta(days=1)

    # -- scheduled events (forecast) --
    occ = match_occurrences(
        bills, posted, as_of - timedelta(days=OVERDUE_GRACE_DAYS), end, as_of
    )
    tomorrow = as_of + timedelta(days=1)
    events: list[Event] = []
    for o in occ:
        if o.status == "suggested":
            when = o.due if o.due > as_of else tomorrow
            events.append(Event(
                when, f"{o.bill.name} (may already be paid on {human(o.txn.posted_on)}. Confirm on Bills)",
                o.bill.amount, "recurring", o.bill.id,
            ))
        elif o.status == "upcoming" and o.due > as_of:
            events.append(Event(o.due, o.bill.name, o.bill.amount, "recurring", o.bill.id))
        elif o.status == "overdue":
            events.append(
                Event(tomorrow, f"{o.bill.name} (due {human(o.due)}, not seen yet)", o.bill.amount, "overdue", o.bill.id)
            )
    for e in extra_events or []:
        if as_of < e.day <= end:
            events.append(e)
    events.sort(key=lambda e: (e.day, e.label))
    by_day: dict[date, list[Event]] = {}
    for e in events:
        by_day.setdefault(e.day, []).append(e)

    # -- daily projection --
    rate = {w: assumptions.total(w) / DAYS_PER_MONTH for w in ("low", "base", "high")}
    points: list[Point] = []
    base = best = worst = float(now)
    for i in range(1, horizon_days + 1):
        day = as_of + timedelta(days=i)
        delta = sum(e.amount for e in by_day.get(day, []))
        base += delta - rate["base"]
        best += delta - rate["low"]
        worst += delta - rate["high"]
        points.append(Point(day, to_rupee(base), to_rupee(best), to_rupee(worst), by_day.get(day, [])))

    # -- explanations --
    sched_in = sum(e.amount for e in events if e.amount > 0)
    sched_out = sum(e.amount for e in events if e.amount < 0)
    var_base = to_rupee(rate["base"] * horizon_days)
    what_if = [e for e in events if e.source == "what-if"]
    period = ", ".join(assumptions.period_labels) or "recent days"

    assumption_text = [
        f"Typical variable spending is the median of your {assumptions.months_available or 'available'} "
        f"most recent full month(s) ({period}), per category: {format_pkr(assumptions.total('base'))} a month.",
        "Bills and income only count if they are set up as recurring. Irregular income is not assumed.",
        "Bills that vary (for example electricity) are assumed to cost their usual amount.",
        "A bill that was due in the last week but has not appeared is assumed to be paid tomorrow.",
        "A payment that only looks like a bill (not confirmed) does not cancel that bill in the forecast.",
        "Best/worst case use your lowest and highest spending month. They are not guarantees.",
    ]
    if assumptions.limited:
        assumption_text.insert(
            0,
            "LIMITED ESTIMATE: fewer than two full months of history. Check the monthly spending figure.",
        )
    if what_if:
        assumption_text.append("This forecast includes what-if changes that are not real transactions.")

    balance_now = Explained(
        "balance_now", "Balance today", now, "pkr", "actual",
        "Opening balance + all transactions up to today",
        (
            money_input(f"Opening balance on {human(opening_date)}", opening_balance),
            money_input(
                f"{sum(1 for t in posted if t.posted_on >= opening_date)} transactions since then",
                now - opening_balance,
            ),
        ),
    )
    inputs = [
        money_input("Balance today", now, "actual"),
        Input(f"Scheduled income ({sum(1 for e in events if e.amount > 0)} items)", format_pkr(sched_in, signed=True), "forecast"),
        Input(f"Scheduled bills ({sum(1 for e in events if e.amount < 0)} items)", format_pkr(sched_out), "forecast"),
        Input(
            f"Variable spending: {format_pkr(assumptions.total('base'))}/month × {horizon_days} days",
            format_pkr(-var_base),
            "forecast",
        ),
    ]
    if what_if:
        inputs.append(Input("What-if one-off changes", format_pkr(sum(e.amount for e in what_if), signed=True), "forecast"))
    last = points[-1] if points else Point(as_of, now, now, now)
    closing = Explained(
        "closing", f"Forecast balance on {human(end)}", last.base, "pkr", "forecast",
        "Balance today + scheduled income − scheduled bills − typical variable spending",
        tuple(inputs), tuple(assumption_text),
    )
    closing_range = Explained(
        "closing_range", "Likely range", f"{format_pkr(last.worst)} to {format_pkr(last.best)}", "text", "forecast",
        "Same calculation with your highest-spending month (worst) and lowest-spending month (best)",
        (
            Input("Worst case monthly variable spending", format_pkr(assumptions.total("high")), "forecast"),
            Input("Best case monthly variable spending", format_pkr(assumptions.total("low")), "forecast"),
        ),
        tuple(assumption_text),
    )
    low_pt = min(points, key=lambda p: p.base) if points else last
    lowest = Explained(
        "lowest", "Lowest forecast balance", low_pt.base, "pkr", "forecast",
        f"Smallest daily balance in the next {horizon_days} days (on {human(low_pt.day)})",
        tuple(Input(e.label, format_pkr(e.amount, signed=True), "forecast") for e in low_pt.events)
        or (Input("Driven by", "steady variable spending", "forecast"),),
        tuple(assumption_text),
    )
    scheduled_in = Explained(
        "scheduled_in", "Scheduled income", sched_in, "pkr", "forecast",
        "Sum of recurring income due before the end of the forecast",
        tuple(Input(f"{e.label} · {human(e.day)}", format_pkr(e.amount, signed=True), "forecast") for e in events if e.amount > 0),
    )
    scheduled_out = Explained(
        "scheduled_out", "Scheduled bills", -sched_out, "pkr", "forecast",
        "Sum of recurring bills due before the end of the forecast",
        tuple(Input(f"{e.label} · {human(e.day)}", format_pkr(e.amount), "forecast") for e in events if e.amount < 0),
    )
    variable = Explained(
        "variable", "Typical monthly variable spending", assumptions.total("base"), "pkr",
        "plan" if any(a.source == "user" for a in assumptions.items) else "forecast",
        "Sum over categories of the median monthly spending (not counting recurring bills)",
        tuple(
            Input(
                "Your monthly figure (all variable spending)" if a.source == "user" else name_of(a.category_id),
                f"{format_pkr(a.base)} (range {format_pkr(a.low)}–{format_pkr(a.high)})",
                "plan" if a.source == "user" else "actual",
            )
            for a in assumptions.items
        ),
        tuple(assumption_text[:1]),
    )
    return Forecast(
        as_of, end, horizon_days, balance_now, history, points, events, closing,
        closing_range, lowest, scheduled_in, scheduled_out, variable, assumption_text,
        assumptions, assumptions.limited,
    )


def scale_assumptions(a: AssumptionSet, category_id: str, percent: int) -> AssumptionSet:
    """Return a copy with one category's spending changed by ``percent``."""
    factor = max(0.0, 1 + percent / 100)
    items = [
        replace(x, low=round(x.low * factor), base=round(x.base * factor), high=round(x.high * factor))
        if x.category_id == category_id
        else x
        for x in a.items
    ]
    return replace(a, items=items)
