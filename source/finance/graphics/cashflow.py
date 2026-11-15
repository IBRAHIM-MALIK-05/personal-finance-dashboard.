"""Cash-flow chart: actual history + forecast, drawn as SVG by code.

Visual encoding, chosen so that no meaning depends on colour alone:

* actual balance     -> solid line
* forecast (typical) -> dashed line, starting exactly at the 'as of' date
* best/worst range   -> lightly filled band with a diagonal hatch pattern
* what-if forecast   -> dotted, thicker line with a direct text label
* 'as of' boundary   -> vertical rule labelled with the date
* zero               -> heavier baseline; the y-axis always includes zero so
                        a fall in balance is never exaggerated (no truncated axis)
"""
from __future__ import annotations

import json
from datetime import date

from ..engine.dates import human
from ..engine.forecast import Forecast
from ..engine.money import format_pkr
from .svg import LinearScale, area, compact_pkr, esc, nice_domain, path

W, H = 760, 320
M = {"l": 74, "r": 110, "t": 18, "b": 40}


def cashflow_svg(fc: Forecast, *, compare: Forecast | None = None, chart_id: str = "cf",
                 compact: bool = False, title: str = "Balance: actual and forecast") -> str:
    h = 220 if compact else H
    iw, ih = W - M["l"] - M["r"], h - M["t"] - M["b"]

    hist = fc.history
    pts = fc.points
    days = [d for d, _ in hist] + [p.day for p in pts]
    if not days:
        return ""
    first, last = days[0], days[-1]
    x = LinearScale(0, max((last - first).days, 1), 0, iw)

    def X(d: date) -> float:
        return x((d - first).days)

    values = [b for _, b in hist] + [p.best for p in pts] + [p.worst for p in pts] + [0]
    if compare:
        values += [p.base for p in compare.points]
    lo, hi, ticks = nice_domain(min(values), max(values), 4 if compact else 5)
    y = LinearScale(lo, hi, ih, 0)

    out: list[str] = []
    desc = (
        f"Balance today {fc.balance_now.display}. Forecast {fc.closing.display} on {human(fc.end)} "
        f"(likely {format_pkr(pts[-1].worst)} to {format_pkr(pts[-1].best)}). "
        f"Lowest forecast {fc.lowest.display} on {human(fc.lowest_point.day)}."
    )
    out.append(
        f'<svg class="chart cashflow" viewBox="0 0 {W} {h}" role="img" '
        f'aria-labelledby="{chart_id}-t {chart_id}-d" preserveAspectRatio="xMidYMid meet">'
        f'<title id="{chart_id}-t">{esc(title)}</title><desc id="{chart_id}-d">{esc(desc)}</desc>'
        f'<defs><pattern id="{chart_id}-hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" class="hatch-line"/></pattern>'
        f'<clipPath id="{chart_id}-reveal"><rect class="reveal" x="{X(fc.as_of):.1f}" y="-5" '
        f'width="{iw - X(fc.as_of) + 5:.1f}" height="{ih + 10}"/></clipPath></defs>'
    )
    out.append(f'<g transform="translate({M["l"]},{M["t"]})">')

    # gridlines and y-axis labels
    for t in ticks:
        ty = y(t)
        cls = "zero" if t == 0 else "grid"
        out.append(f'<line class="{cls}" x1="0" x2="{iw}" y1="{ty:.1f}" y2="{ty:.1f}"/>')
        out.append(f'<text class="axis" x="-8" y="{ty + 4:.1f}" text-anchor="end">{esc(compact_pkr(t))}</text>')
    # x-axis: first day of each month
    d = date(first.year, first.month, 1)
    while d <= last:
        if d >= first:
            tx = X(d)
            out.append(f'<line class="tick" x1="{tx:.1f}" x2="{tx:.1f}" y1="{ih}" y2="{ih + 5}"/>')
            out.append(f'<text class="axis" x="{tx:.1f}" y="{ih + 20}" text-anchor="middle">{d.strftime("%b")}</text>')
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)

    # band (best..worst), forecast line, actual line
    top = [(X(fc.as_of), y(fc.balance_now.value))] + [(X(p.day), y(p.best)) for p in pts]
    bottom = [(X(fc.as_of), y(fc.balance_now.value))] + [(X(p.day), y(p.worst)) for p in pts]
    out.append(f'<g clip-path="url(#{chart_id}-reveal)">')
    out.append(f'<path class="band" d="{area(top, bottom)}"/>')
    out.append(f'<path class="band-hatch" d="{area(top, bottom)}" fill="url(#{chart_id}-hatch)"/>')
    base_line = [(X(fc.as_of), y(fc.balance_now.value))] + [(X(p.day), y(p.base)) for p in pts]
    out.append(f'<path class="forecast" d="{path(base_line)}"/>')
    if compare:
        cmp_line = [(X(fc.as_of), y(compare.balance_now.value))] + [(X(p.day), y(p.base)) for p in compare.points]
        out.append(f'<path class="scenario" d="{path(cmp_line)}"/>')
    out.append("</g>")
    out.append(f'<path class="actual" d="{path([(X(dd), y(b)) for dd, b in hist])}"/>')

    # events (bills/income) as small markers on the forecast line
    if not compact:
        for p in pts:
            if p.events:
                ex, ey = X(p.day), y(p.base)
                label = "; ".join(f"{e.label} {format_pkr(e.amount, signed=True)}" for e in p.events)
                shape = "M0,-5L5,0L0,5L-5,0Z"
                out.append(f'<path class="event" transform="translate({ex:.1f},{ey:.1f})" d="{shape}">'
                           f'<title>{esc(human(p.day))}: {esc(label)}</title></path>')

    # 'as of' boundary
    ax = X(fc.as_of)
    out.append(f'<line class="asof" x1="{ax:.1f}" x2="{ax:.1f}" y1="0" y2="{ih}"/>')
    out.append(f'<text class="asof-label" x="{ax + 4:.1f}" y="12">As of {esc(human(fc.as_of))}</text>')

    # direct labels at the right edge (instead of a colour-only legend)
    end_x = iw + 6
    labels = [(y(pts[-1].base), f"Forecast {compact_pkr(pts[-1].base)}", "lbl-forecast")]
    if compare:
        labels.append((y(compare.points[-1].base), f"What-if {compact_pkr(compare.points[-1].base)}", "lbl-scenario"))
    labels.sort()
    last_y = -100.0
    for ly, text, cls in labels:
        ly = max(ly, last_y + 14)
        out.append(f'<text class="end-label {cls}" x="{end_x}" y="{ly + 4:.1f}">{esc(text)}</text>')
        last_y = ly

    # keyboard / pointer cursor (moved by static/js/app.js)
    out.append(f'<g class="cursor" id="{chart_id}-cursor" visibility="hidden">'
               f'<line x1="0" x2="0" y1="0" y2="{ih}"/><circle r="5" cx="0" cy="0"/></g>')
    out.append(f'<rect class="hit" x="0" y="0" width="{iw}" height="{ih}"/>')
    out.append("</g></svg>")
    return "".join(out)


def chart_points_json(fc: Forecast, compare: Forecast | None = None, *, compact: bool = False) -> str:
    """Data the keyboard reader needs: plot x/y per day and a spoken label."""
    h = 220 if compact else H
    iw, ih = W - M["l"] - M["r"], h - M["t"] - M["b"]
    hist, pts = fc.history, fc.points
    first, last = (hist[0][0] if hist else fc.as_of), pts[-1].day
    x = LinearScale(0, max((last - first).days, 1), 0, iw)
    values = [b for _, b in hist] + [p.best for p in pts] + [p.worst for p in pts] + [0]
    if compare:
        values += [p.base for p in compare.points]
    lo, hi, _ = nice_domain(min(values), max(values), 4 if compact else 5)
    y = LinearScale(lo, hi, ih, 0)
    cmp = {p.day: p for p in compare.points} if compare else {}
    data = []
    for d, b in hist:
        data.append({"x": round(x((d - first).days), 1), "y": round(y(b), 1),
                     "t": f"{human(d)}. Actual balance {format_pkr(b)}."})
    for p in pts:
        t = (f"{human(p.day)}. Forecast {format_pkr(p.base)}, likely between "
             f"{format_pkr(p.worst)} and {format_pkr(p.best)}.")
        if p.day in cmp:
            t += f" What-if {format_pkr(cmp[p.day].base)}."
        if p.events:
            t += " " + "; ".join(f"{e.label} {format_pkr(e.amount, signed=True)}" for e in p.events) + "."
        data.append({"x": round(x((p.day - first).days), 1), "y": round(y(p.base), 1), "t": t})
    return json.dumps({"ml": M["l"], "mt": M["t"], "points": data})
