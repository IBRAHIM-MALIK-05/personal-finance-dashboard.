"""Database schema: every table, defined once with SQLAlchemy Core.

Only portable constructs are used, so the same schema runs on MySQL 8 (the app)
and SQLite (the tests).  The app creates the tables itself; MySQL Workbench shows them under
hci_finance -> Tables.  Money columns hold paisa (BIGINT).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index,
    Integer, MetaData, String, Table, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects import mysql

metadata = MetaData()
LongText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

profiles = Table(
    "profiles", metadata,
    Column("id", String(32), primary_key=True),
    Column("display_name", String(80), nullable=False),
    Column("role", String(16), nullable=False),  # owner | manager | coach
    Column("blurb", String(200), nullable=False, default=""),
    # Real accounts log in with email + password; demo personas have neither.
    Column("email", String(254), nullable=True, unique=True),
    Column("password_hash", String(255), nullable=True),
    Column("is_demo", Boolean, nullable=False, default=False),
    Column("created_at", DateTime, nullable=True),
    # Soft delete.  is_deleted: 0 = active, 1 = deleted by its owner.  The account can't log in,
    # but every record is kept; the email moves to deleted_email so the address can sign up again.
    # Restore / permanent delete happen in the database: see database/account_admin.sql.
    Column("is_deleted", Boolean, nullable=False, default=False, server_default=text("0")),
    Column("deleted_at", DateTime, nullable=True),
    Column("deleted_email", String(254), nullable=True),
    CheckConstraint("role IN ('owner','manager','coach')", name="ck_profile_role"),
)

workspaces = Table(
    "workspaces", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(80), nullable=False),
    Column("kind", String(16), nullable=False),  # personal | household
    Column("currency", String(3), nullable=False, default="PKR"),
    Column("opening_balance", BigInteger, nullable=False),
    Column("opening_date", Date, nullable=False),
    Column("is_demo", Boolean, nullable=False, default=False),  # demo budgets can be reset; real ones never
    Column("created_at", DateTime, nullable=False),
    Column("is_deleted", Boolean, nullable=False, default=False, server_default=text("0")),  # 1 = owner deleted it
    Column("deleted_at", DateTime, nullable=True),
)

# One row per soft-deleted account: the place to check which accounts are deleted.
# Written in the same transaction that sets profiles.is_deleted = 1; the row is removed when the
# account is restored or permanently purged (see database/account_admin.sql).
deleted_accounts = Table(
    "deleted_accounts", metadata,
    Column("profile_id", String(32), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True),
    Column("name", String(80), nullable=False),
    Column("email", String(254), nullable=True),
    Column("deleted_at", DateTime, nullable=False),
)

memberships = Table(
    "memberships", metadata,
    Column("profile_id", String(32), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True),
    Column("permission", String(8), nullable=False),  # edit | view
)

sharing_grants = Table(
    "sharing_grants", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("coach_id", String(32), ForeignKey("profiles.id"), nullable=False),
    Column("scope", String(8), nullable=False),  # summary | full
    Column("granted_by", String(32), ForeignKey("profiles.id"), nullable=False),
    Column("granted_at", DateTime, nullable=False),
    Column("revoked_at", DateTime, nullable=True),
)

categories = Table(
    "categories", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(60), nullable=False),
    Column("grp", String(16), nullable=False),
)

rules = Table(
    "rules", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("pattern", String(80), nullable=False),
    Column("category_id", String(32), ForeignKey("categories.id"), nullable=False),
    Column("priority", Integer, nullable=False, default=0),
    Column("source", String(8), nullable=False),  # system | user
    Column("created_at", DateTime, nullable=False),
)

recurring_bills = Table(
    "recurring_bills", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(80), nullable=False),
    Column("payee", String(80), nullable=False),
    Column("category_id", String(32), ForeignKey("categories.id"), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("cadence", String(10), nullable=False),
    Column("anchor_date", Date, nullable=False),
    Column("tolerance_pct", Integer, nullable=False, default=10),
    Column("end_date", Date, nullable=True),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    CheckConstraint("amount <> 0", name="ck_bill_amount"),
    CheckConstraint("cadence IN ('weekly','monthly','quarterly','yearly')", name="ck_bill_cadence"),
)

transactions = Table(
    "transactions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("posted_on", Date, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("payee", String(80), nullable=False),
    Column("description", String(160), nullable=False, default=""),
    Column("category_id", String(32), ForeignKey("categories.id"), nullable=False),
    Column("category_source", String(8), nullable=False, default="rule"),
    Column("kind", String(10), nullable=False, default="normal"),
    Column("recurring_id", Integer, ForeignKey("recurring_bills.id", ondelete="SET NULL"), nullable=True),
    Column("occurrence_date", Date, nullable=True),
    Column("refund_of", Integer, nullable=True),
    Column("import_batch_id", Integer, nullable=True),
    Column("raw_row", Text, nullable=True),
    Column("source_mapping", Text, nullable=True),
    Column("note", String(600), nullable=False, default=""),
    Column("created_by", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    CheckConstraint("amount <> 0", name="ck_txn_amount"),
    CheckConstraint("kind IN ('normal','refund','transfer')", name="ck_txn_kind"),
)
Index("ix_txn_ws_date", transactions.c.workspace_id, transactions.c.posted_on)
Index("ix_txn_batch", transactions.c.import_batch_id)

budgets = Table(
    "budgets", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("category_id", String(32), ForeignKey("categories.id"), nullable=False),
    Column("monthly_limit", BigInteger, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    UniqueConstraint("workspace_id", "category_id", name="uq_budget_ws_cat"),
    CheckConstraint("monthly_limit > 0", name="ck_budget_limit"),
)

goals = Table(
    "goals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(80), nullable=False),
    Column("target", BigInteger, nullable=False),
    Column("deadline", Date, nullable=False),
    Column("monthly_plan", BigInteger, nullable=False, default=0),
    Column("created_on", Date, nullable=False),
    Column("version", Integer, nullable=False, default=1),
)

goal_contributions = Table(
    "goal_contributions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("goal_id", Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=False),
    Column("on_date", Date, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("note", String(200), nullable=False, default=""),
    Column("transfer_txn_id", Integer, nullable=True),
)

scenarios = Table(
    "scenarios", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(80), nullable=False),
    Column("adjustments_json", Text, nullable=False),
    Column("created_by", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

import_drafts = Table(
    "import_drafts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", String(32), nullable=False),
    Column("file_name", String(200), nullable=False),
    Column("content", LongText, nullable=False),
    Column("settings_json", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

import_batches = Table(
    "import_batches", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("profile_id", String(32), nullable=False),
    Column("file_name", String(200), nullable=False),
    Column("imported_at", DateTime, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("skipped_count", Integer, nullable=False),
    Column("status", String(10), nullable=False),  # committed | undone
    Column("undone_at", DateTime, nullable=True),
    Column("summary_json", Text, nullable=False),
)

import_journal = Table(
    "import_journal", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", Integer, ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False),
    Column("table_name", String(32), nullable=False),
    Column("record_id", Integer, nullable=False),
    Column("op", String(8), nullable=False),  # insert | delete | restore
    Column("before_json", LongText, nullable=True),
    Column("after_version", Integer, nullable=True),
    Column("at", DateTime, nullable=False),
)

coach_notes = Table(
    "coach_notes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
    Column("coach_id", String(32), ForeignKey("profiles.id"), nullable=False),
    Column("target", String(40), nullable=False),
    Column("text", String(600), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

preferences = Table(
    "preferences", metadata,
    Column("profile_id", String(32), ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True),
    Column("lens", String(10), nullable=False),
    Column("theme", String(10), nullable=False, default="system"),
    Column("reduced_motion", String(6), nullable=False, default="system"),
    Column("horizon", Integer, nullable=False, default=30),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workspace_id", Integer, nullable=True),
    Column("profile_id", String(32), nullable=False),
    Column("at", DateTime, nullable=False),
    Column("action", String(40), nullable=False),
    Column("detail", String(300), nullable=False),
)

meta = Table(
    "meta", metadata,
    Column("key", String(40), primary_key=True),
    Column("value", Text, nullable=False),
)
