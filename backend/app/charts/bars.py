"""Budget bars and the category variance chart.

Same visual language as the cash-flow chart: solid = actual, dashed outline =
forecast.  Over-budget spending also gets a hatch pattern and a text status,
so 'over' is never signalled by red alone.  Every bar starts at zero.
"""
from __future__ import annotations

from ..engine.budgets import BudgetStatus, VarianceRow
from ..engine.money import format_pkr
from .svg import LinearScale, compact_pkr, esc, nice_domain

BW, BH = 400, 30


def budget_bar_svg(s: BudgetStatus, bar_id: str) -> str:
    limit = s.budget.monthly_limit
    spent = max(s.spent.value, 0)
    projected = max(s.projected.value, spent)
    x = LinearScale(0, max(limit, spent, projected) * 1.05 or 1, 0, BW)
    over = spent > limit
    sentence = (f"{s.category_name}: spent {format_pkr(spent)} of {format_pkr(limit)} "
                f"({s.pct_used}%). Forecast for the month {format_pkr(projected)}. {s.status_text}.")
    parts = [
        f'<svg class="bar" viewBox="0 0 {BW} {BH}" preserveAspectRatio="none" role="img" aria-label="{esc(sentence)}">',
        f'<defs><pattern id="{bar_id}-over" width="6" height="6" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><rect width="6" height="6" class="over-bg"/>'
        f'<line x1="0" y1="0" x2="0" y2="6" class="over-line"/></pattern></defs>',
        f'<rect class="track" x="0" y="6" width="{x(limit):.1f}" height="18" rx="3"/>',
        f'<rect class="fill status-{s.status}" x="0" y="6" width="{x(min(spent, limit)):.1f}" height="18" rx="3"/>',
    ]
    if over:
        parts.append(f'<rect x="{x(limit):.1f}" y="6" width="{x(spent) - x(limit):.1f}" height="18" '
                     f'fill="url(#{bar_id}-over)" class="overflow"/>')
    if projected > spent:
        parts.append(f'<rect class="projected" x="{x(spent):.1f}" y="7" width="{x(projected) - x(spent):.1f}" '
                     f'height="16" rx="2"/>')
    parts.append(f'<line class="limit" x1="{x(limit):.1f}" x2="{x(limit):.1f}" y1="1" y2="29"/>')
    parts.append("</svg>")
    return "".join(parts)


def variance_svg(rows: list[VarianceRow], chart_id: str = "var") -> str:
    """This month so far (bar) vs a typical full month (tick mark)."""
    if not rows:
        return ""
    row_h, label_w, w = 30, 170, 760
    iw = w - label_w - 90
    h = row_h * len(rows) + 30
    _, hi, ticks = nice_domain(0, max(max(r.this_month, r.typical) for r in rows), 4)
    x = LinearScale(0, hi, 0, iw)
    out = [f'<svg class="chart variance" viewBox="0 0 {w} {h}" role="img" aria-labelledby="{chart_id}-t">'
           f'<title id="{chart_id}-t">Spending this month so far compared with a typical month, by category</title>'
           f'<g transform="translate({label_w},8)">']
    for t in ticks:
        out.append(f'<line class="grid" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="0" y2="{row_h * len(rows)}"/>')
        out.append(f'<text class="axis" x="{x(t):.1f}" y="{row_h * len(rows) + 16}" text-anchor="middle">'
                   f'{esc(compact_pkr(t))}</text>')
    for i, r in enumerate(rows):
        y0 = i * row_h
        out.append(f'<text class="row-label" x="-10" y="{y0 + 19}" text-anchor="end">{esc(r.name)}</text>')
        out.append(f'<rect class="fill" x="0" y="{y0 + 7}" width="{x(max(r.this_month, 0)):.1f}" height="16" rx="2"/>')
        tx = x(r.typical)
        out.append(f'<line class="typical" x1="{tx:.1f}" x2="{tx:.1f}" y1="{y0 + 3}" y2="{y0 + 27}"/>')
        out.append(f'<text class="value" x="{x(max(r.this_month, r.typical)) + 8:.1f}" y="{y0 + 19}">'
                   f'{esc(format_pkr(r.this_month))}</text>')
    out.append("</g></svg>")
    return "".join(out)
