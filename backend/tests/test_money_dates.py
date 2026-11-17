from datetime import date

import pytest

from app.engine.dates import DateParseError, add_months, order_evidence, parse_flexible
from app.engine.money import AmountError, format_pkr, parse_amount


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
