"""Offline coverage proving the retired Wave Tax queue is read-only."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-archive-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from backend.routers import tax  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def insert(transaction_id, state, category):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_transactions "
            "(id, wave_id, date, description, merchant, amount_cents, "
            "ai_category, user_approved) VALUES (?, ?, '2026-07-01', ?, ?, ?, ?, ?)",
            (transaction_id, f"wave-{transaction_id}", transaction_id,
             f"Merchant {transaction_id}", 12500, category, state),
        )


def run():
    failures = 0
    db.init_db()
    insert("pending", 0, "supplies")
    insert("approved", 1, "gross_receipts")
    insert("rejected", 2, "utilities")

    approved = tax.list_legacy_transactions()
    failures += check(
        "archive defaults to approved records and labels its source",
        approved["read_only"] is True
        and approved["source"] == "legacy_wave_tax_archive"
        and approved["count"] == 1
        and approved["transactions"][0]["approval_status"] == "approved",
    )
    all_records = tax.list_legacy_transactions("all")
    failures += check(
        "every historical review state remains visible",
        {row["approval_status"] for row in all_records["transactions"]}
        == {"pending", "approved", "rejected"}
        and all_records["summary"] == {
            "total": 3, "pending": 1, "approved": 1, "rejected": 1,
        },
    )

    routes = {
        (route.path, method)
        for route in tax.router.routes if hasattr(route, "path")
        for method in getattr(route, "methods", set())
    }
    retired_mutations = {
        ("/api/tax/sync-transactions", "POST"),
        ("/api/tax/categorize", "POST"),
        ("/api/tax/transactions/{transaction_id}", "PATCH"),
        ("/api/tax/transactions/{transaction_id}/approve", "POST"),
        ("/api/tax/transactions/{transaction_id}/reject", "POST"),
    }
    failures += check(
        "retired sync, categorization, and approval mutations are unregistered",
        not (routes & retired_mutations),
    )
    failures += check(
        "the archive exposes only a read route plus the approved CSV export",
        ("/api/tax/legacy-transactions", "GET") in routes
        and ("/api/tax/exports/legacy-transactions.csv", "GET") in routes,
    )

    if failures:
        print(f"FAIL: {failures} Tax archive checks failed.")
        return 1
    print("PASS: Tax archive read-only checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
