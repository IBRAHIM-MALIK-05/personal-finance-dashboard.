"""CSV import pipeline (Week 12 change request).

    read -> map columns & declare sign convention -> validate every row
         -> resolve (ambiguous dates, duplicate candidates, refunds)
         -> preview -> atomic commit (done by the service layer) -> undo

Principles agreed in the architecture review:

* Never guess silently.  Ambiguous day/month order, the sign convention and
  every duplicate candidate need an explicit human decision.
* Never drop data silently.  Same-day same-amount rows can be legitimate (two
  coffees), so duplicates are *candidates*, not automatic skips.
* No silent partial import.  Commit is blocked until every row is either ready,
  fixed, or explicitly skipped, and the result lists every skipped row.
* Keep the original.  Each imported transaction stores its raw CSV row and the
  column mapping that produced it.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Literal

from .categories import CATEGORIES, Rule, categorise, normalise_payee
from .dates import DateOrder, DateParseError, human, order_evidence, parse_flexible
from .models import Txn
from .money import AmountError, format_pkr, parse_amount
from .validation import sanitize_text

MAX_BYTES = 2_000_000
MAX_ROWS = 5_000
NEAR_DUPLICATE_DAYS = 3
REFUND_LOOKBACK_DAYS = 90

FIELDS = ("date", "description", "amount", "debit", "credit", "payee", "category")
FIELD_LABELS = {
    "date": "Date", "description": "Description", "amount": "Amount (one column)",
    "debit": "Money out (debit)", "credit": "Money in (credit)", "payee": "Payee / merchant",
    "category": "Category",
}
SYNONYMS = {
    "date": ["date", "txn date", "transaction date", "posting date", "value date", "posted on", "posted"],
    "description": ["description", "narration", "details", "particulars", "memo", "remarks"],
    "amount": ["amount", "amount (pkr)", "amount pkr", "amount (rs)", "transaction amount", "value"],
    "debit": ["debit", "withdrawal", "withdrawals", "dr", "money out", "paid out"],
    "credit": ["credit", "deposit", "deposits", "cr", "money in", "paid in"],
    "payee": ["payee", "merchant", "beneficiary", "counterparty", "name"],
    "category": ["category"],
}
REFUND_WORDS = re.compile(r"\b(refund|refunded|reversal|reversed|returned|chargeback)\b", re.I)
INCOME_WORDS = re.compile(r"\b(salary|payroll|allowance|deposit|received|refund|reversal|tutoring)\b", re.I)

SignConvention = Literal["negative_is_out", "positive_is_out", "split"]
Decision = Literal["import", "skip"]


class ImportFatal(Exception):
    """The file as a whole cannot be used.  The message says how to recover."""


# ----------------------------------------------------------------- reading --

def read_csv(content: bytes | str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Return the header and (line_number, values) for each non-blank record."""
    if isinstance(content, bytes):
        if len(content) > MAX_BYTES:
            raise ImportFatal(
                f"The file is {len(content) // 1024:,} KB. The limit is {MAX_BYTES // 1000:,} KB. "
                "Split it into smaller date ranges."
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportFatal(
                "This file is not UTF-8 text. In Excel choose File → Save As → ‘CSV UTF-8’ and try again."
            ) from exc
    else:
        text = content.lstrip("﻿")
    if "\x00" in text:
        raise ImportFatal("This looks like a binary file (for example .xlsx), not a CSV. Export it as CSV first.")
    rows: list[tuple[int, list[str]]] = []
    reader = csv.reader(io.StringIO(text), strict=True)
    start = 1
    try:
        for record in reader:
            if any(c.strip() for c in record):
                rows.append((start, record))   # physical line where the record starts
            start = reader.line_num + 1
    except csv.Error as exc:
        raise ImportFatal(
            f"The CSV structure is broken near line {reader.line_num} ({exc}). "
            "Check for an unclosed quote mark."
        ) from exc
    if not rows:
        raise ImportFatal("The file is empty.")
    header, body = [h.strip() for h in rows[0][1]], rows[1:]
    if not body:
        raise ImportFatal("The file has a header row but no transactions.")
    if len(body) > MAX_ROWS:
        raise ImportFatal(f"The file has {len(body):,} rows; the limit is {MAX_ROWS:,}. Split it into smaller files.")
    return header, body


def guess_mapping(header: list[str]) -> dict[str, int]:
    norm = [h.strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for f in FIELDS:
        for syn in SYNONYMS[f]:
            if syn in norm and norm.index(syn) not in mapping.values():
                mapping[f] = norm.index(syn)
                break
    return mapping


def missing_fields(mapping: dict[str, int]) -> list[str]:
    missing = []
    if "date" not in mapping:
        missing.append("date")
    if "description" not in mapping and "payee" not in mapping:
        missing.append("description")
    if "amount" not in mapping and not ("debit" in mapping and "credit" in mapping):
        missing.append("amount")
    return missing


def guess_sign(rows: list[list[str]], mapping: dict[str, int]) -> tuple[SignConvention | None, str]:
    """Suggest how the file writes spending.  Only a suggestion: the person
    must confirm it on the mapping screen."""
    if "debit" in mapping and "credit" in mapping and "amount" not in mapping:
        return "split", "The file has separate money-out and money-in columns."
    if "amount" not in mapping:
        return None, "No amount column is mapped yet."
    pos = neg = 0
    income_pos = income_neg = 0
    di = mapping.get("description", mapping.get("payee"))
    for r in rows:
        try:
            v = parse_amount(r[mapping["amount"]])
        except (AmountError, IndexError):
            continue
        text = r[di] if di is not None and di < len(r) else ""
        if v > 0:
            pos += 1
            income_pos += bool(INCOME_WORDS.search(text))
        elif v < 0:
            neg += 1
            income_neg += bool(INCOME_WORDS.search(text))
    total = pos + neg
    if not total:
        return None, "No readable amounts found."
    if pos / total >= 0.7 and income_neg >= income_pos:
        return "positive_is_out", (
            f"{pos} of {total} amounts are positive, and rows that look like income or refunds "
            f"are negative ({income_neg}). This file probably shows spending as positive numbers."
        )
    if neg / total >= 0.5:
        return "negative_is_out", (
            f"{neg} of {total} amounts are negative. This file probably shows spending as negative numbers."
        )
    return None, f"Mixed signs ({pos} positive, {neg} negative). Please check a few rows and choose."


# ---------------------------------------------------------------- settings --

@dataclass
class Settings:
    mapping: dict[str, int] = field(default_factory=dict)
    sign: SignConvention | None = None
    date_order: DateOrder | None = None
    row_dates: dict[int, str] = field(default_factory=dict)        # row_no -> ISO date chosen
    overrides: dict[int, dict[str, str]] = field(default_factory=dict)  # row_no -> fixed values
    decisions: dict[int, Decision] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str | None) -> "Settings":
        if not text:
            return cls()
        d = json.loads(text)
        return cls(
            mapping={k: int(v) for k, v in d.get("mapping", {}).items()},
            sign=d.get("sign"),
            date_order=d.get("date_order"),
            row_dates={int(k): v for k, v in d.get("row_dates", {}).items()},
            overrides={int(k): v for k, v in d.get("overrides", {}).items()},
            decisions={int(k): v for k, v in d.get("decisions", {}).items()},
        )


# ---------------------------------------------------------------- analysis --

@dataclass
class Issue:
    code: str
    severity: Literal["error", "decision", "info"]
    message: str
    field: str | None = None


@dataclass
class Row:
    row_no: int                      # line number in the file (header is line 1)
    raw: list[str]
    posted_on: date | None = None
    amount: int | None = None
    payee: str = ""
    description: str = ""
    category_id: str = "uncategorised"
    category_reason: str = ""
    category_source: str = "rule"
    kind: str = "normal"
    refund_of: int | None = None
    refund_of_row: int | None = None
    duplicate_of: int | None = None
    duplicate_of_row: int | None = None
    date_candidates: list[date] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    decision: Decision | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def decisions_needed(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "decision"]

    @property
    def status(self) -> Literal["ready", "needs_decision", "error", "skipped"]:
        if self.decision == "skip":
            return "skipped"
        if self.errors:
            return "error"
        if self.decision is None or self.posted_on is None or self.amount is None:
            # A generic 'import' decision can never stand in for an unresolved date or amount.
            return "needs_decision"
        return "ready"

    def has(self, code: str) -> bool:
        return any(i.code == code for i in self.issues)


@dataclass
class Analysis:
    header: list[str]
    rows: list[Row]
    mapping: dict[str, int]
    missing: list[str]
    sign: SignConvention | None
    sign_guess: SignConvention | None
    sign_evidence: str
    date_order: DateOrder | None
    date_evidence: dict[str, int]
    blocking: list[str]

    @property
    def counts(self) -> dict[str, int]:
        c = {"total": len(self.rows), "ready": 0, "needs_decision": 0, "error": 0, "skipped": 0}
        for r in self.rows:
            c[r.status] += 1
        return c

    @property
    def to_import(self) -> list[Row]:
        return [r for r in self.rows if r.status == "ready"]

    @property
    def can_commit(self) -> bool:
        return not self.blocking


def derive_payee(description: str) -> str:
    """'QuickBite Delivery - Order #4471' -> 'QuickBite Delivery';
    'REFUND - Bazaar Online order' -> 'Bazaar Online'."""
    s = REFUND_WORDS.sub("", description).strip(" -–:/|")
    s = re.split(r"\s+[-–/|]\s+", s, maxsplit=1)[0]
    s = re.sub(r"(?i)(ref|txn|order|inv)\.?\s*#?\s*\d\w*", "", s)
    s = re.sub(r"#\s*\w+", "", s)
    s = re.sub(r"(?i)\s+(order|purchase|pos purchase|payment|trip)$", "", s.strip())
    s = re.sub(r"\s+", " ", s).strip(" -–:")
    return s[:60] or description[:60]


def same_payee(a: str, b: str) -> bool:
    """Loose payee comparison for refunds: 'bazaar online order' ~ 'bazaar online'."""
    x, y = normalise_payee(a), normalise_payee(b)
    return bool(x and y) and (x == y or x.startswith(y + " ") or y.startswith(x + " "))


def _cell(raw: list[str], mapping: dict[str, int], name: str, overrides: dict[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    idx = mapping.get(name)
    if idx is None or idx >= len(raw):
        return ""
    return raw[idx]


def _match_category_name(text: str) -> str | None:
    t = text.strip().lower()
    for c in CATEGORIES.values():
        if t in (c.id, c.name.lower()):
            return c.id
    return None


def analyse(
    content: bytes | str,
    settings: Settings,
    existing: list[Txn],
    rules: list[Rule],
    as_of: date,
) -> Analysis:
    header, body = read_csv(content)
    mapping = settings.mapping or guess_mapping(header)
    missing = missing_fields(mapping)
    sign_guess, sign_evidence = guess_sign([r for _, r in body], mapping)
    sign = settings.sign
    if "debit" in mapping and "credit" in mapping and "amount" not in mapping:
        sign = "split"
    date_texts = [r[mapping["date"]] for _, r in body if "date" in mapping and mapping["date"] < len(r)]
    evidence = order_evidence(date_texts)

    existing_keys: dict[tuple, Txn] = {}
    by_payee: dict[str, list[Txn]] = {}
    for t in existing:
        key = normalise_payee(t.payee)
        existing_keys.setdefault((t.posted_on, t.amount, key), t)
        by_payee.setdefault(key, []).append(t)

    rows: list[Row] = []
    seen_in_file: dict[tuple, int] = {}
    for line_no, raw in body:
        row = Row(row_no=line_no, raw=raw)
        ov = settings.overrides.get(row.row_no, {})
        rows.append(row)

        if len(raw) != len(header) and not ov:
            row.issues.append(Issue(
                "COLUMN_COUNT", "error",
                f"This row has {len(raw)} values but the header has {len(header)}. "
                "A comma inside a value may have split it. Fix the values below or skip the row.",
            ))
        if missing:
            continue

        # -- date --
        date_text = _cell(raw, mapping, "date", ov)
        try:
            p = parse_flexible(date_text, settings.date_order)
            chosen = settings.row_dates.get(row.row_no)
            if p.ambiguous:
                row.date_candidates = p.candidates
                if chosen:
                    row.posted_on = date.fromisoformat(chosen)
                    row.issues.append(Issue("DATE_CHOSEN", "info", f"You chose {human(row.posted_on)} for ‘{date_text}’.", "date"))
                elif p.value:
                    row.posted_on = p.value
                    row.issues.append(Issue(
                        "DATE_BY_FILE_ORDER", "info",
                        f"‘{date_text}’ read as {human(p.value)} using the file’s {settings.date_order} setting.", "date",
                    ))
                else:
                    a, b = p.candidates
                    row.issues.append(Issue(
                        "DATE_AMBIGUOUS", "decision",
                        f"‘{date_text}’ could be {human(a)} or {human(b)}. Choose one, or set the day/month order for the whole file.",
                        "date",
                    ))
            else:
                row.posted_on = p.value
                if p.evidence and settings.date_order and p.evidence != settings.date_order:
                    row.issues.append(Issue(
                        "DATE_OTHER_ORDER", "info",
                        f"‘{date_text}’ only makes sense as {human(p.value)} ({p.pattern}), "
                        f"unlike the file’s {settings.date_order} setting. Check it is right.", "date",
                    ))
            if row.posted_on and row.posted_on > as_of:
                row.issues.append(Issue(
                    "DATE_FUTURE", "decision",
                    f"Dated {human(row.posted_on)}, which is after today. Bank exports normally contain only past transactions.",
                    "date",
                ))
        except DateParseError as exc:
            row.issues.append(Issue("DATE_INVALID", "error", str(exc), "date"))

        # -- amount --
        try:
            if sign == "split":
                out_txt = _cell(raw, mapping, "debit", ov).strip()
                in_txt = _cell(raw, mapping, "credit", ov).strip()
                out_v = parse_amount(out_txt) if out_txt else 0
                in_v = parse_amount(in_txt) if in_txt else 0
                value = abs(in_v) - abs(out_v)
                if out_txt and in_txt:
                    row.issues.append(Issue("BOTH_SIDES", "info", "Both money-in and money-out are filled; we used the difference.", "amount"))
            else:
                value = parse_amount(_cell(raw, mapping, "amount", ov))
                if sign == "positive_is_out":
                    value = -value
            if value == 0:
                row.issues.append(Issue("AMOUNT_ZERO", "error", "Amount is zero.", "amount"))
            elif sign is not None:
                row.amount = value
        except AmountError as exc:
            row.issues.append(Issue("AMOUNT_INVALID", "error", str(exc), "amount"))

        # -- text --
        desc = sanitize_text(_cell(raw, mapping, "description", ov))
        payee_raw = _cell(raw, mapping, "payee", ov)
        payee = sanitize_text(payee_raw if payee_raw.strip() else derive_payee(desc.value), 60)
        row.description, row.payee = desc.value, payee.value
        if not row.description and not row.payee:
            row.issues.append(Issue("TEXT_MISSING", "error", "Description is empty.", "description"))
        for n in desc.notes + payee.notes:
            row.issues.append(Issue("TEXT_CLEANED", "info", f"For safety we {n}.", "description"))

        # -- category --
        cat_text = _cell(raw, mapping, "category", ov)
        cat = _match_category_name(cat_text) if cat_text else None
        if cat:
            row.category_id, row.category_source, row.category_reason = cat, "import", "From the file’s category column"
        else:
            row.category_id, _, row.category_reason = categorise(row.payee, row.description, rules)

        if row.errors:
            row.decision = "skip" if settings.decisions.get(row.row_no) == "skip" else None
            continue

        # -- refunds --
        if REFUND_WORDS.search(row.description) and row.amount is not None:
            if row.amount < 0:
                row.issues.append(Issue(
                    "REFUND_SIGN", "decision",
                    "This looks like a refund but reads as money out. Check the sign setting, or import it as it is.",
                    "amount",
                ))
            else:
                row.kind = "refund"
                original = _find_original(row, existing, rows)
                if original is not None:
                    if isinstance(original, Txn):
                        row.refund_of, row.category_id = original.id, original.category_id
                        when = human(original.posted_on)
                    else:
                        row.refund_of_row, row.category_id = original.row_no, original.category_id
                        when = f"row {original.row_no}"
                    row.category_reason = "Same category as the original purchase"
                    row.issues.append(Issue("REFUND_MATCHED", "info", f"Refund of a {row.payee} purchase ({when}). It reduces that category’s spending."))
                else:
                    row.issues.append(Issue("REFUND_UNMATCHED", "info", "Refund with no matching purchase found. It reduces spending in its category, not income."))

        # -- duplicates --
        if row.posted_on and row.amount is not None:
            key = (row.posted_on, row.amount, normalise_payee(row.payee))
            if key in existing_keys:
                t = existing_keys[key]
                row.duplicate_of = t.id
                row.issues.append(Issue(
                    "DUPLICATE_EXISTING", "decision",
                    f"Same date, amount and payee as a transaction you already have ({human(t.posted_on)}, {format_pkr(t.amount)}).",
                ))
            elif key in seen_in_file:
                row.duplicate_of_row = seen_in_file[key]
                row.issues.append(Issue(
                    "DUPLICATE_IN_FILE", "decision",
                    f"Identical to row {seen_in_file[key]} in this file. It could be a real repeat purchase or a double export.",
                ))
            else:
                near = [
                    t for t in by_payee.get(key[2], [])
                    if t.amount == row.amount and 0 < abs((t.posted_on - row.posted_on).days) <= NEAR_DUPLICATE_DAYS
                ]
                if near:
                    row.duplicate_of = near[0].id
                    row.issues.append(Issue(
                        "POSSIBLE_DUPLICATE", "decision",
                        f"Same amount and payee as an existing transaction on {human(near[0].posted_on)}. "
                        "This can happen when dates are written differently.",
                    ))
            seen_in_file.setdefault(key, row.row_no)

        explicit = settings.decisions.get(row.row_no)
        if explicit:
            row.decision = explicit
        elif not row.decisions_needed:
            row.decision = "import"

    blocking: list[str] = []
    if missing:
        blocking.append("Map the required columns: " + ", ".join(FIELD_LABELS[m] for m in missing) + ".")
    if sign is None:
        blocking.append("Confirm how this file shows spending (positive or negative numbers).")
    a = Analysis(header, rows, mapping, missing, sign, sign_guess, sign_evidence,
                 settings.date_order, evidence, blocking)
    c = a.counts
    if not missing and sign is not None:
        if c["error"]:
            blocking.append(f"{c['error']} row(s) have errors. Fix them or choose ‘Skip’.")
        if c["needs_decision"]:
            blocking.append(f"{c['needs_decision']} row(s) need a decision.")
        if not c["ready"]:
            blocking.append("No rows are ready to import.")
    return a


def _find_original(refund: Row, existing: list[Txn], rows: list[Row]):
    """Most recent earlier purchase from the same payee that is at least as
    large as the refund, within 90 days: first in the ledger, then in the file."""
    if not refund.posted_on:
        return None
    lo = refund.posted_on - timedelta(days=REFUND_LOOKBACK_DAYS)
    for t in sorted(existing, key=lambda t: t.posted_on, reverse=True):
        if (t.amount < 0 and abs(t.amount) >= refund.amount and lo <= t.posted_on <= refund.posted_on
                and same_payee(t.payee, refund.payee)):
            return t
    for r in reversed(rows[:-1]):
        if (r.posted_on and r.amount is not None and r.amount < 0 and same_payee(r.payee, refund.payee)
                and abs(r.amount) >= refund.amount and lo <= r.posted_on <= refund.posted_on and not r.errors):
            return r
    return None


def bulk_decide(analysis: Analysis, code: str, decision: Decision) -> dict[int, Decision]:
    """Decisions for every row carrying ``code`` (e.g. skip all exact duplicates)."""
    return {r.row_no: decision for r in analysis.rows if r.has(code)}


def skip_errors(analysis: Analysis) -> dict[int, Decision]:
    return {r.row_no: "skip" for r in analysis.rows if r.errors}
