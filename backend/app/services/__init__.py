"""Application services: the only code that reads and writes the database.

Every public function takes an :class:`Actor` and checks permission on the
server before touching data.  Writes go through ``Database.write()``, one atomic
transaction each.  Routes import this package as ``S`` and call ``S.<function>``.

============  ===================================================================
core          errors, the acting person and their permissions, the audit log
accounts      sign up, log in, soft delete, budget details, preferences, sharing
ledger        workspace snapshot for the engine, transactions, recurring bills
planning      budgets, savings goals, saved what-if scenarios
csv_files     CSV import pipeline (with undo / redo) and CSV exports
demo_data     create the database, reset the demo budgets, clear a budget
============  ===================================================================
"""
from .accounts import (
    DEFAULT_LENS, add_note, authenticate, budget_details, coach_clients, count_accounts, create_account,
    current_grant, delete_account, get_meta, get_prefs, is_demo_profile, set_meta, set_prefs, set_sharing,
    update_budget_details,
)
from .core import (
    ROLE_LABELS, Actor, EditConflict, Forbidden, ImportBlocked, NotFound, ValidationFailed,
    accessible_workspaces, audit, audit_entries, list_demo_profiles, make_actor, require,
)
from .csv_files import (
    UndoPreview, analyse_draft, commit_draft, create_draft, discard_draft, export_summary_csv,
    export_transactions_csv, get_batch, get_draft, list_batches, log_export, redo_import, save_draft_settings,
    undo_import, undo_preview,
)
from .demo_data import clear_workspace, reset_database, reset_demo_data, seed_database
from .ledger import (
    Snapshot, TxnPage, add_bill, add_transaction, confirm_payment, delete_bill, delete_transaction, get_bill,
    get_txn_row, kind_for, list_transactions, load_rules, load_snapshot, recategorise, set_bill_active,
    similar_transactions, update_bill, update_transaction,
)
from .planning import (
    add_goal, adjustments_from_json, adjustments_to_json, contribute, delete_budget, delete_goal,
    delete_scenario, list_scenarios, save_scenario, set_budget,
)
