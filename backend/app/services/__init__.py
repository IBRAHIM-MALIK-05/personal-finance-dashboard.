"""Application services: the only code that reads and writes the database.

Every public function takes an :class:`Actor` and checks permission on the
server before touching data.  Writes go through ``Database.write()``, one atomic
transaction each.  Routes import this package as ``S`` and call ``S.<function>``;
the modules below are grouped by domain:

==============  ==============================================================
accounts        sign up, log in, delete (soft), budget name and starting balance
access          Actor, permissions, profiles and workspaces
activity        audit log (who changed what, when)
snapshot        one-shot load of a workspace for the calculation engine
transactions    list / add / edit / delete / re-categorise
bills           recurring bills and income, payment confirmation
budgets         monthly limits
goals           savings goals and contributions
scenarios       saved what-if plans
sharing         coach consent grants and coach notes
preferences     display preferences and app settings
imports         CSV import pipeline, undo and redo
exports         CSV exports
demo_data       create the database, reset the demo budgets, clear a budget
==============  ==============================================================
"""
from .access import ROLE_LABELS, Actor, accessible_workspaces, list_demo_profiles, make_actor, require
from .accounts import (
    authenticate, budget_details, count_accounts, create_account, delete_account, is_demo_profile,
    update_budget_details,
)
from .activity import audit, audit_entries
from .bills import add_bill, confirm_payment, delete_bill, get_bill, set_bill_active, update_bill
from .budgets import delete_budget, set_budget
from .demo_data import clear_workspace, reset_database, reset_demo_data, seed_database
from .errors import EditConflict, Forbidden, ImportBlocked, NotFound, ValidationFailed
from .exports import export_summary_csv, export_transactions_csv, log_export
from .goals import add_goal, contribute, delete_goal
from .imports import (
    UndoPreview, analyse_draft, commit_draft, create_draft, discard_draft, get_batch, get_draft,
    list_batches, redo_import, save_draft_settings, undo_import, undo_preview,
)
from .preferences import DEFAULT_LENS, get_meta, get_prefs, set_meta, set_prefs
from .scenarios import (
    adjustments_from_json, adjustments_to_json, delete_scenario, list_scenarios, save_scenario,
)
from .sharing import add_note, coach_clients, current_grant, set_sharing
from .snapshot import Snapshot, load_rules, load_snapshot
from .transactions import (
    TxnPage, add_transaction, delete_transaction, get_txn_row, kind_for, list_transactions,
    recategorise, similar_transactions, update_transaction,
)
