"""Run the Personal Finance & Budget Dashboard.

    python app.py              # http://127.0.0.1:5000
    python app.py --reset      # drop all tables and reload the synthetic demo data
"""
import sys
from datetime import date

from finance import create_app, services

app = create_app()

if __name__ == "__main__":
    if "--reset" in sys.argv:
        services.reset_database(app.extensions["db"], date.today())
        print("Demo data reset.")
    print(f"Storage: {app.extensions['db'].description}")
    if app.extensions["db_error"]:
        print("DATABASE PROBLEM:", app.extensions["db_error"])
    app.run(host="127.0.0.1", port=5000, debug=False)
