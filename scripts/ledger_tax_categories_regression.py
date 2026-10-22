"""Offline coverage for validated Ledger account tax metadata and UI."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-ledger-tax-categories-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import paths  # noqa: E402
import tax_categories  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    ACCOUNT_TAX_CATEGORY_FIELD,
    run_migrations,
)
from migrations.schema_versions import _stamp_version  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def run():
    failures = 0

    # Start from the stamped historical head immediately before account tax
    # categories (v15), rather than an impossible partial one-table database.
    _stamp_version(paths.db_path(), 0, "fixture-v0")
    plan = db._schema_migration_plan()
    for target in range(1, 16):
        name, migrate = plan[target - 1]
        migrate()
        _stamp_version(paths.db_path(), target, name)
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute(
            "INSERT INTO accounts (code, name, type) "
            "VALUES ('7777', 'Existing custom expense', 'expense')"
        )
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    failures += check(
        "existing Ledgers receive the account tax-category column once",
        first[ACCOUNT_TAX_CATEGORY_FIELD] == "created"
        and second[ACCOUNT_TAX_CATEGORY_FIELD] == "skipped",
    )

    accounts = ledger.list_accounts()
    by_code = {row["code"]: row for row in accounts}
    failures += check(
        "only unambiguous starter accounts are seeded",
        all(by_code[code]["tax_category"] == category
            for code, category in tax_categories.DEFAULT_ACCOUNT_CATEGORIES.items())
        and all(by_code[code]["tax_category"] is None
                for code in ("5000", "6020", "6030", "6060", "6070",
                             "6080", "6090", "6100", "6110", "4190", "4200", "7777")),
    )

    payroll = by_code["6060"]
    updated = ledger.set_account_tax_category(payroll["id"], "wages")
    failures += check(
        "a valid expense mapping saves and reads back",
        updated["tax_category"] == "wages"
        and ledger.account_by_code("6060")["tax_category"] == "wages",
    )
    cleared = ledger.set_account_tax_category(payroll["id"], "")
    failures += check(
        "blank deliberately returns an account to unresolved",
        cleared["tax_category"] is None,
    )

    try:
        ledger.set_account_tax_category(payroll["id"], "gross_receipts")
    except ledger.LedgerValidationError as exc:
        invalid_direction = str(exc)
    else:
        invalid_direction = ""
    failures += check(
        "revenue categories are rejected for expense accounts",
        "valid Schedule C expense category" in invalid_direction,
    )

    try:
        ledger.set_account_tax_category(by_code["1010"]["id"], "gross_receipts")
    except ledger.LedgerValidationError as exc:
        invalid_balance_sheet = str(exc)
    else:
        invalid_balance_sheet = ""
    failures += check(
        "balance-sheet accounts cannot be assigned Schedule C categories",
        "Only revenue and expense accounts" in invalid_balance_sheet,
    )

    with db.get_connection() as conn:
        try:
            conn.execute(
                "UPDATE accounts SET tax_category = 'advertising' WHERE code = '4000'"
            )
        except sqlite3.IntegrityError:
            database_rejected = True
        else:
            database_rejected = False
    failures += check(
        "the database constraint rejects invalid type/category combinations",
        database_rejected,
    )

    ledger.create_journal_entry(
        "2026-08-01", "Unmapped payroll fixture", [
            {"account_id": payroll["id"], "debit_amount": "125.00"},
            {"account_id": by_code["1010"]["id"], "credit_amount": "125.00"},
        ], created_by="regression",
    )
    warning_html = app._ledger_accounts_content()
    failures += check(
        "used unresolved accounts produce a visible blocking warning",
        "Categorization needed." in warning_html
        and "1 used account needs categorization" in warning_html
        and "complete tax projections or exports" in warning_html,
    )

    route_response = app.save_ledger_account_tax_category(
        payroll["id"], "contract_labor")
    failures += check(
        "mapping mutation uses POST-303-banner back to Ledger Accounts",
        route_response.status_code == 303
        and route_response.headers["location"].startswith(
            "/ledger?tab=accounts&msg="
        ),
    )
    invalid_response = app.save_ledger_account_tax_category(
        payroll["id"], "gross_receipts")
    failures += check(
        "invalid route input returns a visible error banner redirect",
        invalid_response.status_code == 303
        and route_response.headers["location"] != invalid_response.headers["location"]
        and invalid_response.headers["location"].startswith(
            "/ledger?tab=accounts&err="
        ),
    )

    html = app._ledger_accounts_content()
    failures += check(
        "Accounts UI explains conservative mapping and renders saved values",
        "Klearing seeds" in html
        and "only clear matches" in html
        and "Needs categorization" in html
        and "not used on Schedule C" in html
        and 'value="contract_labor" selected' in html,
    )
    failures += check(
        "Accounts UI has accessible per-account POST forms without inline styles",
        f'action="/ledger/accounts/{payroll["id"]}/tax-category"' in html
        and f'aria-label="Schedule C category for 6060 Payroll &amp; Contractors"' in html
        and 'method="post"' in html
        and "style=" not in html,
    )

    if failures:
        print(f"FAIL: {failures} Ledger tax-category checks failed.")
        return 1
    print("PASS: Ledger tax-category checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
