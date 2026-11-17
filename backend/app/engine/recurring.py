"""Recurring bills and income: occurrence generation and matching.

Rules that matter for correctness (and are unit-tested):

* Occurrences are always derived from the *anchor* date, not from the
  previous occurrence, so a bill anchored on 31 Jan falls on 28 Feb and then
  back on 31 Mar (no drift to the 28th forever).
* An occurrence is *posted* only when a ledger transaction is explicitly linked
  to it (``recurring_id`` + ``occurrence_date``).  Posted occurrences are never
  projected again, so a bill is never counted twice.
* An unlinked transaction to the same payee within ±3 days and inside the
  bill's amount tolerance makes the occurrence *suggested*: it is still
  projected (two similar purchases can both be real) but labelled "may already
  be paid" until the person confirms the link.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .categories import normalise_payee
from .dates import add_months
from .models import Cadence, RecurringBill, Txn

MATCH_WINDOW_DAYS = 3
OVERDUE_GRACE_DAYS = 7

_STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def occurrences(bill: RecurringBill, start: date, end: date) -> list[date]:
    """All due dates of ``bill`` in ``[start, end]`` (inclusive)."""
    if end < start:
        return []
    last = min(end, bill.end_date) if bill.end_date else end
    out: list[date] = []
    if bill.cadence == "weekly":
        if bill.anchor_date >= start:
            k = 0
        else:
            k = (start - bill.anchor_date).days // 7
        while True:
            d = bill.anchor_date + timedelta(days=7 * k)
            if d > last:
                break
            if d >= start:
                out.append(d)
            k += 1
        return out
    step = _STEP_MONTHS[bill.cadence]
    k = 0
    if bill.anchor_date < start:
        months_gap = (start.year - bill.anchor_date.year) * 12 + start.month - bill.anchor_date.month
        k = max(0, months_gap // step - 1)
    while True:
        d = add_months(bill.anchor_date, step * k)
        if d > last:
            break
        if d >= start:
            out.append(d)
        k += 1
    return out


def monthly_equivalent(amount: int, cadence: Cadence) -> int:
    """What a bill costs per month on average (used for comparisons)."""
    if cadence == "weekly":
        return round(amount * 52 / 12)
    return round(amount / _STEP_MONTHS[cadence])


def _amount_matches(bill: RecurringBill, amount: int) -> bool:
    if (amount > 0) != (bill.amount > 0):
        return False
    tol = abs(bill.amount) * bill.tolerance_pct / 100
    return abs(abs(amount) - abs(bill.amount)) <= tol


@dataclass
class Occurrence:
    bill: RecurringBill
    due: date
    status: Literal["posted", "suggested", "overdue", "upcoming"]
    txn: Txn | None = None

    @property
    def amount(self) -> int:
        return self.txn.amount if self.txn else self.bill.amount


def match_occurrences(
    bills: list[RecurringBill], txns: list[Txn], start: date, end: date, as_of: date
) -> list[Occurrence]:
    """Status of every occurrence of every active bill in ``[start, end]``.

    Each transaction can satisfy at most one occurrence.
    """
    used: set[int] = set()
    result: list[Occurrence] = []
    # Linked transactions first: they are certain.
    linked = {
        (t.recurring_id, t.occurrence_date): t
        for t in txns
        if t.recurring_id is not None and t.occurrence_date is not None
    }
    by_payee: dict[str, list[Txn]] = {}
    for t in txns:
        by_payee.setdefault(normalise_payee(t.payee), []).append(t)

    for bill in bills:
        if not bill.active:
            continue
        key = normalise_payee(bill.payee)
        for due in occurrences(bill, start, end):
            txn = linked.get((bill.id, due))
            if txn is None:
                for t in by_payee.get(key, []):
                    if id(t) in used or t.occurrence_date is not None or t.recurring_id not in (None, bill.id):
                        continue
                    if abs((t.posted_on - due).days) <= MATCH_WINDOW_DAYS and _amount_matches(bill, t.amount):
                        txn = t
                        break
            if txn is not None:
                used.add(id(txn))
                status = "posted" if txn.recurring_id == bill.id and txn.occurrence_date == due else "suggested"
                result.append(Occurrence(bill, due, status, txn))
            elif due <= as_of:
                result.append(Occurrence(bill, due, "overdue"))
            else:
                result.append(Occurrence(bill, due, "upcoming"))
    result.sort(key=lambda o: (o.due, o.bill.name))
    return result


def matched_txn_ids(bills: list[RecurringBill], txns: list[Txn], start: date, as_of: date) -> set[int]:
    """Python ids of transactions explained by a recurring bill (so the
    forecaster can exclude them from 'variable' spending)."""
    occ = match_occurrences(bills, txns, start, as_of + timedelta(days=MATCH_WINDOW_DAYS), as_of)
    return {id(o.txn) for o in occ if o.txn is not None}


def next_due(bill: RecurringBill, txns: list[Txn], as_of: date) -> Occurrence | None:
    """The next unpaid occurrence (an overdue one counts) for display."""
    window_start = as_of - timedelta(days=OVERDUE_GRACE_DAYS)
    horizon = as_of + timedelta(days=400)
    for occ in match_occurrences([bill], txns, window_start, horizon, as_of):
        if occ.status != "posted":
            return occ
    return None
