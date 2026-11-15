"""Budget-versus-actual, variance and the 'free to spend' summary figure."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .categories import EXPENSE_IDS, group_of, name_of
from .dates import human, month_end, month_label, month_start, previous_full_months
from .explain import Explained, Input, money_input
from .forecast import DAYS_PER_MONTH, AssumptionSet, to_rupee
from .models import Budget, Goal, RecurringBill, Txn
from .money import format_pkr
from .recurring import match_occurrences

Status = Literal["ok", "watch", "heading_over", "over"]

STATUS_TEXT = {
    "ok": "On track",
    "watch": "Over 80% used",
    "heading_over": "Likely to go over",
    "over": "Over budget",
}


def spend_in(txns: list[Txn], category_id: str, start: date, end: date) -> int:
    """Net spending (positive paisa) in a category; refunds reduce it."""
    return -sum(
        t.amount for t in txns
        if t.category_id == category_id and start <= t.posted_on <= end and t.kind != "transfer"
    )


@dataclass
class BudgetStatus:
    budget: Budget
    category_name: str
    spent: Explained
    remaining: Explained
    projected: Explained
    pct_used: int
    status: Status
    days_left: int

    @property
    def status_text(self) -> str:
        return STATUS_TEXT[self.status]


def budget_status(
    budget: Budget,
    txns: list[Txn],
    bills: list[RecurringBill],
    assumptions: AssumptionSet,
    as_of: date,
) -> BudgetStatus:
    start, end = month_start(as_of), month_end(as_of)
    spent = spend_in(txns, budget.category_id, start, as_of)
    days_left = (end - as_of).days

    upcoming = [
        o for o in match_occurrences(
            [b for b in bills if b.category_id == budget.category_id], txns, start, end, as_of
        )
        if o.status != "posted"
    ]
    upcoming_total = -sum(o.bill.amount for o in upcoming)
    a = assumptions.by_category().get(budget.category_id)
    daily = (a.base / DAYS_PER_MONTH) if a else 0.0
    variable_rest = to_rupee(daily * days_left)
    projected = spent + upcoming_total + variable_rest
    remaining = budget.monthly_limit - spent
    pct = round(100 * spent / budget.monthly_limit) if budget.monthly_limit else 0

    if spent > budget.monthly_limit:
        status: Status = "over"
    elif projected > budget.monthly_limit:
        status = "heading_over"
    elif pct >= 80:
        status = "watch"
    else:
        status = "ok"

    cname = name_of(budget.category_id)
    month = month_label(as_of)
    spent_e = Explained(
        f"spent_{budget.category_id}", f"{cname} spent in {month}", spent, "pkr", "actual",
        "Sum of this month's purchases in the category − refunds",
        (
            Input("Period", f"{human(start)} – {human(as_of)}"),
            money_input("Net spending", spent),
        ),
    )
    remaining_e = Explained(
        f"remaining_{budget.category_id}", f"{cname} budget remaining", remaining, "pkr", "actual",
        "Monthly limit − spent so far",
        (money_input("Monthly limit", budget.monthly_limit, "plan"), money_input("Spent so far", spent)),
        ("A negative number means you are over the limit.",),
    )
    projected_e = Explained(
        f"projected_{budget.category_id}", f"{cname} forecast for {month}", projected, "pkr", "mixed",
        "Spent so far + bills still due this month + typical daily spending × days left",
        (
            money_input("Spent so far", spent, "actual"),
            Input(f"Bills still due ({len(upcoming)})", format_pkr(upcoming_total), "forecast"),
            Input(
                f"Typical spending {format_pkr(to_rupee(daily))}/day × {days_left} days",
                format_pkr(variable_rest), "forecast",
            ),
        ),
        ("Typical daily spending comes from the median of your last full months.",),
    )
    return BudgetStatus(budget, cname, spent_e, remaining_e, projected_e, pct, status, days_left)


@dataclass
class VarianceRow:
    category_id: str
    name: str
    this_month: int
    typical: int
    delta: int
    pct: int | None


def variance_table(txns: list[Txn], as_of: date, months: int = 3) -> list[VarianceRow]:
    """This month so far vs the median of the previous full months.

    Note: this month is partial, so 'so far' is compared with a *full* typical
    month; the UI says so instead of pro-rating silently.
    """
    start = month_start(as_of)
    periods = previous_full_months(as_of, months)
    rows: list[VarianceRow] = []
    for cid in EXPENSE_IDS:
        now = spend_in(txns, cid, start, as_of)
        hist = [spend_in(txns, cid, s, e) for s, e in periods]
        typical = int(statistics.median(hist)) if hist else 0
        if now == 0 and typical == 0:
            continue
        pct = round(100 * (now - typical) / typical) if typical else None
        rows.append(VarianceRow(cid, name_of(cid), now, typical, now - typical, pct))
    rows.sort(key=lambda r: -r.this_month)
    return rows


def monthly_totals(txns: list[Txn], as_of: date, months: int = 6) -> list[dict]:
    """Income, spending and net per month; transfers excluded from both."""
    out = []
    periods = [(month_start(as_of), as_of)] + previous_full_months(as_of, months - 1)
    for s, e in reversed(periods):
        income = sum(t.amount for t in txns if s <= t.posted_on <= e and group_of(t.category_id) == "income")
        spending = -sum(
            t.amount for t in txns
            if s <= t.posted_on <= e and group_of(t.category_id) == "expense" and t.kind != "transfer"
        )
        out.append({"label": month_label(s), "income": income, "spending": spending,
                    "net": income - spending, "partial": e == as_of})
    return out


def free_to_spend(
    balance_now: int,
    txns: list[Txn],
    bills: list[RecurringBill],
    goals: list[Goal],
    as_of: date,
) -> Explained:
    """The beginner's headline number: what is left until month end once
    scheduled bills, scheduled income and planned savings are accounted for."""
    end = month_end(as_of)
    occ = [
        o for o in match_occurrences(bills, txns, as_of - timedelta(days=7), end, as_of)
        if o.status != "posted"
    ]
    bills_due = -sum(o.bill.amount for o in occ if o.bill.amount < 0)
    income_due = sum(o.bill.amount for o in occ if o.bill.amount > 0)
    savings_plan = sum(g.monthly_plan for g in goals if g.saved < g.target)
    value = balance_now + income_due - bills_due - savings_plan
    return Explained(
        "free_to_spend", f"Free to spend until {human(end)}", value, "pkr", "mixed",
        "Balance today + income still due − bills still due − planned savings",
        (
            money_input("Balance today", balance_now, "actual"),
            Input(f"Income still due this month ({sum(1 for o in occ if o.bill.amount > 0)})", format_pkr(income_due, signed=True), "forecast"),
            Input(f"Bills still due this month ({sum(1 for o in occ if o.bill.amount < 0)})", format_pkr(-bills_due), "forecast"),
            Input("Planned savings this month (goals)", format_pkr(-savings_plan), "plan"),
        ),
        (
            "Only bills and income set up as recurring are included.",
            "Planned savings are money you intend to set aside, not a transfer that has happened.",
            "This is a guide, not financial advice.",
        ),
    )
