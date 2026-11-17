"""Start the Personal Finance & Budget Dashboard.

    python run.py                  # http://127.0.0.1:5000
    python run.py --reset-demo     # put the demo budgets back to the start (real accounts are kept)
    python run.py --reset          # rebuild ALL tables: deletes every account (asks for --yes if any exist)
"""
import sys
from datetime import date

from sqlalchemy.exc import SQLAlchemyError

from app import create_app, services

application = create_app()


def _real_accounts(db) -> int:
    try:
        with db.read() as conn:
            return services.count_accounts(conn)
    except SQLAlchemyError:  # tables missing or from an older version
        return 0


if __name__ == "__main__":
    db = application.extensions["db"]
    if "--reset" in sys.argv:
        accounts = _real_accounts(db)
        if accounts and "--yes" not in sys.argv:
            sys.exit(f"Refusing to reset: this would delete {accounts} real account(s) and their data.\n"
                     "Use --reset-demo to reset only the demo budgets, or add --yes to delete everything.")
        services.reset_database(db, date.today())
        application.extensions["db_error"] = None
        print("Database rebuilt with the demo data.")
    elif "--reset-demo" in sys.argv:
        services.reset_demo_data(db, date.today())
        print("Demo budgets reset. Real accounts were kept.")
    print(f"Storage: {db.description}")
    for change in application.extensions["schema_changes"]:
        print("Database:", change)
    if application.extensions["db_error"]:
        print("DATABASE PROBLEM:", application.extensions["db_error"])
    application.run(host="127.0.0.1", port=5000, debug=False)
