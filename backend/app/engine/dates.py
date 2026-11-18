"""Date-only helpers.

Financial events are *dates*, never datetimes: a bill is due on 31 Jan, not at
a time-zone-dependent instant.  Everything is ``datetime.date`` internally and
ISO ``YYYY-MM-DD`` text in storage.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

DateOrder = Literal["DMY", "MDY"]

MONTHS = {
    m.lower(): i
    for i, m in enumerate(calendar.month_abbr)
    if m
}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTHS["sept"] = 9

MIN_YEAR, MAX_YEAR = 1990, 2100


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's length.

    31 Jan + 1 month -> 28/29 Feb (never 3 Mar).
    """
    total = d.year * 12 + (d.month - 1) + months
    y, m = divmod(total, 12)
    m += 1
    return date(y, m, min(d.day, days_in_month(y, m)))


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=days_in_month(d.year, d.month))


def month_label(d: date) -> str:
    return f"{calendar.month_abbr[d.month]} {d.year}"


def previous_full_months(as_of: date, count: int) -> list[tuple[date, date]]:
    """The ``count`` complete calendar months before ``as_of``'s month,
    most recent first, as (start, end) pairs."""
    out = []
    cursor = month_start(as_of)
    for _ in range(count):
        cursor = add_months(cursor, -1)
        out.append((cursor, month_end(cursor)))
    return out


def human(d: date) -> str:
    """``3 Nov 2026`` - unambiguous in every locale (no 03/11 confusion)."""
    return f"{d.day} {calendar.month_abbr[d.month]} {d.year}"


# ---------------------------------------------------------------- parsing --

@dataclass
class ParsedDate:
    """Result of reading a date written by a person or a bank export.

    ``value`` is ``None`` when the text is ambiguous and no order was chosen;
    ``candidates`` then lists every valid reading so the UI can ask.
    """

    text: str
    value: date | None
    pattern: str
    ambiguous: bool = False
    candidates: list[date] = field(default_factory=list)
    evidence: DateOrder | None = None  # which order this row *proves*, if any


class DateParseError(ValueError):
    pass


_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_NUMERIC = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$")
_D_MON_Y = re.compile(r"^(\d{1,2})[\s-]+([A-Za-z]{3,9})\.?,?[\s-]+(\d{4})$")
_MON_D_Y = re.compile(r"^([A-Za-z]{3,9})\.?[\s-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s-]+(\d{4})$")


def _make(y: int, m: int, d: int) -> date | None:
    if not (MIN_YEAR <= y <= MAX_YEAR) or not (1 <= m <= 12):
        return None
    if not (1 <= d <= days_in_month(y, m)):
        return None
    return date(y, m, d)


def parse_flexible(text: object, order: DateOrder | None = None) -> ParsedDate:
    """Read the date formats commonly found in Pakistani bank exports.

    Supported: ``2026-11-03``, ``03/11/2026`` (D/M or M/D), ``3-11-26``,
    ``3 Nov 2026``, ``Nov 3, 2026``.

    A numeric date such as ``04/05/2026`` is *ambiguous* (4 May or 5 Apr).  We
    never guess silently: without an explicit ``order`` the result has
    ``value=None`` and both ``candidates``.  With an ``order`` the value is
    filled but ``ambiguous`` stays ``True`` so the UI can still flag it.
    """
    s = ("" if text is None else str(text)).strip()
    if not s:
        raise DateParseError("Date is missing.")

    m = _YMD.match(s)
    if m:
        v = _make(int(m[1]), int(m[2]), int(m[3]))
        if not v:
            raise DateParseError(f"'{s}' is not a real calendar date.")
        return ParsedDate(s, v, "YYYY-MM-DD")

    m = _D_MON_Y.match(s) or None
    if m and m[2].lower() in MONTHS:
        v = _make(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
        if not v:
            raise DateParseError(f"'{s}' is not a real calendar date.")
        return ParsedDate(s, v, "D Mon YYYY")

    m = _MON_D_Y.match(s)
    if m and m[1].lower() in MONTHS:
        v = _make(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        if not v:
            raise DateParseError(f"'{s}' is not a real calendar date.")
        return ParsedDate(s, v, "Mon D, YYYY")

    m = _NUMERIC.match(s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        dmy = _make(y, b, a)
        mdy = _make(y, a, b)
        if dmy and not mdy:
            return ParsedDate(s, dmy, "DD/MM/YYYY", evidence="DMY")
        if mdy and not dmy:
            return ParsedDate(s, mdy, "MM/DD/YYYY", evidence="MDY")
        if not dmy and not mdy:
            raise DateParseError(f"'{s}' is not a real calendar date.")
        if dmy == mdy:  # e.g. 05/05/2026
            return ParsedDate(s, dmy, "DD/MM/YYYY")
        chosen = None if order is None else (dmy if order == "DMY" else mdy)
        return ParsedDate(
            s, chosen, "DD/MM or MM/DD", ambiguous=True, candidates=[dmy, mdy]
        )

    raise DateParseError(f"'{s}' is not a date format we recognise (try 2026-11-03).")


def order_evidence(texts: list[str]) -> dict[str, int]:
    """Count rows that can *only* be read one way.  Used to suggest - never
    impose - a file-level day/month order."""
    counts = {"DMY": 0, "MDY": 0, "ambiguous": 0, "unambiguous_other": 0, "invalid": 0}
    for t in texts:
        try:
            p = parse_flexible(t)
        except DateParseError:
            counts["invalid"] += 1
            continue
        if p.evidence:
            counts[p.evidence] += 1
        elif p.ambiguous:
            counts["ambiguous"] += 1
        else:
            counts["unambiguous_other"] += 1
    return counts
