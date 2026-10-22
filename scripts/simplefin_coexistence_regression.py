"""Step 4 proof that Plaid and SimpleFIN history coexist without collision."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-coexist-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def bank(source, account, txn, amount, name):
    db.upsert_bank_transaction({
        "source": source, "transaction_id": txn, "account_id": account,
        "date": "2026-08-10", "authorized_date": None,
        "amount_cents": amount, "merchant_name": name, "name": name,
        "pending": 0, "category_primary": "", "category_detailed": "",
        "removed": 0, "synced_at": "2026-08-12T00:00:00Z",
    })


def payout(payout_id, amount):
    db.upsert_shopify_payout({
        "payout_id": payout_id, "amount_cents": amount,
        "date": "2026-08-10", "status": "paid", "currency": "USD",
        "fee_amount_cents": 0, "gross_amount_cents": amount,
        "summary_json": "{}", "synced_at": "2026-08-12T00:00:00Z",
    })


def run():
    db.init_db()
    checking = ledger.account_by_code("1010")
    # A distinct, dedicated Ledger asset account for the Plaid side: the
    # global partial unique index on bank_accounts.ledger_account_id
    # (added alongside SimpleFIN's mapping_state column) means two
    # different bank-account identities, even across sources, can no
    # longer share one Ledger account. This test is about transaction-id
    # coexistence, not about the mapping itself, so any second cash
    # account would do -- but it must be one created just for this test,
    # not "1090" Transfer Clearing, which ledger_sync._sync_transfer uses
    # as a real suspense account for half-matched transfers; reusing it
    # here would quietly pollute its balance for any test that later
    # asserts on actual transfer-clearing behavior.
    plaid_cash = ledger.create_account("Test Plaid Cash", "asset", parent_code="1000")
    db.upsert_bank_account({
        "source": "simplefin", "account_id": "sf-checking",
        "name": "SimpleFIN Checking", "official_name": None, "type": "",
        "subtype": "", "mask": None, "synced_at": "2026-08-12T00:00:00Z",
    })
    db.set_bank_ledger_account("simplefin", "sf-checking", checking["id"])
    db.upsert_bank_account({
        "source": "plaid", "account_id": "plaid-checking",
        "name": "Plaid Checking", "official_name": None,
        "type": "depository", "subtype": "checking", "mask": None,
        "synced_at": "2026-08-12T00:00:00Z",
    })
    db.set_bank_ledger_account("plaid", "plaid-checking", plaid_cash["id"])

    # Same provider-level ID, deliberately. Source/account-scoped storage must
    # retain both while descriptors and amounts independently identify payouts.
    bank("plaid", "plaid-checking", "shared-id", 10000,
         "Shopify payout plaid-payout")
    bank("simplefin", "sf-checking", "shared-id", 20000,
         "Shopify payout sf-payout")
    payout("plaid-payout", 10000)
    payout("sf-payout", 20000)

    print("Union and identity")
    rows = db.list_bank_transactions_for_ledger()
    check("Plaid and SimpleFIN rows with the same ID both survive",
          len(rows) == 2 and {row["source"] for row in rows}
          == {"plaid", "simplefin"})
    check("source references cannot collide",
          len({ledger_sync.bank_transaction_reference(row) for row in rows}) == 2)

    print("Shared payout workflow")
    plaid_match, plaid_reason = ledger_sync.match_payout_to_bank(
        db.list_shopify_payouts()[0], rows)
    matches = {}
    for item in db.list_shopify_payouts():
        matched, reason = ledger_sync.match_payout_to_bank(item, rows)
        matches[item["payout_id"]] = (matched, reason)
    check("each payout selects its exact source row",
          matches["plaid-payout"][0]["source"] == "plaid"
          and matches["sf-payout"][0]["source"] == "simplefin"
          and all(reason is None for _matched, reason in matches.values()))

    first = ledger_sync.sync_ledger_from_sources()
    second = ledger_sync.sync_ledger_from_sources()
    check("both payouts post once through the shared Ledger workflow",
          first["shopify_payouts"]["created"] == 2
          and second["shopify_payouts"]["created"] == 0
          and second["shopify_payouts"]["already_imported"] == 2)
    with db.get_connection() as conn:
        payout_entries = conn.execute(
            "SELECT COUNT(*) FROM journal_entries "
            "WHERE source_reference LIKE 'shopify:payout:%'"
        ).fetchone()[0]
    check("coexisting source rows do not double-count payouts",
          payout_entries == 2)

    print("Ambiguity refusal")
    bank("simplefin", "sf-checking", "second-same-amount", 3333,
         "Shopify payout ambiguous")
    bank("plaid", "plaid-checking", "other-same-amount", 3333,
         "Shopify payout ambiguous")
    ambiguous = {"payout_id": "ambiguous", "amount_cents": 3333,
                 "date": "2026-08-10"}
    matched, reason = ledger_sync.match_payout_to_bank(
        ambiguous, db.list_bank_transactions_for_ledger())
    check("cross-source ambiguity is refused rather than guessed",
          matched is None
          and reason == ledger_sync.SkipReason.AMBIGUOUS_PAYOUT_MATCH)

    sheet = ledger.balance_sheet("2026-08-12")
    check("coexisting-source entries preserve the accounting identity",
          sheet["is_balanced"] and sheet["difference"] == 0)

    if failures:
        print(f"FAIL: {failures} of {checks} coexistence checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} coexistence checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
