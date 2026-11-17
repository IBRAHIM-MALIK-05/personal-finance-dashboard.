"""People: sign up, log in, soft delete, budget details, display preferences, coach sharing.

Passwords are stored only as salted hashes (Werkzeug's ``generate_password_hash``).
Each new account gets its own private, empty personal budget.
"""
from __future__ import annotations

import re
import secrets
from datetime import date

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection
from werkzeug.security import check_password_hash, generate_password_hash

from ..database import Database, tables as T
from ..engine.dates import DateParseError, parse_flexible
from ..engine.money import AmountError, format_pkr, parse_amount
from ..engine.validation import sanitize_text
from .core import Actor, Forbidden, ValidationFailed, audit, now, require
from .demo_data import system_rules


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


MIN_PASSWORD = 8


MAX_PASSWORD = 128


# Compared against when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def _starting_balance(form: dict, today: date, errors: dict) -> tuple[int, date | None]:
    """Balance in the account on a given day.  Transactions before that day are
    treated as already included in it, so nothing is counted twice."""
    try:
        balance = parse_amount(form.get("opening_balance") or "0")
    except AmountError as e:
        errors["opening_balance"] = str(e)
        balance = 0
    try:
        on = parse_flexible(form.get("opening_date") or today.isoformat(), "DMY").value
        if on is None:
            raise DateParseError("Use a date like 2026-11-01.")
        if on > today:
            errors["opening_date"] = "Choose today or an earlier date."
    except DateParseError as e:
        errors["opening_date"] = str(e)
        on = None
    return balance, on


def create_account(db: Database, form: dict, today: date) -> str:
    """Create a profile, its personal budget and membership.  Returns the new profile id."""
    errors: dict[str, str] = {}
    name = sanitize_text(form.get("name", ""), 80).value
    email = normalise_email(form.get("email", ""))
    password = form.get("password", "")
    if not name:
        errors["name"] = "Enter your name."
    if not EMAIL_RE.match(email) or len(email) > 254:
        errors["email"] = "Enter a valid email address, like name@example.com."
    if len(password) < MIN_PASSWORD:
        errors["password"] = f"Use at least {MIN_PASSWORD} characters."
    elif len(password) > MAX_PASSWORD:
        errors["password"] = f"Use at most {MAX_PASSWORD} characters."
    elif password != form.get("confirm_password", ""):
        errors["confirm_password"] = "The two passwords don’t match."
    balance, opening_date = _starting_balance(form, today, errors)
    if errors:
        raise ValidationFailed(errors, form)

    profile_id = "u_" + secrets.token_hex(8)
    ts = now()
    with db.write() as conn:
        if conn.execute(select(T.profiles.c.id).where(T.profiles.c.email == email)).first():
            raise ValidationFailed({"email": "An account with this email already exists. Log in instead."}, form)
        conn.execute(insert(T.profiles).values(
            id=profile_id, display_name=name, role="owner", blurb="", email=email,
            password_hash=generate_password_hash(password), is_demo=False, created_at=ts))
        ws_id = conn.execute(insert(T.workspaces).values(
            name=f"{name.split()[0]}’s budget"[:80], kind="personal", currency="PKR",
            opening_balance=balance, opening_date=opening_date, is_demo=False,
            created_at=ts)).inserted_primary_key[0]
        conn.execute(insert(T.memberships).values(profile_id=profile_id, workspace_id=ws_id, permission="edit"))
        conn.execute(insert(T.rules), system_rules(ws_id))
        conn.execute(insert(T.audit_log).values(workspace_id=ws_id, profile_id=profile_id, at=ts,
                                                action="account.create", detail="Account and budget created"))
    return profile_id


def authenticate(conn: Connection, email: str, password: str) -> str | None:
    """Return the profile id for a correct email + password, otherwise None."""
    row = conn.execute(select(T.profiles.c.id, T.profiles.c.password_hash).where(
        T.profiles.c.email == normalise_email(email), ~T.profiles.c.is_demo)).first()
    if row is None or not row.password_hash:
        check_password_hash(_DUMMY_HASH, password)
        return None
    return row.id if check_password_hash(row.password_hash, password) else None


def is_demo_profile(conn: Connection, profile_id: str) -> bool:
    return bool(conn.execute(select(T.profiles.c.is_demo).where(T.profiles.c.id == profile_id)).scalar())


def budget_details(conn: Connection, actor: Actor) -> dict:
    row = conn.execute(select(T.workspaces).where(T.workspaces.c.id == actor.workspace_id)).first()
    return dict(row._mapping)


def update_budget_details(db: Database, actor: Actor, form: dict, today: date) -> None:
    """Rename the budget and correct its starting balance."""
    require(actor, "edit")
    errors: dict[str, str] = {}
    name = sanitize_text(form.get("name", ""), 80).value
    if not name:
        errors["name"] = "Give the budget a name."
    balance, opening_date = _starting_balance(form, today, errors)
    if errors:
        raise ValidationFailed(errors, form)
    with db.write() as conn:
        conn.execute(update(T.workspaces).where(T.workspaces.c.id == actor.workspace_id).values(
            name=name, opening_balance=balance, opening_date=opening_date))
        audit(conn, actor, "budget.details",
              f"Budget details: {name}, starting balance {format_pkr(balance)} on {opening_date}")


def delete_account(db: Database, actor: Actor, password: str, confirmation: str) -> None:
    """Soft delete: sets ``is_deleted = 1`` (and ``deleted_at``) on the profile and its budget and
    records the account in the ``deleted_accounts`` table.
    The account can no longer log in, but every record stays in the database, and the email is
    moved to ``deleted_email`` so the address can sign up again.  Restoring or permanently
    deleting is done in the database: see database/account_admin.sql."""
    if actor.is_demo:
        raise Forbidden("Demo profiles can’t be deleted. Create your own account to try this.")
    errors: dict[str, str] = {}
    if confirmation.strip() != "DELETE":
        errors["confirm"] = "Type DELETE to confirm."
    with db.read() as conn:
        stored = conn.execute(select(T.profiles.c.password_hash).where(T.profiles.c.id == actor.profile_id)).scalar()
    if not stored or not check_password_hash(stored, password):
        errors["password"] = "That password is not right."
    if errors:
        raise ValidationFailed(errors)

    ts = now()
    with db.write() as conn:
        # Read the email first: MySQL applies SET assignments left to right, so copying a column
        # and clearing it in one UPDATE would lose the value there (SQLite would not show the bug).
        email = conn.execute(select(T.profiles.c.email).where(T.profiles.c.id == actor.profile_id)).scalar()
        conn.execute(update(T.profiles).where(T.profiles.c.id == actor.profile_id, ~T.profiles.c.is_deleted)
                     .values(is_deleted=True, deleted_at=ts, deleted_email=email, email=None))
        owned = select(T.memberships.c.workspace_id).where(T.memberships.c.profile_id == actor.profile_id)
        conn.execute(update(T.workspaces).where(T.workspaces.c.id.in_(owned), ~T.workspaces.c.is_demo)
                     .values(is_deleted=True, deleted_at=ts))
        conn.execute(insert(T.deleted_accounts).values(
            profile_id=actor.profile_id, workspace_id=actor.workspace_id, name=actor.name, email=email,
            deleted_at=ts))
        audit(conn, actor, "account.delete", "Account deleted by its owner (data kept)")


def count_accounts(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(T.profiles).where(~T.profiles.c.is_demo)).scalar_one()


DEFAULT_LENS = {"owner": "summary", "manager": "standard", "coach": "detailed"}


def get_prefs(conn: Connection, profile_id: str, role: str) -> dict:
    r = conn.execute(select(T.preferences).where(T.preferences.c.profile_id == profile_id)).first()
    if r:
        return dict(r._mapping)
    return {"profile_id": profile_id, "lens": DEFAULT_LENS[role], "theme": "system",
            "reduced_motion": "system", "horizon": 30}


def set_prefs(db: Database, profile_id: str, role: str, **changes) -> dict:
    allowed = {"lens": ("summary", "standard", "detailed"), "theme": ("system", "light", "dark", "contrast"),
               "reduced_motion": ("system", "on", "off"), "horizon": (30, 60, 90)}
    with db.engine.begin() as conn:  # preferences still save during the storage-failure demo
        prefs = get_prefs(conn, profile_id, role)
        for k, v in changes.items():
            if k == "horizon":
                v = int(v) if str(v).isdigit() else 30
            if k in allowed and v in allowed[k]:
                prefs[k] = v
        exists = conn.execute(select(T.preferences.c.profile_id).where(T.preferences.c.profile_id == profile_id)).first()
        values = {k: prefs[k] for k in ("lens", "theme", "reduced_motion", "horizon")}
        if exists:
            conn.execute(update(T.preferences).where(T.preferences.c.profile_id == profile_id).values(**values))
        else:
            conn.execute(insert(T.preferences).values(profile_id=profile_id, **values))
    return prefs


def get_meta(conn: Connection, key: str, default: str = "") -> str:
    v = conn.execute(select(T.meta.c.value).where(T.meta.c.key == key)).scalar()
    return default if v is None else v


def set_meta(db: Database, key: str, value: str) -> None:
    with db.engine.begin() as conn:  # bypasses the failure simulation on purpose
        if conn.execute(select(T.meta.c.key).where(T.meta.c.key == key)).first():
            conn.execute(update(T.meta).where(T.meta.c.key == key).values(value=value))
        else:
            conn.execute(insert(T.meta).values(key=key, value=value))


def coach_clients(conn: Connection, coach_id: str) -> list[dict]:
    """For the demo coach's home page: the demo households, and whether each is shared."""
    rows = []
    for ws in conn.execute(select(T.workspaces).where(T.workspaces.c.kind == "household", T.workspaces.c.is_demo)):
        g = conn.execute(
            select(T.sharing_grants)
            .where(T.sharing_grants.c.workspace_id == ws.id, T.sharing_grants.c.coach_id == coach_id)
            .order_by(T.sharing_grants.c.granted_at.desc())
        ).first()
        rows.append({"workspace": dict(ws._mapping), "grant": dict(g._mapping) if g else None})
    return rows


def current_grant(conn: Connection, workspace_id: int, coach_id: str = "coach_sara") -> dict | None:
    r = conn.execute(select(T.sharing_grants).where(
        T.sharing_grants.c.workspace_id == workspace_id, T.sharing_grants.c.coach_id == coach_id,
        T.sharing_grants.c.revoked_at.is_(None))).first()
    return dict(r._mapping) if r else None


def set_sharing(db: Database, actor: Actor, scope: str | None, coach_id: str = "coach_sara") -> None:
    """scope = 'summary' | 'full' | None (revoke).  Each change is a new grant
    record, so the history of consent is kept."""
    require(actor, "manage_sharing")
    if scope not in (None, "summary", "full"):
        raise ValidationFailed({"scope": "Choose what to share."})
    with db.write() as conn:
        conn.execute(update(T.sharing_grants).where(
            T.sharing_grants.c.workspace_id == actor.workspace_id, T.sharing_grants.c.coach_id == coach_id,
            T.sharing_grants.c.revoked_at.is_(None)).values(revoked_at=now()))
        if scope:
            conn.execute(insert(T.sharing_grants).values(workspace_id=actor.workspace_id, coach_id=coach_id,
                                                         scope=scope, granted_by=actor.profile_id, granted_at=now()))
        audit(conn, actor, "sharing.change", f"Coach access: {scope or 'revoked'}")


def add_note(db: Database, actor: Actor, target: str, text: str) -> None:
    require(actor, "note")
    clean = sanitize_text(text, 600).value
    if not clean:
        raise ValidationFailed({"text": "Write a note first."})
    with db.write() as conn:
        conn.execute(insert(T.coach_notes).values(workspace_id=actor.workspace_id, coach_id=actor.profile_id,
                                                  target=sanitize_text(target, 40).value or "general",
                                                  text=clean, created_at=now()))
        audit(conn, actor, "note.add", f"Coach note on {target}")

