"""The Review queue governs the native Ledger.

Written in Phase 0 of the Wave offboarding to FAIL, as executable evidence
that it did not. Two independent pipelines processed the same bank feed:

  POST /approve/{item_id} -> _post_one_to_wave (app.py)   -> Wave only
  ledger_sync._sync_plaid                                  -> native Ledger

``_sync_plaid`` read ``plaid_transactions`` directly and never consulted
``pending_items``, so a transaction the user had DENIED still posted, and
the category chosen at approval was discarded in favour of one derived
from Plaid's metadata. Neither was visible from the outside: debits still
equalled credits, so ``ledger_verify.py`` passed throughout. That silence
is why this is a regression test rather than a bug report.

Phase 2 made approval the only writer of bank transactions, and this file
turned green. It is kept, unchanged in what it asserts, as the standing
guard on both halves.

The one thing that changed is how "the user approved it" is set up. Phase
0 could fake it by writing the approved status straight to the database,
because the sync was the poster and never looked. Approval is the poster
now, so the fixture drives the real path. The sync still runs afterwards,
which makes the second assertion stronger than it was: the chosen account
has to survive it.

Offline: isolated temp data dir, no network, no credentials.
"""

import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-denied-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ledger  # noqa: E402
import ledger_sync  # noqa: E402
import app  # noqa: E402

DENIED_TRANSACTION_ID = "plaid-denied-by-user"
APPROVED_TRANSACTION_ID = "plaid-approved-with-category"

# The Ledger account the user picks at approval time. Deliberately not a
# code _plaid_expense_account can arrive at on its own: Plaid metadata is
# blank on both fixtures, so its only possible answer is 6080.
USER_CHOSEN_CODE = "6010"          # Advertising
PLAID_FALLBACK_CODE = "6080"       # Uncategorized Expense


def check(label, condition):
    print(f"PASS: {label}" if condition else f"FAIL: {label}")
    return 0 if condition else 1


def plaid_transaction(transaction_id, amount_cents, vendor):
    """One settled, non-removed expense with no usable Plaid category."""
    return {
        "transaction_id": transaction_id,
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": amount_cents,
        "merchant_name": vendor,
        "name": vendor,
        "pending": 0,
        "category_primary": "",
        "category_detailed": "",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    }


def source_reference(transaction_id):
    return f"plaid:transaction:{transaction_id}"


def entry_exists(reference):
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM journal_entries WHERE source_reference = ?",
            (reference,),
        ).fetchone() is not None


def main():
    failures = 0
    db.init_db()

    by_code = {row["code"]: row for row in ledger.list_accounts()}
    chosen_account_name = by_code[USER_CHOSEN_CODE]["name"]
    db.upsert_plaid_account({
        "account_id": "bank-1", "name": "Operating Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "1001", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.set_bank_ledger_account("plaid", "bank-1", by_code["1010"]["id"])

    # --- Fixture 1: the user denied it. Nothing should reach the Ledger.
    db.upsert_plaid_transaction(
        plaid_transaction(DENIED_TRANSACTION_ID, -4200, "Denied Vendor"))
    denied_id = db.insert_pending_item(
        source="plaid", vendor="Denied Vendor", amount=42.00,
        date="2026-07-24", suggested_category="Uncategorized Expense",
        direction="expense", external_transaction_id=DENIED_TRANSACTION_ID,
        account_id="bank-1")
    db.set_status(denied_id, "denied")

    # --- Fixture 2: the user approved it and chose a specific category.
    # Driven through the real approval path, with only the Wave call
    # stubbed: that still runs first and needs live credentials, and this
    # suite is offline.
    def _fake_wave_post(item, category, notes, evidence):
        db.set_category(item["id"], category)
        db.set_status(item["id"], "approved",
                      wave_transaction_id=f'stub-{item["id"]}')
        return True
    app._post_one_to_wave = _fake_wave_post

    db.upsert_plaid_transaction(
        plaid_transaction(APPROVED_TRANSACTION_ID, -1500, "Approved Vendor"))
    approved_id = db.insert_pending_item(
        source="plaid", vendor="Approved Vendor", amount=15.00,
        date="2026-07-24", suggested_category="Uncategorized Expense",
        direction="expense", external_transaction_id=APPROVED_TRANSACTION_ID,
        account_id="bank-1")
    account, name = app.resolve_submitted_category(USER_CHOSEN_CODE)
    app._post_one_approved(db.get_item(approved_id), name, account, "", None)

    # Runs after the approval on purpose: the chosen account has to survive
    # a sync, not merely be written before one.
    ledger_sync.sync_ledger_from_sources()

    print("\nThe Review queue governs what reaches the Ledger")
    failures += check(
        "a denied transaction posts no journal entry",
        not entry_exists(source_reference(DENIED_TRANSACTION_ID)))

    print("\nThe category the user chose is the category that posts")
    approved_reference = source_reference(APPROVED_TRANSACTION_ID)
    posted_codes = db.source_import_account_codes(approved_reference)
    failures += check(
        "an approved transaction posts at all",
        entry_exists(approved_reference))
    failures += check(
        f"it posts to the user's chosen account ({USER_CHOSEN_CODE} "
        f"{chosen_account_name}), not Plaid's guess "
        f"({PLAID_FALLBACK_CODE})",
        USER_CHOSEN_CODE in posted_codes
        and PLAID_FALLBACK_CODE not in posted_codes)

    # Ties out either way. That was the point in Phase 0: the bug was
    # silent, so this assertion can never be the one that catches it.
    trial = ledger.trial_balance()
    total_debits = sum(row["debit_total"] for row in trial)
    total_credits = sum(row["credit_total"] for row in trial)
    failures += check("trial balance ties out", total_debits == total_credits)

    print(f"\n{failures} failed")
    if failures:
        return 1
    print("PASS: the Review queue governs the Ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
