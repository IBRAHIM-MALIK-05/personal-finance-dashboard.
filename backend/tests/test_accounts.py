"""Real accounts: sign up, log in / out, privacy between accounts and from the demo."""
from sqlalchemy import select

from app.database import tables as T

from .conftest import AS_OF, login

PASSWORD = "correct horse 42"


def signup(client, name="Zara Ahmed", email="zara@example.com", password=PASSWORD, **extra):
    data = {"name": name, "email": email, "password": password, "confirm_password": password,
            "opening_balance": "25,000", "opening_date": AS_OF.isoformat(), **extra}
    return client.post("/signup", data=data)


def add_txn(client, payee, amount="500"):
    return client.post("/transactions/new", data={"posted_on": AS_OF.isoformat(), "amount": amount,
                                                  "payee": payee, "direction": "out"})


def test_signup_creates_a_private_empty_budget(client, db):
    r = signup(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    page = client.get("/").get_data(as_text=True)
    assert "Zara’s budget" in page and "Get started" in page and "0 of 4 done" in page
    assert "Rs 25,000" in page  # starting balance is today's balance
    assert "Log out" in page and "Switch profile" not in page
    with db.read() as conn:
        row = conn.execute(select(T.profiles).where(T.profiles.c.email == "zara@example.com")).first()
    assert row.password_hash and PASSWORD not in row.password_hash and not row.is_demo


def test_new_budget_categorises_automatically(client):
    signup(client)
    add_txn(client, "Mega Mart")
    assert "Groceries" in client.get("/transactions").get_data(as_text=True)


def test_signup_validation_keeps_input_but_never_the_password(client):
    r = signup(client, name="", email="not-an-email", password="short")
    page = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "Enter your name." in page and "valid email" in page and "at least 8 characters" in page
    assert 'value="not-an-email"' in page and "short" not in page.split("<form", 1)[1].split("</form>")[0].replace(
        "8 characters", "")
    r = client.post("/signup", data={"name": "Zara", "email": "z@example.com", "password": PASSWORD,
                                     "confirm_password": "something else"})
    assert r.status_code == 400 and "don’t match" in r.get_data(as_text=True)


def test_duplicate_email_is_rejected(client, app):
    signup(client)
    other = app.test_client()
    r = signup(other, name="Someone Else", email="ZARA@example.com ")
    assert r.status_code == 400 and "already exists" in r.get_data(as_text=True)


def test_login_and_logout(client, app):
    signup(client)
    client.post("/logout")
    assert client.get("/").status_code == 302  # back to the log-in page
    r = client.post("/login", data={"email": "zara@example.com", "password": "wrong password"})
    assert r.status_code == 401 and "don’t match" in r.get_data(as_text=True)
    r = client.post("/login", data={"email": " Zara@Example.com", "password": PASSWORD})
    assert r.status_code == 302
    assert "Zara’s budget" in client.get("/").get_data(as_text=True)


def test_accounts_cannot_see_each_other(client, app):
    signup(client)
    add_txn(client, "Private Payee One")
    other = app.test_client()
    signup(other, name="Omar", email="omar@example.com")
    page = other.get("/transactions").get_data(as_text=True)
    assert "Private Payee One" not in page and "0 in this budget" in page


def test_real_accounts_are_not_reachable_through_the_demo(client, app, db):
    signup(client)
    with db.read() as conn:
        zara_id = conn.execute(select(T.profiles.c.id).where(T.profiles.c.email == "zara@example.com")).scalar()
    visitor = app.test_client()
    assert "Zara" not in visitor.get("/profiles").get_data(as_text=True)
    r = visitor.post("/profiles", data={"profile_id": zara_id})
    assert r.status_code == 302 and visitor.get("/").status_code == 302  # refused, still signed out
    login(visitor, "coach_sara")
    assert "Zara" not in visitor.get("/coach").get_data(as_text=True)


def test_real_accounts_cannot_share_with_the_demo_coach(client):
    signup(client)
    assert "Sharing with a financial coach" not in client.get("/settings").get_data(as_text=True)
    assert client.post("/settings/sharing", data={"scope": "full"}).status_code == 403


def test_demo_reset_keeps_real_accounts(client, app):
    signup(client)
    add_txn(client, "Kept After Reset")
    demo = app.test_client()
    login(demo, "bilal")
    assert demo.post("/settings/reset", data={"confirm": "RESET"}).status_code == 302
    assert "Kept After Reset" in client.get("/transactions").get_data(as_text=True)
    assert client.post("/settings/reset", data={"confirm": "RESET"}).status_code == 403  # not for real budgets


def test_storage_failure_test_only_affects_one_browser(client, app):
    signup(client)
    demo = app.test_client()
    login(demo, "bilal")
    demo.post("/settings/storage", data={"simulate": "1"})
    assert add_txn(demo, "Fails").status_code == 503
    assert add_txn(client, "Still Saves").status_code == 302


def test_budget_details_can_be_corrected(client):
    signup(client)
    r = client.post("/settings/budget", data={"name": "Home money", "opening_balance": "40000",
                                              "opening_date": AS_OF.isoformat()})
    assert r.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Home money" in page and "Rs 40,000" in page
    r = client.post("/settings/budget", data={"name": "", "opening_balance": "abc", "opening_date": "2099-01-01"})
    assert r.status_code == 400


# ------------------------------------------------------------- delete account --

def delete(client, password=PASSWORD, confirm="DELETE"):
    return client.post("/settings/account/delete", data={"password": password, "confirm": confirm})


def test_delete_account_needs_password_and_confirmation(client):
    signup(client)
    r = delete(client, password="wrong password", confirm="delete please")
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "password is not right" in page and "Type DELETE" in page
    assert client.get("/").status_code == 200  # still signed in, nothing changed


def test_delete_account_is_soft(client, app, db):
    signup(client)
    add_txn(client, "Kept For Records")
    r = delete(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert client.get("/").status_code == 302  # logged out
    with db.read() as conn:
        p = conn.execute(select(T.profiles).where(T.profiles.c.deleted_email == "zara@example.com")).first()
        ws = conn.execute(select(T.workspaces).join(T.memberships, T.memberships.c.workspace_id == T.workspaces.c.id)
                          .where(T.memberships.c.profile_id == p.id)).first()
        payees = [r.payee for r in conn.execute(select(T.transactions.c.payee).where(T.transactions.c.workspace_id == ws.id))]
    assert p.is_deleted and p.deleted_at is not None and p.email is None and p.password_hash  # kept, login removed
    assert ws.is_deleted and ws.deleted_at is not None and "Kept For Records" in payees  # data kept
    with db.read() as conn:
        listed = conn.execute(select(T.deleted_accounts).where(T.deleted_accounts.c.profile_id == p.id)).first()
    assert listed.email == "zara@example.com" and listed.workspace_id == ws.id and listed.name == "Zara Ahmed"
    r = client.post("/login", data={"email": "zara@example.com", "password": PASSWORD})
    assert r.status_code == 401  # can't log in any more


def test_deleted_email_can_sign_up_again_with_an_empty_budget(client, app):
    signup(client)
    add_txn(client, "Old Life")
    delete(client)
    fresh = app.test_client()
    assert signup(fresh).status_code == 302
    assert "Old Life" not in fresh.get("/transactions").get_data(as_text=True)


def test_deleting_signs_out_other_browsers(client, app):
    signup(client)
    other = app.test_client()
    other.post("/login", data={"email": "zara@example.com", "password": PASSWORD})
    assert other.get("/").status_code == 200
    delete(client)
    r = other.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")


def test_demo_profiles_cannot_be_deleted(client):
    login(client, "bilal")
    assert "Delete account" not in client.get("/settings").get_data(as_text=True)
    assert delete(client, password="", confirm="DELETE").status_code == 403


def _old_database(tmp_path):
    """A database from before soft delete existed: no is_deleted / deleted_* columns."""
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE profiles (id VARCHAR(32) PRIMARY KEY, display_name VARCHAR(80) NOT NULL, "
                       "role VARCHAR(16) NOT NULL, blurb VARCHAR(200) NOT NULL, email VARCHAR(254), "
                       "password_hash VARCHAR(255), is_demo BOOLEAN NOT NULL, created_at DATETIME)"))
        c.execute(text("INSERT INTO profiles VALUES ('u_1', 'Old User', 'owner', '', 'old@example.com', 'h', 0, NULL)"))
    eng.dispose()
    return url


def test_upgrade_adds_new_columns_without_losing_data(tmp_path):
    from sqlalchemy import inspect, text

    from app.database import Database
    from app.database.migrations import upgrade_schema
    db = Database(_old_database(tmp_path))
    added = upgrade_schema(db)
    assert {"table deleted_accounts", "profiles.is_deleted", "profiles.deleted_at",
            "profiles.deleted_email"} <= set(added)
    assert {"is_deleted", "deleted_at", "deleted_email"} <= {c["name"] for c in inspect(db.engine).get_columns("profiles")}
    with db.read() as c:
        row = c.execute(text("SELECT display_name, is_deleted FROM profiles")).first()
    assert row.display_name == "Old User" and row.is_deleted == 0  # existing accounts start as active
    assert upgrade_schema(db) == []  # running again changes nothing


def test_upgrade_backfills_is_deleted_for_older_soft_deletes(tmp_path):
    """Accounts soft-deleted before the flag existed (deleted_at only) are marked is_deleted = 1."""
    from sqlalchemy import create_engine, text

    from app.database import Database
    from app.database.migrations import upgrade_schema
    url = _old_database(tmp_path)
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE profiles ADD COLUMN deleted_at DATETIME"))
        c.execute(text("ALTER TABLE profiles ADD COLUMN deleted_email VARCHAR(254)"))
        c.execute(text("INSERT INTO profiles VALUES ('u_2', 'Gone', 'owner', '', NULL, 'h', 0, NULL, "
                       "'2026-11-01 10:00:00', 'gone@example.com')"))
    eng.dispose()
    db = Database(url)
    upgrade_schema(db)
    with db.read() as c:
        flags = dict(c.execute(text("SELECT id, is_deleted FROM profiles")).all())
        listed = c.execute(text("SELECT profile_id, email FROM deleted_accounts")).all()
    assert flags == {"u_1": 0, "u_2": 1}
    assert [tuple(r) for r in listed] == [("u_2", "gone@example.com")]  # recorded in the new table


def test_admin_sql_script_splits_into_whole_statements():
    """The Workbench script (with DELIMITER) is also what the app installs, so it must parse cleanly."""
    from app.database.migrations import ADMIN_SQL, split_sql_script
    statements = split_sql_script(ADMIN_SQL.read_text(encoding="utf-8"))
    creates = [s.split("(")[0].split()[-1] for s in statements if s.startswith("CREATE PROCEDURE")]
    views = [s.split()[4] for s in statements if s.startswith("CREATE OR REPLACE VIEW")]
    assert views == ["accounts_overview", "deleted_account_transactions"]  # deleted_accounts is a real table
    assert creates == ["restore_account", "purge_account_rows", "purge_account", "purge_deleted_accounts"]
    assert all(s.rstrip().endswith("END") for s in statements if s.startswith("CREATE PROCEDURE"))
    assert not any(s.startswith(("DELIMITER", "USE")) for s in statements)
