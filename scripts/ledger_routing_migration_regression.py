"""Regression coverage for the table-gated Bank Rewards Ledger account."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-ledger-routing-migration-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    BANK_REWARDS_CASHBACK_ACCOUNT,
    run_migrations,
)


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def run():
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute(
            "CREATE TABLE accounts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, type TEXT NOT NULL, parent_account_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO accounts (code, name, type) "
            "VALUES ('7777', 'Existing custom account', 'revenue')"
        )
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    with sqlite3.connect(paths.db_path()) as conn:
        account = conn.execute(
            "SELECT code, name, type, parent_account_id FROM accounts WHERE code = '4210'"
        ).fetchone()
    failures = 0
    failures += check(
        "existing Ledger tables receive the rewards account exactly once",
        first[BANK_REWARDS_CASHBACK_ACCOUNT] == "created"
        and second[BANK_REWARDS_CASHBACK_ACCOUNT] == "skipped",
    )
    failures += check(
        "rewards account is an unparented revenue sibling",
        account == ("4210", "Bank Rewards & Cashback Income", "revenue", None),
    )
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute("UPDATE accounts SET name = 'User account' WHERE code = '4210'")
    try:
        run_migrations(report=False)
    except RuntimeError as exc:
        conflict_rejected = "4210 is already in use" in str(exc)
    else:
        conflict_rejected = False
    failures += check(
        "a conflicting user account code fails safely instead of receiving bank income",
        conflict_rejected,
    )
    if failures:
        print(f"FAIL: {failures} Ledger routing migration checks failed.")
        return 1
    print("PASS: Ledger routing migration checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
