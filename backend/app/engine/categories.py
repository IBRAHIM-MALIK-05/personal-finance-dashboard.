"""Fixed category list and keyword rules.

Categories are global (not per workspace) to keep budgets comparable across
profiles.  Rules are per workspace: system rules are seeded, and a person's
correction can create a *user* rule that always wins over system rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Group = Literal["income", "expense", "transfer"]


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    group: Group


CATEGORIES: dict[str, Category] = {
    c.id: c
    for c in [
        Category("salary", "Salary", "income"),
        Category("allowance", "Allowance", "income"),
        Category("other_income", "Other income", "income"),
        Category("housing", "Rent & housing", "expense"),
        Category("utilities", "Utilities", "expense"),
        Category("phone_internet", "Phone & internet", "expense"),
        Category("groceries", "Groceries", "expense"),
        Category("dining", "Food & dining", "expense"),
        Category("transport", "Transport & fuel", "expense"),
        Category("education", "Education", "expense"),
        Category("health", "Health", "expense"),
        Category("shopping", "Shopping", "expense"),
        Category("subscriptions", "Subscriptions & fitness", "expense"),
        Category("giving", "Gifts & charity", "expense"),
        Category("savings_transfer", "Transfer to savings", "transfer"),
        Category("uncategorised", "Uncategorised", "expense"),
    ]
}

EXPENSE_IDS = [c.id for c in CATEGORIES.values() if c.group == "expense"]
BUDGETABLE_IDS = [i for i in EXPENSE_IDS if i != "uncategorised"]


def group_of(category_id: str) -> Group:
    cat = CATEGORIES.get(category_id)
    return cat.group if cat else "expense"


def name_of(category_id: str) -> str:
    cat = CATEGORIES.get(category_id)
    return cat.name if cat else category_id


# ----------------------------------------------------------------- rules --

@dataclass(frozen=True)
class Rule:
    id: int | None
    pattern: str          # normalised substring to look for in payee/description
    category_id: str
    priority: int = 0
    source: Literal["system", "user"] = "system"


_PUNCT = re.compile(r"[^a-z0-9 ]+")
_NOISE = re.compile(r"\b(pvt|ltd|limited|pk|pos|ref|txn|no)\b")


def normalise_payee(text: str) -> str:
    """'QuickBite Delivery #4471 (Pvt) Ltd' -> 'quickbite delivery'."""
    s = (text or "").lower()
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    s = _NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


#: (pattern, category) - patterns are normalised payee fragments.
SYSTEM_RULES: list[tuple[str, str]] = [
    ("payroll", "salary"),
    ("salary", "salary"),
    ("family transfer", "allowance"),
    ("allowance", "allowance"),
    ("tutoring", "other_income"),
    ("freelance", "other_income"),
    ("hostel", "housing"),
    ("landlord", "housing"),
    ("house rent", "housing"),
    ("power co", "utilities"),
    ("gasline", "utilities"),
    ("water board", "utilities"),
    ("netzone", "phone_internet"),
    ("zing mobile", "phone_internet"),
    ("mega mart", "groceries"),
    ("al noor grocers", "groceries"),
    ("sabzi", "groceries"),
    ("cafeteria", "dining"),
    ("quickbite", "dining"),
    ("karahi house", "dining"),
    ("chai corner", "dining"),
    ("ridenow", "transport"),
    ("metro card", "transport"),
    ("petroone", "transport"),
    ("book bazaar", "education"),   # deliberately also catches 'Book Bazaar Cafe'
    ("print shop", "education"),
    ("bright future school", "education"),
    ("pharma plus", "health"),
    ("city clinic", "health"),
    ("bazaar online", "shopping"),
    ("style hub", "shopping"),
    ("streamflix", "subscriptions"),
    ("fitzone", "subscriptions"),
    ("hope foundation", "giving"),
    ("savings account", "savings_transfer"),
]


def categorise(payee: str, description: str, rules: list[Rule]) -> tuple[str, Rule | None, str]:
    """Pick a category and say *why*.

    Order: user rules before system rules, then higher priority, then the
    longest (most specific) pattern.  Returns (category_id, rule, reason).
    """
    haystack = f"{normalise_payee(payee)} | {normalise_payee(description)}"
    ordered = sorted(
        rules,
        key=lambda r: (r.source != "user", -r.priority, -len(r.pattern)),
    )
    for rule in ordered:
        if rule.pattern and rule.pattern in haystack:
            who = "your rule" if rule.source == "user" else "built-in rule"
            return rule.category_id, rule, f"Matched {who} “{rule.pattern}”"
    return "uncategorised", None, "No rule matched"
