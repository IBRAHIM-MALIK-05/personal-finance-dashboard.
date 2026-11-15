"""Page routes (everything except import/export, which lives in imports.py)."""
from __future__ import annotations

import json
from datetime import date

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   session, url_for)

from .. import get_db, services as S, today
from ..engine import scenarios as SC
from ..engine.categories import BUDGETABLE_IDS, CATEGORIES, normalise_payee
from ..engine.dates import DateParseError, parse_flexible
from ..engine.forecast import variable_assumptions
from ..engine.models import Adjustment
from ..engine.money import AmountError, parse_amount
from ..engine.recurring import monthly_equivalent, next_due
from ..graphics.bars import budget_bar_svg, variance_svg
from ..graphics.cashflow import cashflow_svg, chart_points_json
from .compute import build_view

bp = Blueprint("views", __name__)


def safe_next(url: str | None, default: str) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return default


def horizon() -> int:
    h = request.args.get("horizon", type=int) or g.prefs.get("horizon", 30)
    return h if h in (30, 60, 90) else 30


# --------------------------------------------------------------- profiles --

@bp.get("/profiles")
def profiles():
    with get_db().read() as conn:
        profs = S.list_profiles(conn)
    return render_template("profiles.html", profiles=profs, reason=request.args.get("reason"),
                           role_labels=S.ROLE_LABELS)


@bp.post("/profiles")
def choose_profile():
    pid = request.form.get("profile_id", "")
    with get_db().read() as conn:
        actor = S.make_actor(conn, pid, None)
    if actor is None:
        flash("That profile has no budget it can open yet. If it is a coach, a household must share first.", "error")
        return redirect(url_for("views.profiles"))
    session["profile_id"], session["workspace_id"] = pid, actor.workspace_id
    session.pop("whatif", None)
    return redirect(url_for("views.coach") if actor.role == "coach" else url_for("views.dashboard"))


@bp.post("/workspace")
def switch_workspace():
    session["workspace_id"] = request.form.get("workspace_id", type=int)
    return redirect(url_for("views.dashboard"))


@bp.post("/prefs")
def set_prefs():
    changes = {k: request.form[k] for k in ("lens", "theme", "language", "reduced_motion", "horizon") if k in request.form}
    S.set_prefs(get_db(), g.actor.profile_id, g.actor.role, **changes)
    return redirect(safe_next(request.form.get("next"), url_for("views.dashboard")))


@bp.get("/db-problem")
def db_problem():
    err = current_app.extensions["db_error"]
    if not err:
        return redirect(url_for("views.dashboard"))
    return render_template("db_problem.html", error=err), 503


# -------------------------------------------------------------- dashboard --

@bp.get("/")
def dashboard():
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    chart = cashflow_svg(v.fc, chart_id="dash", compact=True) if v.snap.txns else ""
    data = chart_points_json(v.fc, compact=True) if v.snap.txns else "{}"
    bars = {s.budget.category_id: budget_bar_svg(s, f"bb-{s.budget.category_id}") for s in v.statuses}
    return render_template("dashboard.html", v=v, chart=chart, chart_data=data, bars=bars,
                           variance_chart=variance_svg(v.variance[:8], "dash-var"))


# ----------------------------------------------------------- transactions --

@bp.get("/transactions")
def transactions():
    S.require(g.actor, "view_details")
    args = request.args
    with get_db().read() as conn:
        page = S.list_transactions(conn, g.actor, q=args.get("q", ""), category=args.get("category", ""),
                                   month=args.get("month", ""), batch=args.get("batch", type=int),
                                   page=args.get("page", 1, type=int))
        rules = S.load_rules(conn, g.actor.workspace_id)
        total_all = S.list_transactions(conn, g.actor, per_page=1).total
    from ..engine.categories import categorise
    for r in page.rows:
        if r["category_source"] == "user":
            r["reason"] = "You chose this"
        elif r["category_source"] == "import":
            r["reason"] = "From the imported file"
        else:
            r["reason"] = categorise(r["payee"], r["description"], rules)[2]
    months = sorted({f"{d.year:04d}-{d.month:02d}" for d in _recent_months(today(), 7)}, reverse=True)
    return render_template("transactions.html", page=page, args=args, months=months, total_all=total_all)


def _recent_months(as_of: date, n: int):
    from ..engine.dates import add_months
    return [add_months(as_of.replace(day=1), -i) for i in range(n)]


@bp.route("/transactions/new", methods=["GET", "POST"])
def new_transaction():
    S.require(g.actor, "edit")
    errors, values = {}, {"posted_on": today().isoformat(), "direction": "out", "category_id": "auto"}
    if request.method == "POST":
        try:
            S.add_transaction(get_db(), g.actor, request.form.to_dict(), today())
            flash("Transaction added.", "success")
            return redirect(url_for("views.transactions"))
        except S.ValidationFailed as e:
            errors, values = e.errors, request.form.to_dict()
    return render_template("transaction_form.html", errors=errors, values=values, mode="new"), (400 if errors else 200)


@bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
def edit_transaction(txn_id: int):
    S.require(g.actor, "edit")
    with get_db().read() as conn:
        row = S.get_txn_row(conn, g.actor, txn_id)
    errors = {}
    values = {"posted_on": row["posted_on"].isoformat(), "amount": f"{abs(row['amount']) / 100:.2f}".rstrip("0").rstrip("."),
              "direction": "in" if row["amount"] > 0 else "out", "payee": row["payee"],
              "description": row["description"], "category_id": row["category_id"], "note": row["note"],
              "version": row["version"]}
    if request.method == "POST":
        try:
            S.update_transaction(get_db(), g.actor, txn_id, request.form.to_dict(),
                                 request.form.get("version", type=int), today())
            flash("Transaction updated.", "success")
            return redirect(safe_next(request.form.get("next"), url_for("views.transactions")))
        except S.ValidationFailed as e:
            errors, values = e.errors, {**request.form.to_dict(), "version": row["version"]}
    return render_template("transaction_form.html", errors=errors, values=values, mode="edit", row=row), \
        (400 if errors else 200)


@bp.post("/transactions/<int:txn_id>/delete")
def delete_transaction(txn_id: int):
    S.delete_transaction(get_db(), g.actor, txn_id, request.form.get("version", type=int))
    flash("Transaction deleted. It is recorded in Activity.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("views.transactions")))


@bp.post("/transactions/<int:txn_id>/category")
def change_category(txn_id: int):
    """Two-step correction: if other transactions from the same payee exist,
    ask whether to change them too (and remember a rule) before saving."""
    S.require(g.actor, "edit")
    cat = request.form.get("category_id", "")
    version = request.form.get("version", type=int)
    apply = request.form.get("apply")
    nxt = safe_next(request.form.get("next"), url_for("views.transactions"))
    with get_db().read() as conn:
        row = S.get_txn_row(conn, g.actor, txn_id)
        similar = S.similar_transactions(conn, g.actor, row)
    if cat == row["category_id"] and apply is None:
        flash("Category unchanged.", "info")
        return redirect(nxt)
    if similar and apply is None:
        return render_template("category_confirm.html", row=row, new_cat=cat, similar=len(similar),
                               version=version, next=nxt)
    n = S.recategorise(get_db(), g.actor, txn_id, cat, version, apply == "similar")
    msg = f"Category changed to {CATEGORIES[cat].name}."
    if apply == "similar":
        msg += f" {n - 1} other {row['payee']} transaction(s) updated, and future ones will use this category."
    flash(msg, "success")
    return redirect(nxt)


# ---------------------------------------------------------------- budgets --

@bp.get("/budgets")
def budgets():
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    bars = {s.budget.category_id: budget_bar_svg(s, f"b-{s.budget.category_id}") for s in v.statuses}
    used = {s.budget.category_id for s in v.statuses}
    notes = {}
    for n in v.snap.notes:
        notes.setdefault(n["target"], []).append(n)
    return render_template("budgets.html", v=v, bars=bars, notes=notes,
                           unbudgeted=[c for c in BUDGETABLE_IDS if c not in used],
                           edit_cat=request.args.get("edit"))


@bp.post("/budgets")
def save_budget():
    version = request.form.get("version", type=int)
    cat = request.form.get("category_id", "")
    try:
        S.set_budget(get_db(), g.actor, cat, request.form.get("monthly_limit", ""), version)
        flash(f"{CATEGORIES.get(cat).name if cat in CATEGORIES else 'Budget'} budget saved.", "success")
    except S.ValidationFailed as e:
        for msg in e.errors.values():
            flash(msg, "error")
        return redirect(url_for("views.budgets", edit=cat) + f"#b-{cat}")
    return redirect(url_for("views.budgets") + f"#b-{cat}")


@bp.post("/budgets/<cat>/delete")
def remove_budget(cat: str):
    S.delete_budget(get_db(), g.actor, cat)
    flash("Budget removed. Your transactions are unchanged.", "success")
    return redirect(url_for("views.budgets"))


@bp.post("/notes")
def add_note():
    try:
        S.add_note(get_db(), g.actor, request.form.get("target", "general"), request.form.get("text", ""))
        flash("Note added. The household will see it on their Budgets page.", "success")
    except S.ValidationFailed as e:
        flash(next(iter(e.errors.values())), "error")
    return redirect(safe_next(request.form.get("next"), url_for("views.budgets")))


# ------------------------------------------------------------------ bills --

@bp.get("/bills")
def bills():
    q = request.args.get("q", "").strip().lower()
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    rows = []
    for b in v.snap.bills:
        if q and q not in b.name.lower() and q not in normalise_payee(b.payee):
            continue
        rows.append({"bill": b, "next": next_due(b, v.snap.txns, today()) if b.active else None,
                     "monthly": monthly_equivalent(b.amount, b.cadence)})
    income = [r for r in rows if r["bill"].amount > 0]
    outgoing = [r for r in rows if r["bill"].amount < 0]
    return render_template("bills.html", v=v, income=income, outgoing=outgoing, q=request.args.get("q", ""))


def _bill_form_defaults(b=None) -> dict:
    if b is None:
        return {"cadence": "monthly", "direction": "out", "tolerance_pct": "10",
                "anchor_date": today().isoformat(), "category_id": "utilities"}
    return {"name": b["name"], "payee": b["payee"], "amount": f"{abs(b['amount']) // 100}",
            "direction": "in" if b["amount"] > 0 else "out", "cadence": b["cadence"],
            "anchor_date": b["anchor_date"].isoformat(), "category_id": b["category_id"],
            "tolerance_pct": str(b["tolerance_pct"]), "version": b["version"]}


@bp.route("/bills/new", methods=["GET", "POST"])
def new_bill():
    S.require(g.actor, "edit")
    errors, values = {}, _bill_form_defaults()
    if request.method == "POST":
        try:
            S.add_bill(get_db(), g.actor, request.form.to_dict())
            flash("Recurring item added. The forecast now includes it.", "success")
            return redirect(url_for("views.bills"))
        except S.ValidationFailed as e:
            errors, values = e.errors, request.form.to_dict()
    return render_template("bill_form.html", errors=errors, values=values, mode="new"), (400 if errors else 200)


@bp.route("/bills/<int:bill_id>/edit", methods=["GET", "POST"])
def edit_bill(bill_id: int):
    S.require(g.actor, "edit")
    from .. import db as T
    from sqlalchemy import select
    with get_db().read() as conn:
        b = conn.execute(select(T.recurring_bills).where(T.recurring_bills.c.id == bill_id,
                         T.recurring_bills.c.workspace_id == g.actor.workspace_id)).first()
    if b is None:
        raise S.NotFound("That recurring item no longer exists.")
    errors, values = {}, _bill_form_defaults(dict(b._mapping))
    if request.method == "POST":
        try:
            S.update_bill(get_db(), g.actor, bill_id, request.form.to_dict(), request.form.get("version", type=int))
            flash("Recurring item updated.", "success")
            return redirect(url_for("views.bills"))
        except S.ValidationFailed as e:
            errors, values = e.errors, {**request.form.to_dict(), "version": b.version}
    return render_template("bill_form.html", errors=errors, values=values, mode="edit", bill_id=bill_id), \
        (400 if errors else 200)


@bp.post("/bills/<int:bill_id>/toggle")
def toggle_bill(bill_id: int):
    active = request.form.get("active") == "1"
    S.set_bill_active(get_db(), g.actor, bill_id, active, request.form.get("version", type=int))
    flash("Resumed. It is back in the forecast." if active else "Paused. It is no longer in the forecast.", "success")
    return redirect(url_for("views.bills"))


@bp.post("/bills/<int:bill_id>/delete")
def delete_bill(bill_id: int):
    S.delete_bill(get_db(), g.actor, bill_id)
    flash("Recurring item deleted. Past payments are kept as normal transactions.", "success")
    return redirect(url_for("views.bills"))


@bp.post("/bills/<int:bill_id>/confirm")
def confirm_payment(bill_id: int):
    occ = date.fromisoformat(request.form["occurrence"])
    S.confirm_payment(get_db(), g.actor, bill_id, occ, request.form.get("txn_id", type=int),
                      request.form.get("version", type=int))
    flash("Payment confirmed. The forecast no longer expects this bill.", "success")
    return redirect(url_for("views.bills") + "#suggested")


# ------------------------------------------------------------------ goals --

@bp.get("/goals")
def goals():
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), 30)
    from ..engine.goals import status_sentence
    return render_template("goals.html", v=v, status_sentence=status_sentence, errors={}, values={})


@bp.post("/goals/new")
def new_goal():
    try:
        S.add_goal(get_db(), g.actor, request.form.to_dict(), today())
        flash("Goal added.", "success")
    except S.ValidationFailed as e:
        with get_db().read() as conn:
            v = build_view(conn, g.actor, today(), 30)
        from ..engine.goals import status_sentence
        return render_template("goals.html", v=v, status_sentence=status_sentence, errors=e.errors,
                               values=request.form.to_dict()), 400
    return redirect(url_for("views.goals"))


@bp.post("/goals/<int:goal_id>/contribute")
def contribute(goal_id: int):
    try:
        S.contribute(get_db(), g.actor, goal_id, request.form.to_dict(), today())
        flash("Contribution recorded." + (" A transfer to savings was added to your transactions."
                                          if request.form.get("record_transfer") == "1" else
                                          " Your balance is unchanged (allocation only)."), "success")
    except S.ValidationFailed as e:
        flash(next(iter(e.errors.values())), "error")
    return redirect(url_for("views.goals") + f"#g-{goal_id}")


@bp.post("/goals/<int:goal_id>/delete")
def delete_goal(goal_id: int):
    S.delete_goal(get_db(), g.actor, goal_id)
    flash("Goal deleted.", "success")
    return redirect(url_for("views.goals"))


# --------------------------------------------------------------- forecast --

@bp.get("/forecast")
def forecast():
    as_of = today()
    raw = request.args.get("as_of", "")
    if raw:
        try:
            chosen = date.fromisoformat(raw)
            if chosen <= today():
                as_of = chosen
        except ValueError:
            flash("That ‘as of’ date was not valid, so today is used.", "error")
    overrides = None
    monthly = request.args.get("monthly_spending", "")
    if monthly:
        try:
            v_ = parse_amount(monthly)
            if v_ > 0:
                overrides = {"__total__": v_}
        except AmountError as e:
            flash(str(e), "error")
    with get_db().read() as conn:
        v = build_view(conn, g.actor, as_of, horizon(), overrides=overrides)
    chart = cashflow_svg(v.fc, chart_id="fc") if v.snap.txns else ""
    return render_template("forecast.html", v=v, chart=chart, horizon=horizon(),
                           chart_data=chart_points_json(v.fc) if chart else "{}",
                           as_of=as_of, overrides=overrides)


# ---------------------------------------------------------------- what-if --

def _draft_key() -> str:
    return f"whatif-{g.actor.workspace_id}"


def _draft() -> dict:
    return session.get(_draft_key()) or {"name": "", "adjustments": "[]"}


def _store(d: dict) -> None:
    session[_draft_key()] = d


@bp.get("/whatif")
def whatif():
    d = _draft()
    adjs = S.adjustments_from_json(d["adjustments"])
    h = horizon()
    with get_db().read() as conn:
        v = build_view(conn, g.actor, today(), h)
        saved = S.list_scenarios(conn, g.actor)
    bills = {b.id: b for b in v.snap.bills}
    alt = delta = None
    if adjs:
        alt = SC.run(opening_balance=v.snap.workspace["opening_balance"], opening_date=v.snap.workspace["opening_date"],
                     txns=v.snap.txns, bills=v.snap.bills, as_of=today(), horizon_days=h,
                     assumptions=v.fc.assumption_set, adjustments=adjs)
        delta = SC.compare(v.fc, alt, adjs, v.snap.bills)
    chart = cashflow_svg(v.fc, compare=alt, chart_id="wi", title="Forecast with and without your changes") \
        if v.snap.txns else ""
    return render_template("whatif.html", v=v, adjs=[(i, SC.describe(a, bills)) for i, a in enumerate(adjs)],
                           alt=alt, delta=delta, chart=chart, saved=saved, draft=d, horizon=h,
                           chart_data=chart_points_json(v.fc, alt) if chart else "{}",
                           categories=[c for c in BUDGETABLE_IDS])


@bp.post("/whatif/add")
def whatif_add():
    d = _draft()
    adjs = S.adjustments_from_json(d["adjustments"])
    f = request.form
    kind = f.get("kind")
    try:
        if kind == "cancel_bill":
            adjs.append(Adjustment("cancel_bill", bill_id=f.get("bill_id", type=int)))
        elif kind == "change_bill":
            adjs.append(Adjustment("change_bill", bill_id=f.get("bill_id", type=int), amount=abs(parse_amount(f.get("amount")))))
        elif kind == "scale_category":
            pct = int(f.get("percent", "0"))
            if not -100 <= pct <= 300:
                raise ValueError("Use a change between -100% and +300%.")
            if f.get("category_id") not in CATEGORIES:
                raise ValueError("Choose a category.")
            adjs.append(Adjustment("scale_category", category_id=f.get("category_id"), percent=pct))
        elif kind == "one_off":
            amt = abs(parse_amount(f.get("amount")))
            on = parse_flexible(f.get("on", ""), "DMY").value
            if on is None or on <= today():
                raise ValueError("Choose a date after today.")
            adjs.append(Adjustment("one_off", amount=amt if f.get("direction") == "in" else -amt, on=on,
                                   label=S.sanitize_text(f.get("label", ""), 60).value or "One-off"))
        elif kind == "add_recurring":
            amt = abs(parse_amount(f.get("amount")))
            on = parse_flexible(f.get("on", ""), "DMY").value
            if on is None or on <= today():
                raise ValueError("Choose a start date after today.")
            adjs.append(Adjustment("add_recurring", amount=amt if f.get("direction") == "in" else -amt, on=on,
                                   cadence=f.get("cadence", "monthly") if f.get("cadence") in ("weekly", "monthly", "quarterly", "yearly") else "monthly",
                                   label=S.sanitize_text(f.get("label", ""), 60).value or "New item"))
        else:
            raise ValueError("Choose what kind of change to make.")
    except (ValueError, AmountError, DateParseError) as e:
        flash(str(e), "error")
        return redirect(url_for("views.whatif"))
    d["adjustments"] = S.adjustments_to_json(adjs)
    _store(d)
    return redirect(url_for("views.whatif") + "#result")


@bp.post("/whatif/remove/<int:index>")
def whatif_remove(index: int):
    d = _draft()
    adjs = S.adjustments_from_json(d["adjustments"])
    if 0 <= index < len(adjs):
        adjs.pop(index)
    d["adjustments"] = S.adjustments_to_json(adjs)
    _store(d)
    return redirect(url_for("views.whatif"))


@bp.post("/whatif/clear")
def whatif_clear():
    session.pop(_draft_key(), None)
    flash("What-if changes cleared.", "info")
    return redirect(url_for("views.whatif"))


@bp.post("/whatif/load/<int:scenario_id>")
def whatif_load(scenario_id: int):
    with get_db().read() as conn:
        s = next((x for x in S.list_scenarios(conn, g.actor) if x["id"] == scenario_id), None)
    if s is None:
        raise S.NotFound("That saved scenario no longer exists.")
    _store({"name": s["name"], "adjustments": s["adjustments_json"]})
    return redirect(url_for("views.whatif") + "#result")


@bp.post("/whatif/save")
def whatif_save():
    d = _draft()
    try:
        S.save_scenario(get_db(), g.actor, request.form.get("name", ""), S.adjustments_from_json(d["adjustments"]))
        flash("Scenario saved. It is only a plan; your transactions are unchanged.", "success")
    except S.ValidationFailed as e:
        flash(next(iter(e.errors.values())), "error")
    return redirect(url_for("views.whatif"))


@bp.post("/whatif/delete/<int:scenario_id>")
def whatif_delete(scenario_id: int):
    S.delete_scenario(get_db(), g.actor, scenario_id)
    flash("Saved scenario deleted.", "success")
    return redirect(url_for("views.whatif"))


# --------------------------------------------------- settings / coach / log --

@bp.get("/settings")
def settings():
    with get_db().read() as conn:
        grant = S.current_grant(conn, g.actor.workspace_id) if g.actor.workspace_kind == "household" else None
    return render_template("settings.html", grant=grant)


@bp.post("/settings/sharing")
def sharing():
    scope = request.form.get("scope") or None
    S.set_sharing(get_db(), g.actor, scope)
    flash({"summary": "Coach Sara can now see summaries only (no transaction details).",
           "full": "Coach Sara can now see full details, including transactions.",
           None: "Sharing stopped. Coach Sara can no longer see this budget."}[scope], "success")
    return redirect(url_for("views.settings") + "#sharing")


@bp.post("/settings/storage")
def storage():
    S.require(g.actor, "edit")
    on = request.form.get("simulate") == "1"
    S.set_meta(get_db(), "simulate_storage_failure", "1" if on else "0")
    flash("Storage failure simulation is ON: saving will fail until you switch it off." if on
          else "Storage failure simulation is off. Saving works again.", "info")
    return redirect(url_for("views.settings") + "#storage")


@bp.post("/settings/reset")
def reset():
    S.require(g.actor, "edit")
    if request.form.get("confirm") != "RESET":
        flash("Type RESET to confirm. Nothing was changed.", "error")
        return redirect(url_for("views.settings") + "#data")
    S.reset_database(get_db(), today())
    session.clear()
    flash("All demo data was reset to the original synthetic data.", "success")
    return redirect(url_for("views.profiles"))


@bp.post("/settings/clear")
def clear():
    if request.form.get("confirm") != "CLEAR":
        flash("Type CLEAR to confirm. Nothing was changed.", "error")
        return redirect(url_for("views.settings") + "#data")
    S.clear_workspace(get_db(), g.actor)
    flash("This budget is now empty. Use Settings → Reset to restore the demo data.", "success")
    return redirect(url_for("views.dashboard"))


@bp.get("/coach")
def coach():
    if g.actor.role != "coach":
        return redirect(url_for("views.dashboard"))
    with get_db().read() as conn:
        clients = S.coach_clients(conn, g.actor.profile_id)
    return render_template("coach.html", clients=clients)


@bp.get("/history")
def history():
    with get_db().read() as conn:
        entries = S.audit_entries(conn, g.actor)
    return render_template("history.html", entries=entries)


@bp.get("/help")
def help_page():
    return render_template("help.html")
