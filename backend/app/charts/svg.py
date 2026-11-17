"""Small SVG toolkit: scales, 'nice' ticks and safe text.

Coordinate system: SVG's origin is top-left and y grows downwards, so a
value scale maps the data domain [min, max] to the *inverted* range
[inner_height, 0].  Charts draw inside a <g transform="translate(left, top)">
so every mark can use plot coordinates without repeating the margin maths.
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass


def esc(text: object) -> str:
    """Escape any user-supplied text placed inside SVG/XML."""
    return html.escape(str(text), quote=True)


def nice_step(span: float, target_ticks: int = 5) -> float:
    """A 'human' tick step: 1, 2 or 5 × 10^k (e.g. 20,000 not 17,316)."""
    if span <= 0:
        return 1.0
    raw = span / max(target_ticks, 1)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def nice_domain(lo: float, hi: float, target_ticks: int = 5) -> tuple[float, float, list[float]]:
    """Expand [lo, hi] outward to round numbers and return the ticks."""
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    step = nice_step(hi - lo, target_ticks)
    nlo = math.floor(lo / step) * step
    nhi = math.ceil(hi / step) * step
    ticks = []
    v = nlo
    while v <= nhi + step / 2:
        ticks.append(round(v, 6))
        v += step
    return nlo, nhi, ticks


@dataclass
class LinearScale:
    d0: float
    d1: float
    r0: float
    r1: float

    def __call__(self, v: float) -> float:
        if self.d1 == self.d0:
            return (self.r0 + self.r1) / 2
        return self.r0 + (v - self.d0) / (self.d1 - self.d0) * (self.r1 - self.r0)

    def invert(self, px: float) -> float:
        if self.r1 == self.r0:
            return self.d0
        return self.d0 + (px - self.r0) / (self.r1 - self.r0) * (self.d1 - self.d0)


def path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    head, *rest = points
    return f"M{head[0]:.1f},{head[1]:.1f}" + "".join(f"L{x:.1f},{y:.1f}" for x, y in rest)


def area(top: list[tuple[float, float]], bottom: list[tuple[float, float]]) -> str:
    """Closed polygon between two lines (used for the best/worst band)."""
    if not top:
        return ""
    pts = top + list(reversed(bottom))
    return path(pts) + "Z"


def compact_pkr(paisa: float) -> str:
    """Axis labels: 'Rs 50k', 'Rs 1.2M', '-Rs 20k'."""
    r = paisa / 100
    sign = "-" if r < 0 else ""
    r = abs(r)
    if r >= 1_000_000:
        s = f"{r / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    elif r >= 1_000:
        s = f"{r / 1_000:.0f}k"
    else:
        s = f"{r:.0f}"
    return f"{sign}Rs {s}"
