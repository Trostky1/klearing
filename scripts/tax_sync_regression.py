"""Offline coverage for retiring the legacy Wave Tax sync path."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-sync-retired-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from backend.routers import tax  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def run():
    failures = 0
    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_transactions "
            "(id, wave_id, date, amount_cents, ai_category, user_approved) "
            "VALUES ('preserved', 'wave-preserved', '2026-06-12', 1234, "
            "'gross_receipts', 1)"
        )

    db.init_db()
    preserved = tax.list_legacy_transactions()["transactions"]
    failures += check(
        "startup migrations preserve existing Wave Tax archive rows",
        len(preserved) == 1 and preserved[0]["id"] == "preserved",
    )
    failures += check(
        "Tax router no longer imports Wave or exposes sync helpers",
        "wave_client" not in tax.__dict__
        and not hasattr(tax, "sync_transactions")
        and not hasattr(tax, "categorize_transactions"),
    )
    failures += check(
        "legacy categorization implementation has no registered Tax API path",
        not any(
            getattr(route, "path", "").startswith("/api/tax/categorize")
            for route in tax.router.routes
        ),
    )

    if failures:
        print(f"FAIL: {failures} retired Tax sync checks failed.")
        return 1
    print("PASS: Retired Tax sync checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
