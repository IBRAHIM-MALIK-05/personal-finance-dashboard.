"""Savings goals.

A contribution is an *allocation* of money you already have.  It only changes
the account balance if the person also records a transfer to a savings
account, so goal money is never subtracted twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .dates import add_months, human
from .explain import Explained, Input, money_input
from .models import Goal
from .money import format_pkr


@dataclass
class GoalProjection:
    goal: Goal
    pct: int
    remaining: int
    months_left: int
    required_monthly: Explained
    projected_finish: Explained
    on_track: bool
    done: bool


def months_between(a: date, b: date) -> int:
    """Whole months from a to b, counting a partial month as one."""
    m = (b.year - a.year) * 12 + b.month - a.month
    if b.day > a.day:
        m += 1
    return max(m, 0)


def project(goal: Goal, as_of: date) -> GoalProjection:
    remaining = max(goal.target - goal.saved, 0)
    pct = min(100, round(100 * goal.saved / goal.target)) if goal.target else 0
    months_left = months_between(as_of, goal.deadline)
    done = remaining == 0
    required = 0 if done else math.ceil(remaining / max(months_left, 1))

    if done:
        finish_value, finish_formula = as_of, "Goal reached"
    elif goal.monthly_plan > 0:
        n = math.ceil(remaining / goal.monthly_plan)
        finish_value = add_months(as_of, n)
        finish_formula = f"Today + ⌈remaining ÷ monthly plan⌉ = {n} month(s)"
    else:
        finish_value, finish_formula = None, "No monthly plan set, so no finish date"

    on_track = done or (goal.monthly_plan >= required and months_left > 0)
    required_e = Explained(
        f"goal_required_{goal.id}", "Needed each month", required, "pkr", "plan",
        "(Target − saved) ÷ months until the deadline, rounded up",
        (
            money_input("Target", goal.target, "plan"),
            money_input("Saved so far", goal.saved, "actual"),
            Input("Months until deadline", f"{months_left} (deadline {human(goal.deadline)})", "plan"),
        ),
        ("Months are counted as calendar months; a partly-finished month counts as one.",)
        + (("The deadline has passed. Choose a new date or a new target.",) if months_left == 0 and not done else ()),
    )
    finish_e = Explained(
        f"goal_finish_{goal.id}", "Projected finish", finish_value, "date", "forecast",
        finish_formula,
        (money_input("Remaining", remaining, "actual"), money_input("Your monthly plan", goal.monthly_plan, "plan")),
        ("Assumes you add the planned amount every month.",),
    )
    return GoalProjection(goal, pct, remaining, months_left, required_e, finish_e, on_track, done)


def status_sentence(p: GoalProjection) -> str:
    if p.done:
        return "Goal reached"
    if p.months_left == 0:
        return "Deadline passed"
    if p.on_track:
        return "On track"
    return f"Behind: needs {format_pkr(p.required_monthly.value)} a month"
