"""What-if scenarios.

A scenario is a list of adjustments applied to *copies* of the bills and
spending assumptions.  Nothing here touches the ledger, which is what lets a
person experiment safely ("what if I cancel the gym and eat out 30% less?").
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .categories import name_of
from .dates import human
from .explain import Explained, Input
from .forecast import AssumptionSet, Event, Forecast, build_forecast, scale_assumptions
from .models import Adjustment, RecurringBill, Txn
from .money import format_pkr


def describe(adj: Adjustment, bills: dict[int, RecurringBill]) -> str:
    bill = bills.get(adj.bill_id or -1)
    if adj.kind == "cancel_bill":
        return f"Cancel {bill.name if bill else 'a bill'}"
    if adj.kind == "change_bill":
        return f"Change {bill.name if bill else 'a bill'} to {format_pkr(adj.amount)}"
    if adj.kind == "scale_category":
        verb = "less" if adj.percent < 0 else "more"
        return f"Spend {abs(adj.percent)}% {verb} on {name_of(adj.category_id or '')}"
    if adj.kind == "one_off":
        return f"One-off {adj.label or 'item'} {format_pkr(adj.amount, signed=True)} on {human(adj.on)}"
    if adj.kind == "add_recurring":
        return f"New {adj.cadence} {adj.label or 'item'} {format_pkr(adj.amount, signed=True)} from {human(adj.on)}"
    return adj.kind


def apply(
    bills: list[RecurringBill],
    assumptions: AssumptionSet,
    adjustments: list[Adjustment],
    as_of: date,
) -> tuple[list[RecurringBill], AssumptionSet, list[Event]]:
    """Return modified copies of bills/assumptions plus one-off events."""
    out_bills = [replace(b) for b in bills]
    index = {b.id: b for b in out_bills}
    extra: list[Event] = []
    next_fake_id = -1
    for adj in adjustments:
        if adj.kind == "cancel_bill" and adj.bill_id in index:
            index[adj.bill_id].active = False
        elif adj.kind == "change_bill" and adj.bill_id in index:
            b = index[adj.bill_id]
            sign = -1 if b.amount < 0 else 1
            b.amount = sign * abs(adj.amount)
        elif adj.kind == "scale_category" and adj.category_id:
            assumptions = scale_assumptions(assumptions, adj.category_id, adj.percent)
        elif adj.kind == "one_off" and adj.on:
            extra.append(Event(adj.on, f"What-if: {adj.label or 'one-off'}", adj.amount, "what-if"))
        elif adj.kind == "add_recurring":
            out_bills.append(
                RecurringBill(
                    next_fake_id, f"What-if: {adj.label or 'new item'}", f"what-if {next_fake_id}",
                    adj.category_id or "uncategorised", adj.amount, adj.cadence,
                    adj.on or as_of + timedelta(days=1), tolerance_pct=0,
                )
            )
            next_fake_id -= 1
    return out_bills, assumptions, extra


def run(
    *,
    opening_balance: int,
    opening_date: date,
    txns: list[Txn],
    bills: list[RecurringBill],
    as_of: date,
    horizon_days: int,
    assumptions: AssumptionSet,
    adjustments: list[Adjustment],
) -> Forecast:
    b2, a2, extra = apply(bills, assumptions, adjustments, as_of)
    fc = build_forecast(
        opening_balance=opening_balance, opening_date=opening_date, txns=txns, bills=b2,
        as_of=as_of, horizon_days=horizon_days, assumptions=a2, extra_events=extra,
    )
    # What-if recurring items must be labelled as what-if in the event list.
    for e in fc.events:
        if e.bill_id is not None and e.bill_id < 0:
            e.source = "what-if"
    return fc


def compare(base: Forecast, alt: Forecast, adjustments: list[Adjustment], bills: list[RecurringBill]) -> Explained:
    delta = alt.closing.value - base.closing.value
    index = {b.id: b for b in bills}
    word = "better" if delta > 0 else ("worse" if delta < 0 else "the same")
    return Explained(
        "scenario_delta",
        f"Difference on {human(base.end)}",
        delta,
        "pkr",
        "forecast",
        "Forecast with your changes − forecast without them",
        (
            Input("Without changes", base.closing.display, "forecast"),
            Input("With changes", alt.closing.display, "forecast"),
            *(Input("Change", describe(a, index), "plan") for a in adjustments),
        ),
        (
            f"With these changes you would be {format_pkr(abs(delta))} {word} off on {human(base.end)}.",
            "What-if changes are not saved to your transactions.",
        ),
    )
