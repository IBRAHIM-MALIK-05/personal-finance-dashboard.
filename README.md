# Personal Finance & Budget Dashboard

A web app that helps students and households understand their spending, plan bills and savings, and see where
their money is heading. Every number can explain itself, with its formula, inputs and assumptions.

People **create an account** (name, email, password) and get their own private budget, which starts empty. A
"Get started" checklist walks them through adding transactions (by hand or CSV import), recurring bills, budgets and
goals. Anyone can also **try the demo**, which has ready-made sample budgets and a financial coach persona.

**Stack:** Python 3 · Flask · Jinja2 · SQLAlchemy Core · MySQL 8 · SVG charts drawn by our own code ·
plain CSS and a little vanilla JavaScript · pytest.

The app never connects to a bank, never asks for bank passwords and gives no investment advice. The demo data is
synthetic.

## Project structure

```
backend/                     Python (Flask)
├─ run.py                    start the app
├─ requirements.txt
├─ .env.example              copy to .env for MySQL
├─ app/
│  ├─ __init__.py            create_app(): wires everything together
│  ├─ config.py              paths and settings
│  ├─ context.py             per request: database, today, CSRF, current profile
│  ├─ errors.py              friendly error pages
│  ├─ template_helpers.py    Jinja filters (money, dates, categories)
│  ├─ view_model.py          runs the engine once and shares the results between pages
│  ├─ routes/                one file per screen
│  ├─ services/              database reads and writes, with permission checks:
│  │                         core, accounts, ledger, planning, csv_files, demo_data
│  ├─ engine/                pure calculations, no database (forecast, budgets, importer, ...)
│  ├─ charts/                SVG charts drawn by code
│  └─ database/              Python access to the database: tables, connection, migrations, seed
├─ scripts/                  generate_data.py (sample data files)
└─ tests/

frontend/                    what the browser gets
├─ templates/
│  ├─ layouts/base.html      page shell: sidebar, top bar, footer
│  ├─ components/            macros, icons, navigation, getting-started checklist, import steps
│  └─ pages/                 one template per screen (folders for screens with several:
│                            auth, transactions, bills, imports, errors)
└─ static/
   ├─ css/                   base (design tokens), layout, components (incl. charts), pages
   └─ js/                    chart-reader.js, forms.js

database/                    MySQL scripts, opened in MySQL Workbench
├─ setup_mysql.sql           create the databases and the app user (run once)
└─ account_admin.sql         soft-deleted accounts: views and restore / purge procedures
                             (the app creates its own tables and installs this file itself)
```

How a request flows: `routes/` checks the request and calls `services/` (which check permissions and talk to the
database) and `view_model.py` (which runs the `engine/` calculations). The route then renders a template from
`frontend/templates/pages/`, with charts from `charts/`.

## Setup (Windows)

Needs Python 3.12 or newer. Install the libraries once:

```powershell
cd backend
python -m pip install -r requirements.txt
```

**Database.** Without any configuration the app uses a local SQLite file, and every page shows which storage is in
use. To use MySQL:

1. In MySQL Workbench, open `database/setup_mysql.sql`, replace `CHANGE_ME` with a password and run it.
2. Copy `backend/.env.example` to `backend/.env` and put the same password in both URLs.

`backend/.env` holds your private settings (database passwords and the key that signs log-in sessions). It is
git-ignored; the session key is created there automatically on the first run.

## Run

```powershell
cd backend
python run.py                 # open http://127.0.0.1:5000
python run.py --reset-demo    # put the demo budgets back to the start (accounts are kept)
python run.py --reset         # rebuild ALL tables (refuses if real accounts exist, unless --yes)
```

The first page is **Log in**. New people click **Create an account**. To look around without an account, click
**Try the demo** and choose a profile:

| Profile | Role | Can |
|---|---|---|
| Ayesha | Personal owner (student) | Edit her own budget |
| Bilal Khan, Sana Khan | Household managers | Edit the shared household budget; manage coach sharing |
| Coach Sara | Financial coach | Read-only, only what the household shares; leave notes |

When the app starts on an existing database it adds any new optional columns automatically (no data is touched),
so upgrading usually needs no reset.

Demo profiles need no password and only ever see the demo budgets. Real accounts are never listed there, cannot be
opened without their password, and are never touched by a demo reset. Permissions are checked on the server for
every read and write.

## Deleted accounts (soft delete)

Settings → **Delete account** (password + the word `DELETE`) is a **soft delete**. Nothing is erased:

| Where | Meaning |
|---|---|
| table `deleted_accounts` | **one row per soft-deleted account**: `profile_id`, `workspace_id`, `name`, `email`, `deleted_at` |
| `profiles.is_deleted`, `workspaces.is_deleted` | `0` = active (default), `1` = deleted by its owner |
| `profiles.deleted_at` | when it was deleted |
| `profiles.deleted_email` | the email, moved aside so the address can sign up again |

The person is logged out and can't log in again; all their transactions, bills, budgets and goals stay. Restoring
and permanently deleting are done **in the database**. The app installs these tools automatically on MySQL
(defined in [`database/account_admin.sql`](database/account_admin.sql)); use them in MySQL Workbench:

```sql
SELECT * FROM deleted_accounts;                   -- the table of soft-deleted accounts (their ids)
SELECT * FROM accounts_overview;                  -- every real account with is_deleted and status
SELECT * FROM deleted_account_transactions WHERE profile_id = 'u_...';

CALL restore_account('u_...');                    -- undo a soft delete (removes the row from deleted_accounts)
CALL purge_account('u_...');                      -- permanent delete of one soft-deleted account
CALL purge_deleted_accounts(30);                  -- permanent delete of all soft-deleted 30+ days ago (0 = all)
```

Each procedure is one transaction (all or nothing), refuses active and demo accounts, and restore is refused if the
email has been reused.

## Test

```powershell
cd backend
python -B -m pytest -p no:cacheprovider
```

(`-B` and `-p no:cacheprovider` stop Python and pytest leaving cache folders in the project.)

| File | Covers |
|---|---|
| `tests/test_engine.py` | calculations with no database: money, dates, forecast, budgets, goals, what-if, CSV analysis, charts |
| `tests/test_app.py` | the app end to end on a throw-away SQLite file: pages, roles, accounts, soft delete, acceptance cases |
| `tests/test_mysql.py` | the critical paths on MySQL; runs only when `TEST_DATABASE_URL` is set in `backend/.env` |

## Sample data

Run from `backend/`:

```powershell
python scripts\generate_data.py --out generated-data   # sample data files
```

## Known limitations

* English only.
* Undo exists for imports only; the Activity page records every other change.
* The forecast assumes recurring items happen on schedule and everyday spending follows your recent median month.
* Recurring bills are entered by hand. Payments that only look like a bill are suggestions until confirmed.
* An account has one personal budget. Inviting a second person to share a household budget is not built yet.
* There is no "forgot password" (it needs email sending) and no limit on log-in attempts yet.
* Coach sharing works only inside the demo, because the coach is a demo persona.
* Running on the internet would also need HTTPS and a production web server (this uses Flask's development server).

## Libraries

Flask, Jinja2, Werkzeug (BSD-3), SQLAlchemy (MIT), PyMySQL (MIT), cryptography (Apache-2.0/BSD), pytest (MIT).
No chart library, external fonts, images or CDNs: charts are SVG generated by `backend/app/charts/` and icons are
inline SVGs drawn for this project.
