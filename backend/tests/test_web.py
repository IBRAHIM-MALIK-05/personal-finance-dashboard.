"""End-to-end through the Flask test client: journeys, roles, CSRF, states."""
import re

import pytest

from app import create_app

from .conftest import AS_OF, login

PAGES = ["/", "/budgets", "/bills", "/goals", "/forecast", "/forecast?horizon=90", "/whatif",
         "/export", "/history", "/settings", "/help"]


@pytest.mark.parametrize("profile", ["ayesha", "bilal", "sana", "coach_sara"])
@pytest.mark.parametrize("lens", ["summary", "standard", "detailed"])
def test_every_page_renders_for_every_role_and_lens(client, profile, lens):
    login(client, profile)
    client.post("/prefs", data={"lens": lens, "next": "/"})
    for url in PAGES:
        r = client.get(url)
        assert r.status_code == 200, url
        assert b'lang="en"' in r.data and b"skip-link" in r.data


def test_switch_profile_page_lists_profiles_when_already_signed_in(client):
    login(client, "bilal")
    page = client.get("/profiles").get_data(as_text=True)
    assert 'name="profile_id" value="ayesha"' in page and 'value="coach_sara"' in page


def test_unauthenticated_requests_go_to_profiles(client):
    assert client.get("/").status_code == 302


def test_coach_is_read_only(client):
    login(client, "coach_sara")
    assert client.get("/transactions").status_code == 403
    assert client.get("/import").status_code == 403
    r = client.post("/budgets", data={"category_id": "dining", "monthly_limit": "1"})
    assert r.status_code == 403


def test_csrf_blocks_forged_posts(tmp_path):
    app = create_app({"DATABASE_URL": f"sqlite:///{(tmp_path / 'c.db').as_posix()}", "AS_OF": AS_OF,
                      "SECRET_KEY": "x"})
    c = app.test_client()
    c.get("/profiles")
    assert c.post("/profiles", data={"profile_id": "bilal"}).status_code == 400
    page = c.get("/profiles").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    assert c.post("/profiles", data={"profile_id": "bilal", "_csrf": token}).status_code == 302


def test_core_journey_correct_category_then_explain(client):
    login(client, "ayesha")
    r = client.get("/transactions?q=book+bazaar+cafe")
    tid = int(re.search(rb'id="t-(\d+)"', r.data).group(1))
    ver = re.search(rb'name="version" value="(\d+)"', r.data).group(1).decode()
    r = client.post(f"/transactions/{tid}/category", data={"category_id": "dining", "version": ver})
    assert b"Change the category" in r.data  # asks about similar transactions first
    r = client.post(f"/transactions/{tid}/category",
                    data={"category_id": "dining", "version": ver, "apply": "similar"}, follow_redirects=True)
    assert b"future ones will use this category" in r.data
    dash = client.get("/").data
    assert b"How is this worked out?" in dash and b"Formula:" in dash


def test_import_journey_with_messy_sample_then_undo(client):
    login(client, "bilal")
    r = client.post("/import", data={"sample": "1"})
    draft = int(re.search(r"/import/(\d+)/map", r.headers["Location"]).group(1))
    r = client.post(f"/import/{draft}/map", data={"col_date": "0", "col_description": "1", "col_amount": "2",
                                                  "col_category": "3", "sign": "positive_is_out", "date_order": "DMY"})
    assert r.headers["Location"].endswith(f"/import/{draft}/review")
    page = client.get(f"/import/{draft}/review?filter=all").data
    assert b"Not ready to import yet" in page
    client.post(f"/import/{draft}/bulk", data={"action": "skip_errors"})
    client.post(f"/import/{draft}/bulk", data={"action": "skip_exact_duplicates"})
    client.post(f"/import/{draft}/bulk", data={"action": "keep_in_file_duplicates"})
    page = client.get(f"/import/{draft}/review?filter=needs_decision").get_data(as_text=True)
    for row in set(re.findall(r'id="row-(\d+)"', page)):
        client.post(f"/import/{draft}/row/{row}", data={"action": "skip"})
    r = client.post(f"/import/{draft}/commit")
    assert r.status_code == 302 and "/import/batch/" in r.headers["Location"]
    batch_page = client.get(r.headers["Location"]).data
    assert b"transactions imported" in batch_page and b"Skipped rows" in batch_page
    bid = int(r.headers["Location"].split("/")[-1].split("?")[0])
    assert b"Undo the import" in client.get(f"/import/batch/{bid}/undo").data
    r = client.post(f"/import/batch/{bid}/undo", data={"mode": "full"}, follow_redirects=True)
    assert b"Import undone" in r.data


def test_bad_upload_shows_recovery_message(client):
    login(client, "bilal")
    from io import BytesIO
    r = client.post("/import", data={"file": (BytesIO("x".encode("utf-16")), "bank.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 422 and b"CSV UTF-8" in r.data


def test_invalid_manual_entry_keeps_input_and_explains(client):
    login(client, "ayesha")
    r = client.post("/transactions/new", data={"posted_on": "2026-11-14", "amount": "abc", "payee": "Chai",
                                               "direction": "out"})
    assert r.status_code == 400 and b"Please fix" in r.data and b'value="Chai"' in r.data


def test_storage_failure_page(client):
    login(client, "bilal")
    client.post("/settings/storage", data={"simulate": "1"})
    r = client.post("/budgets", data={"category_id": "dining", "monthly_limit": "15000"})
    assert r.status_code == 503 and b"was not saved" in r.data
    client.post("/settings/storage", data={"simulate": "0"})


def test_empty_state(client):
    login(client, "ayesha")
    client.post("/settings/clear", data={"confirm": "CLEAR"})
    dash = client.get("/").data
    assert b"Get started" in dash and b"Add your transactions" in dash and b"0 of 4 done" in dash
    assert b"Nothing to forecast yet" in client.get("/forecast").data


def test_what_if_journey(client):
    login(client, "bilal")
    client.post("/whatif/load/1")
    page = client.get("/whatif").data
    assert b"Difference on" in page and b"What-if (dotted)" in page
    client.post("/whatif/save", data={"name": "Copy"})
    assert b"Copy" in client.get("/whatif").data


def test_export_csv_is_formula_safe(client):
    login(client, "bilal")
    client.post("/transactions/new", data={"posted_on": "2026-11-14", "amount": "10", "payee": "=cmd|' /C calc'!A0",
                                           "direction": "out"})
    r = client.post("/export", data={"kind": "transactions"})
    assert r.mimetype == "text/csv" and b"'=cmd" in r.data
    assert r.data.startswith("﻿".encode())  # UTF-8 byte-order mark for Excel
