"""Synthetic data generator (deterministic).

Every value is fictional: names, merchants and amounts are invented for this
prototype.  The same ``seed`` and ``as_of`` always produce exactly the same data,
which keeps tests and demos repeatable.  Run ``python scripts/generate_data.py``
to write ``data/sample_data.json`` and ``data/messy_import.csv``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from .engine.dates import add_months, month_start
from .engine.categories import SYSTEM_RULES, Rule, categorise
from .engine.models import RecurringBill
from .engine.recurring import occurrences

PROFILES = [
    {"id": "ayesha", "display_name": "Ayesha", "role": "owner",
     "blurb": "University student managing her own allowance (personal budget)."},
    {"id": "bilal", "display_name": "Bilal Khan", "role": "manager",
     "blurb": "Manages the Khan household budget and bills."},
    {"id": "sana", "display_name": "Sana Khan", "role": "manager",
     "blurb": "Second editor of the Khan household budget."},
    {"id": "coach_sara", "display_name": "Coach Sara", "role": "coach",
     "blurb": "Financial coach. Read-only access to budgets that are shared with her."},
]

STUDENT_WS, HOUSEHOLD_WS = 1, 2


@dataclass
class SeedData:
    as_of: date
    workspaces: list[dict] = field(default_factory=list)
    memberships: list[dict] = field(default_factory=list)
    grants: list[dict] = field(default_factory=list)
    bills: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    budgets: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    contributions: list[dict] = field(default_factory=list)
    scenarios: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)


def rs(rupees: float) -> int:
    return int(round(rupees)) * 100


class _Gen:
    def __init__(self, seed: int, as_of: date):
        self.r = random.Random(seed)
        self.as_of = as_of
        self.start = month_start(add_months(as_of, -6))
        self.data = SeedData(as_of)
        self.bill_id = 0
        self.rules = [Rule(None, p, c, 0, "system") for p, c in SYSTEM_RULES]

    # -- helpers --
    def amount(self, lo: float, hi: float) -> int:
        """Rounded like real receipts: to Rs 10 below 1,000, else to Rs 50."""
        v = self.r.uniform(lo, hi)
        step = 10 if v < 1000 else 50
        return rs(round(v / step) * step)

    def txn(self, ws: int, d: date, amount: int, payee: str, desc: str = "", kind: str = "normal",
            category: str | None = None, recurring_id: int | None = None, occ: date | None = None):
        if category is None:
            category, _, _ = categorise(payee, desc, self.rules)
        self.data.transactions.append({
            "workspace_id": ws, "posted_on": d, "amount": amount, "payee": payee,
            "description": desc or payee, "category_id": category, "category_source": "rule",
            "kind": kind, "recurring_id": recurring_id, "occurrence_date": occ,
        })

    def bill(self, ws: int, name: str, payee: str, category: str, amount: int, cadence: str,
             day: int, tolerance: int = 10, vary: tuple[float, float] | None = None,
             kind: str = "normal", anchor_month_offset: int = 0) -> None:
        self.bill_id += 1
        first = add_months(self.start, anchor_month_offset)
        anchor = first.replace(day=day)
        b = {"id": self.bill_id, "workspace_id": ws, "name": name, "payee": payee,
             "category_id": category, "amount": amount, "cadence": cadence,
             "anchor_date": anchor, "tolerance_pct": tolerance, "active": True}
        self.data.bills.append(b)
        model = RecurringBill(self.bill_id, name, payee, category, amount, cadence, anchor, tolerance)
        for due in occurrences(model, self.start, self.as_of):
            paid = due + timedelta(days=self.r.choice([0, 0, 0, 1, 2]))
            if paid > self.as_of:
                continue
            a = amount
            if vary:
                a = -self.amount(*vary) if amount < 0 else self.amount(*vary)
            self.txn(ws, paid, a, payee, f"{name}", kind=kind, category=category,
                     recurring_id=self.bill_id, occ=due)

    def repeat(self, ws: int, per_week: float, lo: float, hi: float, payee: str, desc: str = "",
               income: bool = False, category: str | None = None) -> None:
        d = self.start
        while d <= self.as_of:
            if self.r.random() < per_week / 7:
                a = self.amount(lo, hi)
                self.txn(ws, d, a if income else -a, payee, desc or payee, category=category)
            d += timedelta(days=1)

    # -- workspaces --
    def student(self) -> None:
        ws = STUDENT_WS
        self.data.workspaces.append({"id": ws, "name": "Ayesha’s student budget", "kind": "personal",
                                     "opening_balance": rs(8000), "opening_date": self.start})
        self.data.memberships.append({"profile_id": "ayesha", "workspace_id": ws, "permission": "edit"})
        self.bill(ws, "Monthly allowance", "Family Transfer", "allowance", rs(28000), "monthly", 1, 0)
        self.bill(ws, "Hostel rent", "Iqbal Hall Hostel", "housing", -rs(12000), "monthly", 3, 0)
        self.bill(ws, "Mobile bundle", "Zing Mobile", "phone_internet", -rs(1200), "monthly", 10, 5)
        # StreamFlix is paid monthly but deliberately NOT set up as recurring (test task).
        d = self.start.replace(day=15)
        while d <= self.as_of:
            self.txn(ws, d, -rs(450), "StreamFlix", "StreamFlix subscription")
            d = add_months(d, 1)
        self.repeat(ws, 4, 120, 350, "Campus Cafeteria", "Campus Cafeteria lunch")
        self.repeat(ws, 0.7, 500, 1100, "QuickBite Delivery", "QuickBite Delivery order")
        self.repeat(ws, 0.45, 250, 600, "Book Bazaar Cafe", "Book Bazaar Cafe - coffee and snacks")
        self.repeat(ws, 2, 180, 500, "RideNow", "RideNow trip")
        self.repeat(ws, 0.45, 500, 500, "Metro Card", "Metro Card top-up")
        self.repeat(ws, 0.7, 600, 1500, "Al-Noor Grocers", "Al-Noor Grocers")
        self.repeat(ws, 0.3, 500, 2500, "Book Bazaar", "Book Bazaar - textbooks")
        self.repeat(ws, 1, 50, 300, "Campus Print Shop", "Print shop - assignments")
        self.repeat(ws, 0.15, 1500, 5000, "Bazaar Online", "Bazaar Online order")
        self.repeat(ws, 0.25, 300, 1200, "Pharma Plus", "Pharma Plus pharmacy")
        self.repeat(ws, 0.6, 2000, 4500, "Home Tutoring", "Tutoring - O-level maths", income=True)
        self.txn(ws, self.as_of - timedelta(days=9), -rs(1350), "Ali Hardware", "Ali Hardware store")
        self.txn(ws, self.as_of - timedelta(days=4), -rs(780), "POS 4471 Lahore", "POS 4471 LAHORE")
        self._refund(ws, "Bazaar Online")

        for cat, lim in [("dining", 7000), ("groceries", 4000), ("transport", 4500),
                         ("shopping", 3000), ("education", 2500), ("health", 1500)]:
            self.data.budgets.append({"workspace_id": ws, "category_id": cat, "monthly_limit": rs(lim)})
        self._goal(ws, "New laptop", 120000, 8, 6000, [10000] + [6000] * 4)
        self._goal(ws, "Emergency fund", 20000, 10, 1000, [8000, 1000, 1000, 1000, 1000])

    def household(self) -> None:
        ws = HOUSEHOLD_WS
        self.data.workspaces.append({"id": ws, "name": "Khan household", "kind": "household",
                                     "opening_balance": rs(150000), "opening_date": self.start})
        for p in ("bilal", "sana"):
            self.data.memberships.append({"profile_id": p, "workspace_id": ws, "permission": "edit"})
        self.data.grants.append({"workspace_id": ws, "coach_id": "coach_sara", "scope": "summary",
                                 "granted_by": "bilal"})
        self.bill(ws, "Salary", "Indus Textiles Payroll", "salary", rs(195000), "monthly", 1, 0)
        self.bill(ws, "House rent", "Landlord Rashid", "housing", -rs(55000), "monthly", 5, 0)
        self.bill(ws, "Electricity", "Metro Power Co.", "utilities", -rs(12000), "monthly", 20, 50, (8000, 18000))
        self.bill(ws, "Gas", "GasLine Utility", "utilities", -rs(3000), "monthly", 18, 60, (1500, 4500))
        self.bill(ws, "Internet", "NetZone Broadband", "phone_internet", -rs(3500), "monthly", 12, 0)
        self.bill(ws, "Mobile packages", "Zing Mobile", "phone_internet", -rs(2400), "monthly", 10, 5)
        self.bill(ws, "School fees", "Bright Future School", "education", -rs(28000), "quarterly", 8, 0)
        self.bill(ws, "Gym", "FitZone Gym", "subscriptions", -rs(4000), "monthly", 7, 0)
        self.bill(ws, "StreamFlix", "StreamFlix", "subscriptions", -rs(650), "monthly", 15, 0)
        self.bill(ws, "Savings transfer", "Savings Account", "savings_transfer", -rs(15000), "monthly", 2, 0,
                  kind="transfer")
        self.bill(ws, "Monthly donation", "Hope Foundation", "giving", -rs(2000), "monthly", 25, 0)
        self.repeat(ws, 1, 5000, 10000, "Mega Mart", "Mega Mart weekly shop")
        self.repeat(ws, 2, 600, 2000, "Al-Noor Grocers", "Al-Noor Grocers")
        self.repeat(ws, 1, 800, 2000, "Sabzi Mandi", "Sabzi Mandi vegetables")
        self.repeat(ws, 1, 3500, 6500, "PetroOne", "PetroOne fuel")
        self.repeat(ws, 0.6, 300, 900, "RideNow", "RideNow trip")
        self.repeat(ws, 0.45, 3000, 6500, "Karahi House", "Karahi House dinner")
        self.repeat(ws, 1, 1000, 2200, "QuickBite Delivery", "QuickBite Delivery order")
        self.repeat(ws, 0.45, 400, 2500, "Pharma Plus", "Pharma Plus pharmacy")
        self.repeat(ws, 0.12, 2500, 2500, "City Clinic", "City Clinic consultation")
        self.repeat(ws, 0.45, 1500, 6000, "Bazaar Online", "Bazaar Online order")
        self.repeat(ws, 0.23, 2500, 8000, "Style Hub", "Style Hub clothing")
        self.repeat(ws, 0.25, 15000, 35000, "Freelance Design Client", "Freelance design payment", income=True)
        self._refund(ws, "Style Hub")

        for cat, lim in [("groceries", 50000), ("dining", 15000), ("transport", 25000),
                         ("utilities", 20000), ("shopping", 12000), ("health", 5000)]:
            self.data.budgets.append({"workspace_id": ws, "category_id": cat, "monthly_limit": rs(lim)})
        self._goal(ws, "Car down payment", 600000, 18, 15000, [120000] + [15000] * 6)
        self._goal(ws, "Emergency fund (3 months)", 450000, 24, 5000, [300000])
        self.data.scenarios.append({
            "workspace_id": ws, "name": "Cancel gym, eat out 30% less", "created_by": "bilal",
            "adjustments": [
                {"kind": "cancel_bill", "bill_id": self._bill_id(ws, "Gym")},
                {"kind": "scale_category", "category_id": "dining", "percent": -30},
            ],
        })
        self.data.notes.append({
            "workspace_id": ws, "coach_id": "coach_sara", "target": "budget:groceries",
            "text": "Groceries are close to the limit most months. A weekly cap of about Rs 11,500 may help.",
        })

    def _bill_id(self, ws: int, name: str) -> int:
        return next(b["id"] for b in self.data.bills if b["workspace_id"] == ws and b["name"] == name)

    def _refund(self, ws: int, payee: str) -> None:
        purchases = [t for t in self.data.transactions
                     if t["workspace_id"] == ws and t["payee"] == payee and t["amount"] < 0]
        if not purchases:
            return
        p = purchases[len(purchases) // 2]
        refund_day = min(p["posted_on"] + timedelta(days=6), self.as_of)
        self.txn(ws, refund_day, -p["amount"] // 2 // 100 * 100, payee,
                 f"{payee} refund - returned item", kind="refund", category=p["category_id"])

    def _goal(self, ws, name, target, months, plan, contribs) -> None:
        gid = len(self.data.goals) + 1
        self.data.goals.append({"id": gid, "workspace_id": ws, "name": name, "target": rs(target),
                                "deadline": add_months(self.as_of, months), "monthly_plan": rs(plan),
                                "created_on": self.start})
        for i, c in enumerate(contribs):
            self.data.contributions.append({"goal_id": gid, "on_date": add_months(self.start, i),
                                            "amount": rs(c), "note": "Seed contribution"})


def generate(as_of: date, seed: int = 2026) -> SeedData:
    g = _Gen(seed, as_of)
    g.student()
    g.household()
    g.data.transactions.sort(key=lambda t: (t["workspace_id"], t["posted_on"]))
    return g.data


# ------------------------------------------------------------ messy CSV --

def _fmt(d: date, style: str) -> str:
    if style == "iso":
        return d.isoformat()
    if style == "dmy":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    if style == "mdy":
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    if style == "dmon":
        return d.strftime("%d %b %Y").lstrip("0")
    raise ValueError(style)


def messy_csv(existing: list[dict], as_of: date, seed: int = 7) -> str:
    """A bank-style export with the problems from the Week 12 change request:
    duplicates, mixed date formats, negative refunds, plus >10% malformed rows
    and hostile text.  Spending is written as *positive* numbers, credits and
    refunds as negative, as many bank exports do."""
    import csv
    import io

    r = random.Random(seed)
    rows: list[list[str]] = []
    merchants = [("Mega Mart", 4000, 9000), ("Al-Noor Grocers", 600, 1800), ("PetroOne", 3500, 6000),
                 ("QuickBite Delivery", 900, 2000), ("Karahi House", 2500, 6000), ("Pharma Plus", 400, 2000),
                 ("Bazaar Online", 1500, 5000), ("RideNow", 300, 800), ("Sabzi Mandi", 800, 1800)]
    styles = ["iso", "dmy", "dmon", "dmy"]
    for i in range(30):
        name, lo, hi = r.choice(merchants)
        d = as_of - timedelta(days=r.randint(1, 20))
        amt = round(r.uniform(lo, hi) / 10) * 10
        rows.append([_fmt(d, styles[i % 4]), f"{name} - POS purchase", f"{amt:,}", ""])
    # Two US-style dates (only readable as month/day): a genuinely mixed file.
    for _ in range(2):
        d = as_of - timedelta(days=r.randint(1, 20))
        while d.day <= 12:
            d -= timedelta(days=1)
        rows.append([_fmt(d, "mdy"), "RideNow - trip", f"{round(r.uniform(300, 700) / 10) * 10}", ""])
    # Exact duplicates of transactions already in the ledger.
    hh = [t for t in existing if t["amount"] < 0 and t["kind"] == "normal"
          and as_of - timedelta(days=45) <= t["posted_on"] <= as_of - timedelta(days=15)]
    for t in r.sample(hh, min(4, len(hh))):
        rows.append([_fmt(t["posted_on"], "iso"), t["payee"], f"{-t['amount'] // 100}", ""])
    # A near-duplicate: same purchase exported with a date two days later.
    if hh:
        t = hh[-1]
        rows.append([_fmt(t["posted_on"] + timedelta(days=2), "iso"), t["payee"], f"{-t['amount'] // 100}", ""])
    # Duplicates inside the file itself.
    rows.append(list(rows[0]))
    rows.append(list(rows[5]))
    # Negative refunds and credits.
    bo = next((x for x in rows if x[1].startswith("Bazaar Online")), None)
    if bo:
        rows.append([bo[0], "REFUND - Bazaar Online order", f"-{int(bo[2].replace(',', '')) // 2}", ""])
    rows.append([_fmt(as_of - timedelta(days=3), "iso"), "Reversal QuickBite Delivery", "-650", ""])
    rows.append([_fmt(as_of - timedelta(days=2), "dmon"), "Freelance Design Client - payment received", "-20,000", "Other income"])
    # Malformed rows (well over 10% of the file once the extras below are added).
    rows.append(["31/02/2026", "Karahi House - dinner", "3,200", ""])
    rows.append(["yesterday", "Mega Mart - POS purchase", "5,400", ""])
    rows.append([_fmt(as_of - timedelta(days=6), "iso"), "Pharma Plus - medicines", "abc", ""])
    rows.append([_fmt(as_of - timedelta(days=7), "iso"), "Al-Noor Grocers", "", ""])
    rows.append([_fmt(as_of - timedelta(days=8), "iso"), "", "500", ""])
    rows.append([_fmt(as_of - timedelta(days=9), "iso"), "Karahi House", "Gulberg", "2500", ""])  # stray comma
    rows.append([_fmt(as_of - timedelta(days=10), "iso"), "PetroOne fuel", "1e5", ""])
    # Hostile and unusual text that should import safely.
    rows.append([_fmt(as_of - timedelta(days=5), "iso"), '<script>alert("hi")</script>Mega Mart', "2,150", ""])
    rows.append([_fmt(as_of - timedelta(days=4), "iso"), "☕ Chai Corner 😀", "350", ""])
    rows.append([_fmt(as_of - timedelta(days=11), "iso"), "Style Hub " + "very long description " * 15, "4,500", ""])
    # Something dated in the future.
    rows.append([_fmt(as_of + timedelta(days=20), "iso"), "Mega Mart - POS purchase", "6,100", ""])

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Txn Date", "Narration", "Amount (PKR)", "Category"])
    for row in rows:
        w.writerow(row)
    return out.getvalue()
