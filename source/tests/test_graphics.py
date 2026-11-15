"""Computer-graphics component: scales, ticks, zero baseline, escaping."""
import json
from datetime import date

from finance.engine.budgets import budget_status
from finance.engine.forecast import build_forecast, variable_assumptions
from finance.engine.models import Budget, Txn
from finance.graphics.bars import budget_bar_svg
from finance.graphics.cashflow import cashflow_svg, chart_points_json
from finance.graphics.svg import LinearScale, compact_pkr, esc, nice_domain, nice_step

AS_OF = date(2026, 11, 15)


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
