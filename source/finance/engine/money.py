"""Money handling.

All amounts are stored as signed integers in *paisa* (1 rupee = 100 paisa) so
that sums never suffer from floating-point rounding.

Sign convention for the ledger (shared by every module):
    inflow  (income, refund)  -> positive
    outflow (purchase, bill)  -> negative
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

PAISA_PER_RUPEE = 100
#: Largest single transaction we accept: Rs 10 crore (100,000,000 rupees).
MAX_ABS_RUPEES = 100_000_000
MAX_ABS_PAISA = MAX_ABS_RUPEES * PAISA_PER_RUPEE

# Eastern Arabic (٠-٩) and Extended/Urdu (۰-۹) digits -> ASCII digits.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_DIGIT_MAP[ord("٫")] = "."  # Arabic decimal separator
_DIGIT_MAP[ord("٬")] = ","  # Arabic thousands separator

_CURRENCY = re.compile(r"(?i)^(pkr|rs\.?|₨)\s*")
_PLAIN = re.compile(r"^\d+(\.\d{1,2})?$")                           # 120000.50
_WESTERN = re.compile(r"^\d{1,3}(,\d{3})+(\.\d{1,2})?$")           # 120,000.50
_PAKISTANI = re.compile(r"^\d{1,2}(,\d{2})+,\d{3}(\.\d{1,2})?$")  # 1,20,000.50


class AmountError(ValueError):
    """Raised when text cannot be turned into a valid amount."""


def parse_amount(text: object) -> int:
    """Parse user or CSV text into signed paisa.

    Accepts: ``"1,200"``, ``"Rs. 1,200.50"``, ``"PKR 500"``, ``"-850"``,
    ``"(850)"`` (accounting negative), ``"850-"``, Pakistani grouping
    ``"1,20,000"`` and Urdu/Arabic-Indic digits.
    Rejects: empty text, letters, scientific notation, more than two decimal
    places, zero-padding tricks like ``"NaN"``/``"inf"`` and absurd sizes.
    """
    if text is None:
        raise AmountError("Amount is missing.")
    s = str(text).translate(_DIGIT_MAP).strip()
    if not s:
        raise AmountError("Amount is missing.")

    negative = False
    signs = 0
    if s.startswith("(") and s.endswith(")"):
        negative, s, signs = True, s[1:-1].strip(), 1
    s = _CURRENCY.sub("", s)
    if s[:1] in ("-", "−", "+"):
        negative = negative or s[0] in "-−"
        s, signs = _CURRENCY.sub("", s[1:].strip()), signs + 1
    if s.endswith("-"):
        negative, s, signs = True, s[:-1].strip(), signs + 1
    if signs > 1:
        raise AmountError(f"'{text}' has more than one minus sign or bracket. Use one, like -850.")
    if not (_PLAIN.match(s) or _WESTERN.match(s) or _PAKISTANI.match(s)):
        raise AmountError(f"'{text}' is not a number we can read (use digits like 1,250.50).")
    try:
        value = Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regexes already guard
        raise AmountError(f"'{text}' is not a valid amount.") from exc

    paisa = int(value * PAISA_PER_RUPEE)
    if paisa > MAX_ABS_PAISA:
        raise AmountError(
            f"Amount is larger than the supported maximum (Rs {MAX_ABS_RUPEES:,})."
        )
    return -paisa if negative else paisa


def rupees(paisa: int) -> Decimal:
    return Decimal(paisa) / PAISA_PER_RUPEE


def format_pkr(paisa: int, *, signed: bool = False, compact: bool = False) -> str:
    """Human formatting: ``Rs 1,234`` / ``-Rs 1,234.50`` / ``Rs 1.2M``.

    Decimals are only shown when there are paisa, which keeps screens calm.
    ``signed=True`` adds an explicit ``+`` to inflows (used in ledgers).
    """
    sign = "-" if paisa < 0 else ("+" if signed and paisa > 0 else "")
    p = abs(paisa)
    if compact:
        r = p / PAISA_PER_RUPEE
        if r >= 1_000_000:
            body = f"{r / 1_000_000:.1f}M".replace(".0M", "M")
        elif r >= 1_000:
            body = f"{r / 1_000:.0f}k" if r >= 10_000 else f"{r / 1_000:.1f}k".replace(".0k", "k")
        else:
            body = f"{r:.0f}"
        return f"{sign}Rs {body}"
    whole, frac = divmod(p, PAISA_PER_RUPEE)
    body = f"{whole:,}" + (f".{frac:02d}" if frac else "")
    return f"{sign}Rs {body}"


def format_rupees_plain(paisa: int) -> str:
    """Plain machine-friendly rupees for CSV export: ``-1250.50``."""
    sign = "-" if paisa < 0 else ""
    whole, frac = divmod(abs(paisa), PAISA_PER_RUPEE)
    return f"{sign}{whole}.{frac:02d}"
