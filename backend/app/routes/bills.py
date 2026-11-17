"""Recurring bills and income: these drive the forecast."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from .. import services as S
from ..context import get_db, today
from ..engine.categories import normalise_payee
from ..engine.recurring import monthly_equivalent, next_due
from ..view_models.overview import build_view

bp = Blueprint("bills", __name__)


@bp.get("/bills")
def index():
    q = request.args.get("q", "").strip().lower()
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    rows = [{"bill": b, "next": next_due(b, v.snap.txns, today()) if b.active else None,
             "monthly": monthly_equivalent(b.amount, b.cadence)}
            for b in v.snap.bills
            if not q or q in b.name.lower() or q in normalise_payee(b.payee)]
    return render_template("pages/bills/list.html", v=v, q=request.args.get("q", ""),
                           income=[r for r in rows if r["bill"].amount > 0],
                           outgoing=[r for r in rows if r["bill"].amount < 0])


def _form_defaults(b: dict | None = None) -> dict:
    if b is None:
        return {"cadence": "monthly", "direction": "out", "tolerance_pct": "10",
                "anchor_date": today().isoformat(), "category_id": "utilities"}
    return {"name": b["name"], "payee": b["payee"], "amount": f"{abs(b['amount']) // 100}",
            "direction": "in" if b["amount"] > 0 else "out", "cadence": b["cadence"],
            "anchor_date": b["anchor_date"].isoformat(), "category_id": b["category_id"],
            "tolerance_pct": str(b["tolerance_pct"]), "version": b["version"]}


@bp.route("/bills/new", methods=["GET", "POST"])
def new():
    S.require(g.actor, "edit")
    errors, values = {}, _form_defaults()
    if request.method == "POST":
        try:
            S.add_bill(get_db(), g.actor, request.form.to_dict())
            flash("Recurring item added. The forecast now includes it.", "success")
            return redirect(url_for("bills.index"))
        except S.ValidationFailed as e:
            errors, values = e.errors, request.form.to_dict()
    return render_template("pages/bills/form.html", errors=errors, values=values, mode="new"), \
        (400 if errors else 200)


@bp.route("/bills/<int:bill_id>/edit", methods=["GET", "POST"])
def edit(bill_id: int):
    S.require(g.actor, "edit")
    with get_db().read() as conn:
        bill = S.get_bill(conn, g.actor, bill_id)
    errors, values = {}, _form_defaults(bill)
    if request.method == "POST":
        try:
            S.update_bill(get_db(), g.actor, bill_id, request.form.to_dict(), request.form.get("version", type=int))
            flash("Recurring item updated.", "success")
            return redirect(url_for("bills.index"))
        except S.ValidationFailed as e:
            errors, values = e.errors, {**request.form.to_dict(), "version": bill["version"]}
    return render_template("pages/bills/form.html", errors=errors, values=values, mode="edit", bill_id=bill_id), \
        (400 if errors else 200)


@bp.post("/bills/<int:bill_id>/toggle")
def toggle(bill_id: int):
    active = request.form.get("active") == "1"
    S.set_bill_active(get_db(), g.actor, bill_id, active, request.form.get("version", type=int))
    flash("Resumed. It is back in the forecast." if active else "Paused. It is no longer in the forecast.", "success")
    return redirect(url_for("bills.index"))


@bp.post("/bills/<int:bill_id>/delete")
def delete(bill_id: int):
    S.delete_bill(get_db(), g.actor, bill_id)
    flash("Recurring item deleted. Past payments are kept as normal transactions.", "success")
    return redirect(url_for("bills.index"))


@bp.post("/bills/<int:bill_id>/confirm")
def confirm_payment(bill_id: int):
    try:
        occurrence = date.fromisoformat(request.form.get("occurrence", ""))
    except ValueError:
        flash("That bill date was not valid. Nothing was changed.", "error")
        return redirect(url_for("bills.index"))
    S.confirm_payment(get_db(), g.actor, bill_id, occurrence, request.form.get("txn_id", type=int),
                      request.form.get("version", type=int))
    flash("Payment confirmed. The forecast no longer expects this bill.", "success")
    return redirect(url_for("bills.index") + "#suggested")
