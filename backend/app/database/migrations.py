"""Keep an existing database up to date when the app starts.

1. ``upgrade_schema``  adds missing tables and columns, never touching existing data.
   A new column must be optional (NULL) or have a server default (e.g. ``is_deleted`` 0);
   anything else needs a deliberate migration and is reported instead of guessed.
2. ``BACKFILL``        one-off data fixes that run right after a column is added.
3. ``install_admin_sql`` (MySQL only) creates the views and stored procedures in
   ``sql/account_admin.sql`` that administrators use in MySQL Workbench.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from .connection import Database
from .tables import metadata

ADMIN_SQL = Path(__file__).resolve().parents[2] / "sql" / "account_admin.sql"

# Run once, right after the column is added to an existing database.
BACKFILL = {
    # Accounts soft-deleted before the is_deleted flag existed only had deleted_at set.
    "profiles.is_deleted": "UPDATE profiles SET is_deleted = 1 WHERE deleted_at IS NOT NULL",
    "workspaces.is_deleted": "UPDATE workspaces SET is_deleted = 1 WHERE deleted_at IS NOT NULL",
}


def _column_ddl(db: Database, column) -> str:
    ddl = f"{column.name} {column.type.compile(dialect=db.engine.dialect)}"
    if column.server_default is not None:
        ddl += f" DEFAULT {column.server_default.arg.text}"
    return ddl + (" NULL" if column.nullable else " NOT NULL")


def upgrade_schema(db: Database) -> list[str]:
    """Add what newer versions of ``tables.py`` define.  Returns e.g. ``["profiles.is_deleted"]``."""
    metadata.create_all(db.engine)  # creates missing tables only
    existing = inspect(db.engine)
    added: list[str] = []
    with db.engine.begin() as conn:
        for table in metadata.sorted_tables:
            have = {c["name"] for c in existing.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(f"{table.name}.{column.name} is new, required and has no default; "
                                       "rebuild with `python run.py --reset` or migrate by hand.")
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(db, column)}"))
                added.append(f"{table.name}.{column.name}")
        # Backfills run after *every* column exists, since one may read another added just now.
        for name in added:
            if name in BACKFILL:
                conn.execute(text(BACKFILL[name]))
    return added


def split_sql_script(script: str) -> list[str]:
    """Split a MySQL script into statements, honouring ``DELIMITER`` lines the way
    MySQL Workbench does (needed for stored procedures, whose bodies contain ``;``)."""
    statements, current, delimiter = [], [], ";"
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(None, 1)[1]
            continue
        if not current and (not stripped or stripped.startswith("--")):
            continue  # comments between statements
        current.append(line)
        if stripped.endswith(delimiter):
            statement = "\n".join(current).rstrip()[: -len(delimiter)].strip()
            if statement:
                statements.append(statement)
            current = []
    if "".join(current).strip():
        statements.append("\n".join(current).strip())
    return statements


def install_admin_sql(db: Database) -> int:
    """Create or replace the admin views and procedures (MySQL only).  Returns how many statements ran."""
    if not db.is_mysql:
        return 0
    statements = split_sql_script(ADMIN_SQL.read_text(encoding="utf-8"))
    with db.engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
    return len(statements)


def migrate(db: Database) -> list[str]:
    """Everything an existing database needs at start-up.  Returns what changed."""
    changes = [f"added {c}" for c in upgrade_schema(db)]
    try:
        install_admin_sql(db)
    except Exception as exc:  # the app works without the admin tools; say why they're missing
        changes.append(f"admin views/procedures not installed ({type(exc).__name__}: {exc})")
    return changes
