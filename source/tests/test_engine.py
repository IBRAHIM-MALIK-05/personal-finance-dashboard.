"""Forecasting engine, recurring bills, budgets, goals and what-if."""
from datetime import date, timedelta

from finance.engine import scenarios as SC
from finance.engine.budgets import budget_status, free_to_spend, variance_table
from finance.engine.categories import Rule, categorise
from finance.engine.forecast import build_forecast, variable_assumptions
from finance.engine.goals import project
from finance.engine.models import Adjustment, Budget, Goal, RecurringBill, Txn
from finance.engine.recurring import match_occurrences, monthly_equivalent, occurrences

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
