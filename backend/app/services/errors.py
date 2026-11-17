"""Exceptions raised by services; ``app/errors.py`` turns them into friendly pages."""
from __future__ import annotations


class Forbidden(Exception):
    """The current profile may not do this.  Shown as a 403 page."""


class NotFound(Exception):
    pass


class ValidationFailed(Exception):
    def __init__(self, errors: dict[str, str], values: dict | None = None):
        super().__init__("; ".join(errors.values()))
        self.errors = errors
        self.values = values or {}


class EditConflict(Exception):
    """Someone else saved this record after you opened it (E3 hook)."""

    def __init__(self, table: str, record_id: int, current: dict | None, attempted: dict):
        super().__init__(f"{table} {record_id} changed since you opened it")
        self.table, self.record_id, self.current, self.attempted = table, record_id, current, attempted


class ImportBlocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__(" ".join(reasons))
        self.reasons = reasons

