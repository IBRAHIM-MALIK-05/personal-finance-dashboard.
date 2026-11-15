"""Generate the synthetic dataset and the messy import file into ../data/.

    python scripts/generate_data.py [--as-of 2026-11-15] [--seed 2026]

Same seed + date = identical output, every time.
"""
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finance.seed import PROFILES, generate, messy_csv  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--as-of", default="2026-11-15")
ap.add_argument("--seed", type=int, default=2026)
args = ap.parse_args()
as_of = date.fromisoformat(args.as_of)
data = generate(as_of, args.seed)
out = ROOT.parent / "data"
out.mkdir(exist_ok=True)

with open(out / "sample_transactions.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["workspace_id", "posted_on", "amount_paisa", "payee", "description", "category_id", "kind",
                "recurring_id", "occurrence_date"])
    for t in data.transactions:
        w.writerow([t["workspace_id"], t["posted_on"], t["amount"], t["payee"], t["description"],
                    t["category_id"], t["kind"], t["recurring_id"] or "", t["occurrence_date"] or ""])

summary = {
    "as_of": as_of.isoformat(), "seed": args.seed, "profiles": PROFILES,
    "workspaces": data.workspaces, "recurring_bills": data.bills, "budgets": data.budgets,
    "goals": data.goals, "goal_contributions": data.contributions, "scenarios": data.scenarios,
    "counts": {"transactions": len(data.transactions), "recurring_bills": len(data.bills),
               "budgets": len(data.budgets), "goals": len(data.goals)},
}
(out / "sample_data.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
household = [t for t in data.transactions if t["workspace_id"] == 2]
(out / "messy_import.csv").write_text(messy_csv(household, as_of), encoding="utf-8")
print(f"{len(data.transactions)} transactions -> {out}")
