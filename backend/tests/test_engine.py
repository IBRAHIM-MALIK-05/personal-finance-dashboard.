"""Calculations without a database: money, dates, forecast, budgets, goals, what-if,
CSV import analysis, input validation and the SVG charts.
"""
from datetime import date, timedelta
import json

import pytest

from app.charts.bars import budget_bar_svg
from app.charts.cashflow import cashflow_svg, chart_points_json
from app.charts.svg import LinearScale, compact_pkr, esc, nice_domain, nice_step
from app.database.seed import generate, messy_csv
from app.engine import importer as I
from app.engine import scenarios as SC
from app.engine.budgets import budget_status, free_to_spend, variance_table
from app.engine.categories import SYSTEM_RULES, Rule, categorise
from app.engine.dates import DateParseError, add_months, order_evidence, parse_flexible
from app.engine.forecast import build_forecast, variable_assumptions
from app.engine.goals import project
from app.engine.models import Adjustment, Budget, Goal, RecurringBill, Txn
from app.engine.money import AmountError, format_pkr, parse_amount
from app.engine.recurring import match_occurrences, monthly_equivalent, occurrences
from app.engine.validation import csv_safe, sanitize_text, validate_transaction


# ================================================================================================
# test_money_dates.py
# ================================================================================================


@pytest.mark.parametrize("text, paisa", [
    ("1,200", 120000),
    ("Rs. 1,200.50", 120050),
    ("PKR 500", 50000),
    ("-850", -85000),
    ("(850)", -85000),
    ("850-", -85000),
    ("1,20,000", 12000000),       # Pakistani lakh grouping
    ("۱۲۳", 12300),               # Urdu digits
    ("Rs -99.5", -9950),
])
def test_parse_amount_accepts_real_world_formats(text, paisa):
    assert parse_amount(text) == paisa


@pytest.mark.parametrize("text", ["", "abc", "1e5", "NaN", "inf", "12.345", "--5", "100000001"])
def test_parse_amount_rejects_bad_input(text):
    with pytest.raises(AmountError):
        parse_amount(text)


def test_integer_paisa_never_drift():
    # 0.1 + 0.2 style float errors must be impossible
    assert sum(parse_amount("0.10") for _ in range(3)) == 30
    assert format_pkr(30) == "Rs 0.30"


def test_format_pkr():
    assert format_pkr(-123456) == "-Rs 1,234.56"
    assert format_pkr(500000, signed=True) == "+Rs 5,000"
    assert format_pkr(0) == "Rs 0"


def test_month_end_clamping():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)   # leap year
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)   # from anchor, no drift


def test_ambiguous_dates_are_never_guessed():
    p = parse_flexible("04/05/2026")
    assert p.ambiguous and p.value is None
    assert set(p.candidates) == {date(2026, 5, 4), date(2026, 4, 5)}
    assert parse_flexible("04/05/2026", "DMY").value == date(2026, 5, 4)
    assert parse_flexible("04/05/2026", "MDY").value == date(2026, 4, 5)


def test_unambiguous_numeric_dates_carry_evidence():
    assert parse_flexible("25/10/2026").value == date(2026, 10, 25)
    assert parse_flexible("25/10/2026").evidence == "DMY"
    assert parse_flexible("10/25/2026").evidence == "MDY"
    assert parse_flexible("3 Nov 2026").value == date(2026, 11, 3)
    assert parse_flexible("Nov 3, 2026").value == date(2026, 11, 3)


@pytest.mark.parametrize("bad", ["31/02/2026", "yesterday", "2026-13-01", "", "00/00/0000"])
def test_invalid_dates(bad):
    with pytest.raises(DateParseError):
        parse_flexible(bad)


def test_order_evidence_counts():
    ev = order_evidence(["25/10/2026", "13/11/2026", "10/30/2026", "04/05/2026", "2026-11-01", "junk"])
    assert ev == {"DMY": 2, "MDY": 1, "ambiguous": 1, "unambiguous_other": 1, "invalid": 1}


# ================================================================================================
# Forecasting engine, recurring bills, budgets, goals and what-if.
# ================================================================================================


AS_OF = date(2026, 11, 15)


def rent(anchor=date(2026, 6, 5)):
    return RecurringBill(1, "Rent", "Landlord", "housing", -5_000_000, "monthly", anchor)


def salary():
    return RecurringBill(2, "Salary", "Payroll", "salary", 10_000_000, "monthly", date(2026, 6, 1))


def history(months=4, daily_food=100_000):
    """Simple ledger: daily food spending, rent and salary paid and linked."""
    txns, d = [], date(2026, 7, 1)
    while d <= AS_OF:
        txns.append(Txn(None, d, -daily_food, "Mega Mart", category_id="groceries"))
        d += timedelta(days=1)
    for b in (rent(), salary()):
        for due in occurrences(b, date(2026, 7, 1), AS_OF):
            txns.append(Txn(None, due, b.amount, b.payee, category_id=b.category_id,
                            recurring_id=b.id, occurrence_date=due))
    return txns


def test_occurrences_follow_anchor_and_clamp():
    b = RecurringBill(1, "X", "x", "housing", -100, "monthly", date(2026, 1, 31))
    assert occurrences(b, date(2026, 1, 1), date(2026, 4, 30)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    q = RecurringBill(1, "School", "s", "education", -100, "quarterly", date(2026, 2, 8))
    assert occurrences(q, date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 2, 8), date(2026, 5, 8), date(2026, 8, 8), date(2026, 11, 8)]
    w = RecurringBill(1, "W", "w", "dining", -100, "weekly", date(2026, 11, 2))
    assert occurrences(w, date(2026, 11, 10), date(2026, 11, 30)) == [
        date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 30)]


def test_monthly_equivalent():
    assert monthly_equivalent(-1200, "yearly") == -100
    assert monthly_equivalent(-300, "quarterly") == -100
    assert monthly_equivalent(-1200, "weekly") == -5200


def test_linked_payment_is_posted_but_lookalike_is_only_suggested():
    b = rent()
    linked = Txn(1, date(2026, 11, 5), -5_000_000, "Landlord", recurring_id=1, occurrence_date=date(2026, 11, 5))
    lookalike = Txn(2, date(2026, 10, 6), -5_000_000, "Landlord")
    occ = {o.due: o for o in match_occurrences([b], [linked, lookalike], date(2026, 10, 1), date(2026, 11, 30), AS_OF)}
    assert occ[date(2026, 11, 5)].status == "posted"
    assert occ[date(2026, 10, 5)].status == "suggested"


def test_forecast_does_not_double_count_posted_bills():
    txns = history()
    fc = build_forecast(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns,
                        bills=[rent(), salary()], as_of=AS_OF, horizon_days=30)
    labels = [e.label for e in fc.events]
    # November rent (5 Nov) and salary (1 Nov) are posted: only December ones are projected
    assert labels.count("Rent") == 1 and labels.count("Salary") == 1
    assert all(e.day > AS_OF for e in fc.events)


def test_forecast_arithmetic_and_actual_forecast_boundary():
    txns = history(daily_food=100_000)  # Rs 1,000/day groceries
    fc = build_forecast(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns,
                        bills=[rent(), salary()], as_of=AS_OF, horizon_days=30)
    a = fc.assumption_set
    assert a.months_available == 3 and not a.limited
    groceries = a.by_category()["groceries"]
    # Aug/Sep/Oct have 31/30/31 days at Rs 1,000/day -> median Rs 31,000
    assert groceries.base == 3_100_000 and groceries.low == 3_000_000 and groceries.high == 3_100_000
    assert fc.history[-1][0] == AS_OF and fc.points[0].day == AS_OF + timedelta(days=1)
    expected = fc.balance_now.value + 10_000_000 - 5_000_000 - 3_100_000 * 12 / 365 * 30
    assert abs(fc.closing.value - expected) <= 100  # rounded to the rupee
    assert fc.points[-1].best >= fc.points[-1].base >= fc.points[-1].worst


def test_bills_do_not_inflate_variable_spending():
    txns = history()
    a = variable_assumptions(txns, [rent(), salary()], AS_OF)
    assert "housing" not in a.by_category()


def test_limited_history_is_flagged():
    txns = [Txn(None, AS_OF - timedelta(days=i), -50_000, "Cafe", category_id="dining") for i in range(10)]
    fc = build_forecast(opening_balance=100_000, opening_date=AS_OF - timedelta(days=10), txns=txns,
                        bills=[], as_of=AS_OF, horizon_days=30)
    assert fc.limited
    assert fc.assumptions[0].startswith("LIMITED ESTIMATE")
    override = variable_assumptions(txns, [], AS_OF, overrides={"__total__": 2_000_000})
    assert not override.limited and override.total("base") == 2_000_000


def test_budget_status_refund_reduces_spending_and_projection():
    txns = [
        Txn(None, date(2026, 11, 2), -1_000_000, "Style Hub", category_id="shopping"),
        Txn(None, date(2026, 11, 9), 400_000, "Style Hub", category_id="shopping", kind="refund"),
    ]
    a = variable_assumptions(txns, [], AS_OF)
    s = budget_status(Budget(1, "shopping", 500_000), txns, [], a, AS_OF)
    assert s.spent.value == 600_000 and s.status == "over"
    assert s.remaining.value == -100_000


def test_variance_compares_with_median_month():
    txns = history()
    rows = {r.category_id: r for r in variance_table(txns, AS_OF)}
    assert rows["groceries"].typical == 3_100_000
    assert rows["groceries"].this_month == 1_500_000


def test_free_to_spend_counts_only_unposted_items():
    txns = history()
    goal = Goal(1, "Laptop", 10_000_000, date(2027, 6, 1), 600_000, saved=0)
    e = free_to_spend(1_000_000, txns, [rent(), salary()], [goal], AS_OF)
    # Nothing recurring is still due between 15 and 30 Nov; only the goal plan is subtracted.
    assert e.value == 1_000_000 - 600_000


def test_goal_projection():
    g = Goal(1, "Laptop", 12_000_000, date(2027, 7, 15), 600_000, saved=3_000_000)
    p = project(g, AS_OF)
    assert p.months_left == 8 and p.required_monthly.value == 1_125_000
    assert not p.on_track
    assert p.projected_finish.value == date(2028, 2, 15)  # 15 months at Rs 6,000


def test_what_if_is_isolated_and_cancel_saves_exact_amount():
    txns = history()
    bills = [rent(), salary()]
    base = build_forecast(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns, bills=bills,
                          as_of=AS_OF, horizon_days=30)
    alt = SC.run(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns, bills=bills, as_of=AS_OF,
                 horizon_days=30, assumptions=base.assumption_set,
                 adjustments=[Adjustment("cancel_bill", bill_id=1)])
    assert alt.closing.value - base.closing.value == 5_000_000
    assert bills[0].active  # original untouched
    delta = SC.compare(base, alt, [Adjustment("cancel_bill", bill_id=1)], bills)
    assert delta.value == 5_000_000


def test_what_if_scale_category():
    txns = history()
    base = build_forecast(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns, bills=[],
                          as_of=AS_OF, horizon_days=30)
    alt = SC.run(opening_balance=0, opening_date=date(2026, 7, 1), txns=txns, bills=[], as_of=AS_OF,
                 horizon_days=30, assumptions=base.assumption_set,
                 adjustments=[Adjustment("scale_category", category_id="groceries", percent=-50)])
    groceries = base.assumption_set.by_category()["groceries"].base
    assert alt.variable.value == base.variable.value - groceries // 2


def test_user_rule_beats_system_rule():
    rules = [Rule(1, "book bazaar", "education"), Rule(2, "book bazaar cafe", "dining", 10, "user")]
    assert categorise("Book Bazaar Cafe", "", rules)[0] == "dining"
    assert categorise("Book Bazaar", "textbooks", rules)[0] == "education"
    assert categorise("Unknown Shop", "", rules)[0] == "uncategorised"


# ================================================================================================
# CSV import pipeline (Week 12 change request) and hostile input (card E6).
# ================================================================================================


RULES = [Rule(None, p, c) for p, c in SYSTEM_RULES]


HEADER = "Txn Date,Narration,Amount (PKR),Category\n"


def analyse(body, existing=(), **settings):
    return I.analyse(HEADER + body, I.Settings(**settings), list(existing), RULES, AS_OF)


def test_mapping_and_sign_are_guessed_but_must_be_confirmed():
    a = analyse("2026-11-01,Mega Mart - POS,2500,\n2026-11-02,QuickBite,900,\n2026-11-02,PetroOne,4000,\n"
                "2026-11-03,Salary payroll,-90000,\n")
    assert a.mapping == {"date": 0, "description": 1, "amount": 2, "category": 3}
    assert a.sign_guess == "positive_is_out"
    assert a.sign is None and not a.can_commit  # never applied silently


def test_negative_refund_in_positive_spending_file_becomes_inflow_linked_to_purchase():
    a = analyse("2026-11-01,Bazaar Online - order,3000,\n2026-11-04,REFUND - Bazaar Online order,-1000,\n",
                sign="positive_is_out")
    purchase, refund = a.rows
    assert purchase.amount == -300000
    assert refund.amount == 100000 and refund.kind == "refund"
    assert refund.refund_of_row == purchase.row_no and refund.category_id == purchase.category_id == "shopping"


def test_mixed_and_ambiguous_dates_need_a_decision():
    a = analyse("25/10/2026,Mega Mart,100,\n04/05/2026,Mega Mart,200,\n10/30/2026,RideNow,300,\n", sign="positive_is_out")
    r1, r2, r3 = a.rows
    assert r1.posted_on == date(2026, 10, 25)
    assert r2.status == "needs_decision" and r2.has("DATE_AMBIGUOUS")
    assert r3.posted_on == date(2026, 10, 30)
    a2 = analyse("04/05/2026,Mega Mart,200,\n", sign="positive_is_out", row_dates={2: "2026-04-05"})
    assert a2.rows[0].posted_on == date(2026, 4, 5) and a2.rows[0].status == "ready"


def test_duplicates_are_candidates_not_silent_skips():
    existing = [Txn(1, date(2026, 10, 20), -150000, "PetroOne")]
    a = analyse("2026-10-20,PetroOne,1500,\n2026-10-22,PetroOne,1500,\n2026-11-01,Chai Corner,200,\n2026-11-01,Chai Corner,200,\n",
                existing, sign="positive_is_out")
    codes = [[i.code for i in r.issues if i.severity == "decision"] for r in a.rows]
    assert codes == [["DUPLICATE_EXISTING"], ["POSSIBLE_DUPLICATE"], [], ["DUPLICATE_IN_FILE"]]
    assert [r.status for r in a.rows] == ["needs_decision", "needs_decision", "ready", "needs_decision"]
    # Explicit decisions unblock the commit; the second coffee can be a real purchase
    a = analyse("2026-11-01,Chai Corner,200,\n2026-11-01,Chai Corner,200,\n", sign="positive_is_out",
                decisions={3: "import"})
    assert a.can_commit and len(a.to_import) == 2


def test_malformed_rows_block_commit_until_fixed_or_skipped():
    body = "31/02/2026,Karahi House,3200,\n2026-11-03,Pharma Plus,abc,\n2026-11-04,,500,\n2026-11-05,Karahi House,Gulberg,2500,\n2026-11-06,Mega Mart,900,\n"
    a = analyse(body, sign="positive_is_out")
    assert a.counts["error"] == 4 and not a.can_commit
    a = analyse(body, sign="positive_is_out", decisions=I.skip_errors(a))
    assert a.can_commit and a.counts == {"total": 5, "ready": 1, "needs_decision": 0, "error": 0, "skipped": 4}
    fixed = analyse(body, sign="positive_is_out", overrides={5: {"description": "Karahi House, Gulberg", "amount": "2500"}})
    assert fixed.rows[3].status == "ready" and fixed.rows[3].amount == -250000


def test_fatal_files_explain_how_to_recover():
    with pytest.raises(I.ImportFatal, match="UTF-8"):
        I.read_csv("Datum\n".encode("utf-16"))
    with pytest.raises(I.ImportFatal, match="empty"):
        I.read_csv(b"")
    with pytest.raises(I.ImportFatal, match="no transactions"):
        I.read_csv(b"Date,Amount\n")
    with pytest.raises(I.ImportFatal, match="limit"):
        I.read_csv(b"x" * (I.MAX_BYTES + 1))


def test_generated_messy_file_has_every_problem_from_the_change_request():
    data = generate(AS_OF)
    hh = [t for t in data.transactions if t["workspace_id"] == 2]
    existing = [Txn(i, t["posted_on"], t["amount"], t["payee"], kind=t["kind"]) for i, t in enumerate(hh)]
    text = messy_csv(hh, AS_OF)
    a = I.analyse(text, I.Settings(sign="positive_is_out", date_order="DMY"), existing, RULES, AS_OF)
    codes = {i.code for r in a.rows for i in r.issues}
    assert {"DUPLICATE_EXISTING", "DUPLICATE_IN_FILE", "POSSIBLE_DUPLICATE", "REFUND_MATCHED",
            "DATE_INVALID", "AMOUNT_INVALID", "COLUMN_COUNT", "TEXT_CLEANED", "DATE_OTHER_ORDER"} <= codes
    assert a.counts["error"] / a.counts["total"] >= 0.10  # at least 10% malformed (card E2)


HOSTILE = [
    ('<script>alert("x")</script>Mega Mart', "Mega Mart"),
    ("<img src=x onerror=alert(1)>", ""),
    ("A" * 10_000, None),                       # truncated
    ("☕ Chai Corner 😀", "☕ Chai Corner 😀"),   # emoji kept intact
    ("   ", ""),
    ("Shop‮evil", "Shopevil"),             # bidi override removed
    ("Tab\tand\nnewline", "Tab and newline"),
    ("Robert'); DROP TABLE transactions;--", "Robert'); DROP TABLE transactions;--"),  # harmless: parameterised SQL
]


@pytest.mark.parametrize("raw, expected", HOSTILE)
def test_sanitize_text(raw, expected):
    out = sanitize_text(raw)
    if expected is None:
        assert len(out.value) == 120 and out.value.endswith("…") and out.changed
    else:
        assert out.value == expected


def test_emoji_never_split_when_truncating():
    out = sanitize_text("😀" * 200, 10)
    assert out.value == "😀" * 9 + "…"


def test_csv_formula_injection_is_neutralised():
    assert csv_safe("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert csv_safe("Mega Mart") == "Mega Mart"


@pytest.mark.parametrize("form, field", [
    ({"posted_on": "2026-11-01", "amount": "", "payee": "X"}, "amount"),
    ({"posted_on": "2026-11-01", "amount": "0", "payee": "X"}, "amount"),
    ({"posted_on": "2026-11-01", "amount": "-50", "payee": "X"}, "amount"),
    ({"posted_on": "2026-11-01", "amount": "1e9", "payee": "X"}, "amount"),
    ({"posted_on": "2029-01-01", "amount": "50", "payee": "X"}, "posted_on"),
    ({"posted_on": "not a date", "amount": "50", "payee": "X"}, "posted_on"),
    ({"posted_on": "2026-11-01", "amount": "50", "payee": "<b></b>"}, "payee"),
    ({"posted_on": "2026-11-01", "amount": "50", "payee": "X", "category_id": "hacked"}, "category_id"),
])
def test_transaction_form_rejects_absurd_input(form, field):
    res = validate_transaction(form, AS_OF)
    assert field in res.errors


def test_derive_payee_strips_reference_numbers():
    """Regression: the pattern held backspace characters instead of word boundaries, so it never matched."""
    assert I.derive_payee("INV 123 Landlord") == "Landlord"
    assert I.derive_payee("TXN 123 Mega Mart") == "Mega Mart"
    assert I.derive_payee("Order #4471 QuickBite Delivery") == "QuickBite Delivery"


# ================================================================================================
# Computer-graphics component: scales, ticks, zero baseline, escaping.
# ================================================================================================


def test_nice_ticks():
    assert nice_step(17316, 5) == 5000
    lo, hi, ticks = nice_domain(-1234, 98765, 5)
    assert lo == -20000 and hi == 100000 and ticks[0] == lo and ticks[-1] == hi and 0 in ticks


def test_linear_scale_inverts_y():
    y = LinearScale(0, 100, 200, 0)   # SVG y grows downward
    assert y(0) == 200 and y(100) == 0 and y.invert(100) == 50


def test_compact_labels():
    assert compact_pkr(5_000_000) == "Rs 50k" and compact_pkr(-120_000_000) == "-Rs 1.2M"


def _fc():
    txns = [Txn(None, date(2026, 9, 1 + i % 28), -100_000, "Shop", category_id="groceries") for i in range(60)]
    return build_forecast(opening_balance=5_000_000, opening_date=date(2026, 9, 1), txns=txns, bills=[],
                          as_of=AS_OF, horizon_days=30)


def test_cashflow_chart_is_accessible_and_zero_based():
    svg = cashflow_svg(_fc(), chart_id="t")
    assert 'role="img"' in svg and "<title" in svg and "<desc" in svg
    assert 'class="zero"' in svg          # y-axis includes zero: no truncated axis
    assert 'class="actual"' in svg and 'class="forecast"' in svg and 'class="band"' in svg
    assert "As of 15 Nov 2026" in svg
    data = json.loads(chart_points_json(_fc()))
    assert len(data["points"]) == len(_fc().history) + 30


def test_svg_text_is_escaped():
    assert esc('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"


def test_budget_bar_shows_over_with_pattern_and_text():
    txns = [Txn(None, date(2026, 11, 3), -900_000, "Shop", category_id="dining")]
    s = budget_status(Budget(1, "dining", 500_000), txns, [], variable_assumptions(txns, [], AS_OF), AS_OF)
    svg = budget_bar_svg(s, "x")
    assert 'class="overflow"' in svg and "Over budget" in svg
