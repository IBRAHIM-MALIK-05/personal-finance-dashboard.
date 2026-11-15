"""Plain data objects the engine works on.

The engine never talks to the database: services load rows, convert them to
these dataclasses and hand them to pure functions.  That keeps every
calculation unit-testable with hand-built data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Cadence = Literal["weekly", "monthly", "quarterly", "yearly"]
TxnKind = Literal["normal", "refund", "transfer"]


@dataclass
class Txn:
    id: int | None
    posted_on: date
    amount: int                      # signed paisa: inflow +, outflow -
    payee: str
    description: str = ""
    category_id: str = "uncategorised"
    kind: TxnKind = "normal"
    category_source: str = "rule"    # rule | user | import
    recurring_id: int | None = None
    occurrence_date: date | None = None
    refund_of: int | None = None
    import_batch_id: int | None = None
    version: int = 1


@dataclass
class RecurringBill:
    id: int
    name: str
    payee: str
    category_id: str
    amount: int                      # signed: salary +, bill -
    cadence: Cadence
    anchor_date: date                # first due date; later dates derive from it
    tolerance_pct: int = 10          # how much the real amount may vary (utilities vary)
    end_date: date | None = None
    active: bool = True
    version: int = 1


@dataclass
class Budget:
    id: int
    category_id: str
    monthly_limit: int               # positive paisa
    version: int = 1


@dataclass
class Goal:
    id: int
    name: str
    target: int
    deadline: date
    monthly_plan: int
    saved: int = 0                   # sum of contributions (allocations)
    version: int = 1


@dataclass
class Adjustment:
    """One what-if change.  Never written to the ledger."""

    kind: Literal["cancel_bill", "change_bill", "scale_category", "one_off", "add_recurring"]
    bill_id: int | None = None
    category_id: str | None = None
    percent: int = 0                 # for scale_category: -50 means 'spend 50% less'
    amount: int = 0                  # signed paisa for one_off / add_recurring / change_bill
    on: date | None = None           # one_off date or add_recurring start
    cadence: Cadence = "monthly"
    label: str = ""


@dataclass
class Scenario:
    name: str
    adjustments: list[Adjustment] = field(default_factory=list)
