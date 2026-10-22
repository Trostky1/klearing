"""Permanent proof of canonical bank signs and the Plaid cache migration."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-bank-signs-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402
import db  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    BANK_SOURCE_MIGRATION,
    run_migrations,
)


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}")


def seed_legacy_cache():
    with sqlite3.connect(paths.db_path()) as conn:
        # A real historical install had the core schema around these Plaid
        # cache tables. Strict pre-version fingerprinting deliberately refuses
        # synthetic partial databases, so this fixture must model that head.
        conn.executescript(db.SCHEMA)
        conn.executescript("""
            CREATE TABLE plaid_transactions (
              transaction_id TEXT PRIMARY KEY, account_id TEXT, date TEXT NOT NULL,
              authorized_date TEXT, amount_cents INTEGER NOT NULL,
              merchant_name TEXT, name TEXT, pending INTEGER NOT NULL DEFAULT 0,
              category_primary TEXT, category_detailed TEXT,
              removed INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL);
            CREATE TABLE plaid_accounts (
              account_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
              official_name TEXT, type TEXT NOT NULL DEFAULT '',
              subtype TEXT NOT NULL DEFAULT '', mask TEXT,
              ledger_account_id INTEGER, synced_at TEXT NOT NULL);
        """)
        rows = [
            ("legacy-expense", "acct-1", "2026-08-10", 3150, "Card payment"),
            ("legacy-deposit", "acct-1", "2026-08-11", -14,
             "Shopify payout p-1"),
        ]
        conn.executemany(
            "INSERT INTO plaid_transactions "
            "(transaction_id, account_id, date, amount_cents, merchant_name, "
            " name, pending, removed, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0, '2026-08-12T00:00:00Z')",
            [(txn, acct, when, amount, name, name)
             for txn, acct, when, amount, name in rows],
        )
        conn.execute(
            "INSERT INTO plaid_accounts "
            "(account_id, name, type, subtype, synced_at) "
            "VALUES ('acct-1', 'Checking', 'depository', 'checking', "
            "'2026-08-12T00:00:00Z')"
        )


def run():
    seed_legacy_cache()
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    db._migrate_columns()

    with sqlite3.connect(paths.db_path()) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows = conn.execute(
            "SELECT source, transaction_id, account_id, amount_cents "
            "FROM bank_transactions ORDER BY transaction_id"
        ).fetchall()
        account = conn.execute(
            "SELECT source, account_id, name FROM bank_accounts"
        ).fetchone()

    print("Migration")
    check("legacy cache migrates exactly once",
          first[BANK_SOURCE_MIGRATION] == "created"
          and second[BANK_SOURCE_MIGRATION] == "skipped")
    check("legacy Plaid tables are removed",
          "plaid_transactions" not in tables and "plaid_accounts" not in tables)
    check("rows are source and account scoped",
          all(row[0] == "plaid" and row[2] == "acct-1" for row in rows))
    amounts = {row[1]: row[3] for row in rows}
    check("legacy expense becomes canonical negative money out",
          amounts.get("legacy-expense") == -3150)
    check("legacy deposit becomes canonical positive money in",
          amounts.get("legacy-deposit") == 14)
    check("Plaid account metadata is preserved",
          account == ("plaid", "acct-1", "Checking"))

    import ledger_sync
    import plaid_client

    plaid_client._persist_sync_records({
        "added": [{"transaction_id": "new-expense", "account_id": "acct-1",
                   "date": "2026-08-12", "amount": "9.99",
                   "name": "New expense", "pending": False}],
        "modified": [], "removed": [],
    })
    fresh = next(row for row in db.list_plaid_transactions_for_ledger()
                 if row["transaction_id"] == "new-expense")
    check("new raw Plaid expense normalizes at the write boundary",
          fresh["amount_cents"] == -999)

    payout = {"payout_id": "p-1", "date": "2026-08-11",
              "amount_cents": 14}
    matched, reason = ledger_sync.match_payout_to_plaid(
        payout, db.list_plaid_transactions_for_ledger())
    check("positive canonical deposit reaches payout matching",
          matched is not None
          and matched["transaction_id"] == "legacy-deposit"
          and reason is None)

    if failures:
        print(f"FAIL: {failures} of {checks} bank-sign checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} bank-sign checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
