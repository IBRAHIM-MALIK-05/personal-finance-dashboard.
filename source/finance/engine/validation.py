"""Input validation and sanitisation (shared by forms and CSV import).

Defence in depth: Jinja auto-escapes every value on output, and here we also
clean what goes *in*, so the database never holds markup, control characters
or unbounded text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

from .categories import CATEGORIES
from .dates import DateParseError, parse_flexible
from .money import AmountError, parse_amount

MAX_TEXT = 120
MAX_PAYEE = 80          # = transactions.payee column length
MAX_DESCRIPTION = 160   # = transactions.description column length
MAX_NOTE = 500

_SCRIPT = re.compile(r"(?is)<(script|style)\b[^>]*>.*?(</\1\s*>|$)")
_TAG = re.compile(r"<[^>]*>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_SPACE = re.compile(r"\s+")


@dataclass
class Clean:
    value: str
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def sanitize_text(raw: object, max_len: int = MAX_TEXT) -> Clean:
    """Strip tags, control and bidi-override characters, collapse whitespace
    and truncate by *characters* (an emoji is never cut in half)."""
    s = "" if raw is None else str(raw)
    notes: list[str] = []
    s = unicodedata.normalize("NFC", s)
    if _SCRIPT.search(s) or _TAG.search(s):
        s = _TAG.sub("", _SCRIPT.sub("", s))
        notes.append("removed HTML/script tags")
    if _CONTROL.search(s):
        s = _CONTROL.sub("", s)
        notes.append("removed hidden control characters")
    s = _SPACE.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
        notes.append(f"shortened to {max_len} characters")
    return Clean(s, notes)


def csv_safe(text: str) -> str:
    """Neutralise spreadsheet formula injection in exported text cells."""
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


@dataclass
class FormResult:
    values: dict
    errors: dict[str, str]
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_transaction(form: dict, today: date) -> FormResult:
    """Validate the add/edit transaction form.

    ``direction`` is 'out' (spending) or 'in' (income/refund) so people type
    positive numbers and never have to think about signs.
    """
    errors: dict[str, str] = {}
    values: dict = {}
    notes: list[str] = []

    try:
        parsed = parse_flexible(form.get("posted_on", ""), "DMY")
        if parsed.value is None:
            raise DateParseError("Date is ambiguous.")
        if parsed.value > today + timedelta(days=366):
            errors["posted_on"] = "Date is more than a year in the future. Check the year."
        values["posted_on"] = parsed.value
    except DateParseError as exc:
        errors["posted_on"] = str(exc)

    try:
        amount = parse_amount(form.get("amount", ""))
        if amount == 0:
            errors["amount"] = "Amount cannot be zero."
        elif amount < 0:
            errors["amount"] = "Type the amount without a minus sign and choose ‘Money out’ or ‘Money in’."
        values["amount_abs"] = abs(amount)
    except AmountError as exc:
        errors["amount"] = str(exc)

    direction = form.get("direction", "out")
    if direction not in ("out", "in"):
        errors["direction"] = "Choose money out or money in."

    payee = sanitize_text(form.get("payee", ""), MAX_PAYEE)
    if not payee.value:
        errors["payee"] = "Enter who you paid or who paid you."
    values["payee"] = payee.value
    notes += [f"Payee: {n}" for n in payee.notes]

    desc = sanitize_text(form.get("description", ""), MAX_DESCRIPTION)
    values["description"] = desc.value
    notes += [f"Description: {n}" for n in desc.notes]

    note = sanitize_text(form.get("note", ""), MAX_NOTE)
    values["note"] = note.value

    cat = form.get("category_id") or "auto"
    if cat != "auto" and cat not in CATEGORIES:
        errors["category_id"] = "Choose a category from the list."
    values["category_id"] = cat

    if "amount_abs" in values and not errors.get("direction"):
        values["amount"] = -values["amount_abs"] if direction == "out" else values["amount_abs"]
    return FormResult(values, errors, notes)


def validate_money_field(raw: object, label: str, *, allow_zero: bool = False) -> tuple[int | None, str | None]:
    try:
        v = parse_amount(raw)
    except AmountError as exc:
        return None, f"{label}: {exc}"
    if v < 0:
        return None, f"{label} cannot be negative."
    if v == 0 and not allow_zero:
        return None, f"{label} must be more than zero."
    return v, None
