"""CSV import pipeline (Week 12 change request) and hostile input (card E6)."""
from datetime import date

import pytest

from finance.engine import importer as I
from finance.engine.categories import SYSTEM_RULES, Rule
from finance.engine.models import Txn
from finance.engine.validation import csv_safe, sanitize_text, validate_transaction
from finance.seed import generate, messy_csv

AS_OF = date(2026, 11, 15)
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


# ------------------------------------------------- hostile input (card E6) --

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
