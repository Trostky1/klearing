"""Step 3 proof: adapter rows feed one source-neutral local bank cache."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-bank-source-sync-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import banks  # noqa: E402
import check_bank  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import ledger_sync  # noqa: E402


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    print(("  PASS  " if condition else "  FAIL  ") + label)
    failures += 0 if condition else 1


class FixtureAdapter(banks.BankAdapter):
    id = "simplefin"
    display_name = "SimpleFIN fixture"

    def __init__(self):
        self.rows = [
            {"external_id": "simplefin:acct-1:shared", "account_id": "acct-1",
             "date": "2026-08-12", "amount": 25.00,
             "counterparty": "Shopify payout p-25"},
            {"external_id": "simplefin:acct-2:shared", "account_id": "acct-2",
             "date": "2026-08-12", "amount": -8.50,
             "counterparty": "Office shop"},
        ]

    def credential_fields(self):
        return []

    def stored_credentials(self):
        return {"access_url": "fixture"}

    def list_accounts(self, credentials):
        return [{"id": "acct-1", "name": "Checking", "balance": 25},
                {"id": "acct-2", "name": "Card", "balance": -8.5}]

    def fetch_transactions(self, credentials, since_date=None):
        return list(self.rows)


def run():
    db.init_db()
    adapter = FixtureAdapter()
    original_active = check_bank.banks.active_adapter
    check_bank.banks.active_adapter = lambda: adapter
    try:
        queued = check_bank.run_check()
        print("Shared persistence")
        cached = db.list_bank_transactions_for_ledger(source="simplefin")
        check("one fetch feeds both Review and the canonical cache",
              len(queued) == 2 and len(cached) == 2)
        check("canonical signs are unchanged at shared persistence",
              {row["amount_cents"] for row in cached} == {2500, -850})
        check("same transaction ID in two accounts does not collide",
              {row["account_id"] for row in cached} == {"acct-1", "acct-2"})
        with db.get_connection() as conn:
            account_count = conn.execute(
                "SELECT COUNT(*) FROM bank_accounts WHERE source='simplefin'"
            ).fetchone()[0]
        check("source accounts are cached without guessed Ledger mappings",
              account_count == 2
              and all(db.bank_ledger_account_id("simplefin", account) is None
                      for account in ("acct-1", "acct-2")))

        adapter.rows = []
        check_bank.run_check()
        check("a missing refetch never deletes prior SimpleFIN rows",
              len(db.list_bank_transactions_for_ledger("simplefin")) == 2)

        print("Fatal mapping boundary")
        expense = next(item for item in queued if item["direction"] == "expense")
        chosen = ledger.account_by_code("6040")
        try:
            app._post_one_to_ledger(expense, chosen, "")
        except ledger.LedgerValidationError as exc:
            unmapped_failed = "has not been mapped" in str(exc)
        else:
            unmapped_failed = False
        check("unmapped SimpleFIN approval fails instead of guessing Checking",
              unmapped_failed)
        checking = ledger.account_by_code("1010")
        db.set_bank_ledger_account("simplefin", "acct-2", checking["id"])
        entry = app._post_one_to_ledger(expense, chosen, "")
        check("explicitly mapped account can post through the shared approval path",
              entry is not None
              and entry["source_reference"].startswith("bank:simplefin:acct-2:"))

        db.upsert_plaid_transaction({
            "transaction_id": "unmapped-transfer", "account_id": "missing",
            "date": "2026-08-12", "authorized_date": None,
            "amount_cents": -2900, "merchant_name": "Transfer",
            "name": "Transfer", "pending": 0,
            "category_primary": "TRANSFER",
            "category_detailed": "TRANSFER_OUT", "removed": 0,
            "synced_at": "2026-08-12T00:00:00Z",
        })
        transfer_reference = (
            "plaid:transaction:unmapped-transfer:transfer")
        with db.get_connection() as conn:
            before_sync = conn.execute(
                "SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        transfer_summary = ledger_sync.sync_ledger_from_sources()
        with db.get_connection() as conn:
            after_sync = conn.execute(
                "SELECT COUNT(*) FROM journal_entries").fetchone()[0]
            transfer_skip = conn.execute(
                "SELECT reason FROM ledger_source_import_skips "
                "WHERE source_reference = ?", (transfer_reference,)
            ).fetchone()
        check("unmapped Plaid transfer sync creates zero journal entries",
              after_sync == before_sync
              and transfer_summary["plaid"]["created"] == 0)
        check("unmapped Plaid transfer is logged for manual resolution",
              transfer_skip is not None
              and transfer_skip["reason"] == "unmapped_bank_account")

        print("Source-neutral matching")
        payout = {"payout_id": "p-25", "date": "2026-08-12",
                  "amount_cents": 2500}
        matched, reason = ledger_sync.match_payout_to_bank(
            payout, db.list_bank_transactions_for_ledger())
        check("SimpleFIN deposit participates in the shared payout matcher",
              matched is not None and matched["source"] == "simplefin"
              and reason is None)
    finally:
        check_bank.banks.active_adapter = original_active

    if failures:
        print(f"FAIL: {failures} of {checks} bank-source checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} bank-source checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
