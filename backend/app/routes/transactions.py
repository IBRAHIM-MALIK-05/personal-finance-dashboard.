"""Transactions: list with filters, add, edit, delete and category correction."""
from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db, today
from ..engine.categories import CATEGORIES, categorise
from .helpers import recent_months, safe_next

bp = Blueprint("transactions", __name__)

CATEGORY_REASON = {"user": "You chose this", "import": "From the imported file"}


@bp.get("/transactions")
def index():
    S.require(g.actor, "view_details")
    args = request.args
    with get_db().read() as conn:
        page = S.list_transactions(conn, g.actor, q=args.get("q", ""), category=args.get("category", ""),
                                   month=args.get("month", ""), batch=args.get("batch", type=int),
                                   page=args.get("page", 1, type=int))
        rules = S.load_rules(conn, g.actor.workspace_id)
        total_all = S.list_transactions(conn, g.actor, per_page=1).total
    for r in page.rows:
        r["reason"] = CATEGORY_REASON.get(r["category_source"]) or categorise(r["payee"], r["description"], rules)[2]
    return render_template("pages/transactions/list.html", page=page, args=args,
                           months=recent_months(today(), 7), total_all=total_all)


@bp.route("/transactions/new", methods=["GET", "POST"])
def new():
    S.require(g.actor, "edit")
    errors, values = {}, {"posted_on": today().isoformat(), "direction": "out", "category_id": "auto"}
    if request.method == "POST":
        try:
            S.add_transaction(get_db(), g.actor, request.form.to_dict(), today())
            flash("Transaction added.", "success")
            return redirect(url_for("transactions.index"))
        except S.ValidationFailed as e:
            errors, values = e.errors, request.form.to_dict()
    return render_template("pages/transactions/form.html", errors=errors, values=values, mode="new"), \
        (400 if errors else 200)


def _form_values(row: dict) -> dict:
    return {"posted_on": row["posted_on"].isoformat(),
            "amount": f"{abs(row['amount']) / 100:.2f}".rstrip("0").rstrip("."),
            "direction": "in" if row["amount"] > 0 else "out", "payee": row["payee"],
            "description": row["description"], "category_id": row["category_id"], "note": row["note"],
            "version": row["version"]}


@bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
def edit(txn_id: int):
    S.require(g.actor, "edit")
    with get_db().read() as conn:
        row = S.get_txn_row(conn, g.actor, txn_id)
    errors, values = {}, _form_values(row)
    if request.method == "POST":
        try:
            S.update_transaction(get_db(), g.actor, txn_id, request.form.to_dict(),
                                 request.form.get("version", type=int), today())
            flash("Transaction updated.", "success")
            return redirect(safe_next(request.form.get("next"), url_for("transactions.index")))
        except S.ValidationFailed as e:
            errors, values = e.errors, {**request.form.to_dict(), "version": row["version"]}
    return render_template("pages/transactions/form.html", errors=errors, values=values, mode="edit", row=row), \
        (400 if errors else 200)


@bp.post("/transactions/<int:txn_id>/delete")
def delete(txn_id: int):
    S.delete_transaction(get_db(), g.actor, txn_id, request.form.get("version", type=int))
    flash("Transaction deleted. It is recorded in Activity.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("transactions.index")))


@bp.post("/transactions/<int:txn_id>/category")
def change_category(txn_id: int):
    """Two-step correction: if other transactions from the same payee exist,
    ask whether to change them too (and remember a rule) before saving."""
    S.require(g.actor, "edit")
    category = request.form.get("category_id", "")
    version = request.form.get("version", type=int)
    apply = request.form.get("apply")
    next_url = safe_next(request.form.get("next"), url_for("transactions.index"))
    with get_db().read() as conn:
        row = S.get_txn_row(conn, g.actor, txn_id)
        similar = S.similar_transactions(conn, g.actor, row)
    if category == row["category_id"] and apply is None:
        flash("Category unchanged.", "info")
        return redirect(next_url)
    if similar and apply is None:
        return render_template("pages/transactions/confirm_category.html", row=row, new_cat=category,
                               similar=len(similar), version=version, next=next_url)
    changed = S.recategorise(get_db(), g.actor, txn_id, category, version, apply == "similar")
    message = f"Category changed to {CATEGORIES[category].name}."
    if apply == "similar":
        message += (f" {changed - 1} other {row['payee']} transaction(s) updated, "
                    "and future ones will use this category.")
    flash(message, "success")
    return redirect(next_url)
