"""'Explain this number'.

Every figure the engine shows to a person is an :class:`Explained` value: the
number *plus* the formula, the inputs that went into it and the assumptions it
rests on.  The UI renders the same object as a plain number (summary lens) or
with the working shown (detailed lens) - one model, two presentations, which is
how this project resolves the beginner-vs-coach conflict hurdle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .dates import human
from .money import format_pkr

Kind = Literal["actual", "forecast", "mixed", "plan"]
Unit = Literal["pkr", "percent", "date", "count", "days", "text"]

KIND_LABELS = {
    "actual": "Actual",
    "forecast": "Forecast",
    "mixed": "Actual + forecast",
    "plan": "Your plan",
}


@dataclass(frozen=True)
class Input:
    label: str
    value: str
    kind: Kind = "actual"


@dataclass(frozen=True)
class Explained:
    key: str
    label: str
    value: int | float | date | str | None
    unit: Unit
    kind: Kind
    formula: str
    inputs: tuple[Input, ...] = field(default_factory=tuple)
    assumptions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def display(self) -> str:
        return fmt(self.value, self.unit)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


def fmt(value, unit: Unit) -> str:
    if value is None:
        return "—"
    if unit == "pkr":
        return format_pkr(int(value))
    if unit == "percent":
        return f"{value:.0f}%"
    if unit == "date":
        return human(value)
    if unit == "days":
        return f"{value} day" + ("" if value == 1 else "s")
    if unit == "count":
        return f"{value:,}"
    return str(value)


def money_input(label: str, paisa: int, kind: Kind = "actual") -> Input:
    return Input(label, format_pkr(paisa), kind)
