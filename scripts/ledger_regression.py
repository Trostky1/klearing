"""Offline regression coverage for Klearing's Phase 1 native ledger."""

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-ledger-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ledger  # noqa: E402
import ledger_sync  # noqa: E402


def check(label, condition):
    print(f"PASS: {label}" if condition else f"FAIL: {label}")
    return 0 if condition else 1


def main():
    failures = 0
    db.init_db()
    accounts = ledger.list_accounts()
    by_code = {row["code"]: row for row in accounts}
    failures += check("ledger tables and starter accounts exist",
                      len(accounts) >= 18 and {"1000", "1010", "4000", "6000"}
                      <= set(by_code))
    failures += check(
        "bank rewards and cashback has a distinct revenue account",
        by_code["4210"]["name"] == "Bank Rewards & Cashback Income"
        and by_code["4210"]["type"] == "revenue",
    )
    failures += check("starter sub-account has parent",
                      by_code["1010"]["parent_account_id"] == by_code["1000"]["id"])

    entry = ledger.create_journal_entry(
        "2026-07-23", "Owner contribution", [
            {"account_id": by_code["1010"]["id"], "debit_amount": "1000.00",
             "credit_amount": "0"},
            {"account_id": by_code["3000"]["id"], "debit_amount": "0",
             "credit_amount": "1000.00"},
        ], created_by="regression", source_reference="test:owner-contribution")
    failures += check("balanced journal entry saves", entry["id"] > 0)

    balance = {row["code"]: row for row in ledger.trial_balance()}
    failures += check("trial balance reflects the entry",
                      balance["1010"]["normal_balance"] == 1000
                      and balance["3000"]["normal_balance"] == 1000)
    failures += check("trial balance remains balanced",
                      sum(row["debit_total"] for row in balance.values())
                      == sum(row["credit_total"] for row in balance.values()))

    try:
        ledger.create_journal_entry(
            "2026-07-23", "Unbalanced test", [
                {"account_id": by_code["1010"]["id"], "debit_amount": 5},
                {"account_id": by_code["3000"]["id"], "credit_amount": 4},
            ], created_by="regression")
    except ledger.LedgerValidationError:
        failures += check("unbalanced journal entry is rejected", True)
    else:
        failures += check("unbalanced journal entry is rejected", False)

    # Report fixtures are isolated to July so the August entry proves date
    # filtering without mixing in the source-sync fixtures below.
    ledger.create_journal_entry(
        "2026-07-10", "Sample sale", [
            {"account_id": by_code["1010"]["id"], "debit_amount": "1500.00",
             "credit_amount": "0"},
            {"account_id": by_code["4000"]["id"], "debit_amount": "0",
             "credit_amount": "1500.00"},
        ], created_by="regression", source_reference="test:sample-sale")
    ledger.create_journal_entry(
        "2026-07-10", "Sample inventory purchase", [
            {"account_id": by_code["1200"]["id"], "debit_amount": "400.00",
             "credit_amount": "0"},
            {"account_id": by_code["2000"]["id"], "debit_amount": "0",
             "credit_amount": "400.00"},
        ], created_by="regression", source_reference="test:inventory-purchase")
    ledger.create_journal_entry(
        "2026-07-10", "Sample cost of goods sold", [
            {"account_id": by_code["5000"]["id"], "debit_amount": "400.00",
             "credit_amount": "0"},
            {"account_id": by_code["1200"]["id"], "debit_amount": "0",
             "credit_amount": "400.00"},
        ], created_by="regression", source_reference="test:sample-cogs")
    ledger.create_journal_entry(
        "2026-07-10", "Sample operating expense", [
            {"account_id": by_code["6030"]["id"], "debit_amount": "100.00",
             "credit_amount": "0"},
            {"account_id": by_code["1010"]["id"], "debit_amount": "0",
             "credit_amount": "100.00"},
        ], created_by="regression", source_reference="test:sample-expense")
    ledger.create_journal_entry(
        "2026-08-01", "Out of range sale", [
            {"account_id": by_code["1010"]["id"], "debit_amount": "250.00",
             "credit_amount": "0"},
            {"account_id": by_code["4000"]["id"], "debit_amount": "0",
             "credit_amount": "250.00"},
        ], created_by="regression", source_reference="test:august-sale")

    july_pnl = ledger.profit_and_loss("2026-07-01", "2026-07-31")
    revenue_by_code = {row["code"]: row for row in july_pnl["revenue_by_account"]}
    cogs_by_code = {row["code"]: row for row in july_pnl["cogs_by_account"]}
    operating_by_code = {
        row["code"]: row for row in july_pnl["operating_expenses_by_account"]
    }
    failures += check("P&L calculates revenue, COGS, gross profit, and net income",
                      july_pnl["revenue_total"] == Decimal("1500")
                      and july_pnl["cogs_total"] == Decimal("400")
                      and july_pnl["gross_profit"] == Decimal("1100")
                      and july_pnl["operating_expenses_total"] == Decimal("100")
                      and july_pnl["net_income"] == Decimal("1000")
                      and revenue_by_code["4000"]["amount"] == Decimal("1500")
                      and cogs_by_code["5000"]["amount"] == Decimal("400")
                      and operating_by_code["6030"]["amount"] == Decimal("100"))
    august_pnl = ledger.profit_and_loss("2026-08-01", "2026-08-31")
    failures += check("P&L date range is inclusive and excludes other dates",
                      august_pnl["revenue_total"] == Decimal("250")
                      and july_pnl["revenue_total"] != august_pnl["revenue_total"])

    july_balance_sheet = ledger.balance_sheet("2026-07-31")
    failures += check("Balance Sheet balances with current earnings",
                      july_balance_sheet["assets_total"] == Decimal("2400")
                      and july_balance_sheet["liabilities_total"] == Decimal("400")
                      and july_balance_sheet["equity_accounts_total"] == Decimal("1000")
                      and july_balance_sheet["current_earnings"] == Decimal("1000")
                      and july_balance_sheet["equity_total"] == Decimal("2000")
                      and july_balance_sheet["balances"]
                      and july_balance_sheet["difference"] == Decimal("0"))

    # Deliberately bypass the application validator to prove the report's
    # balance check flags a corrupt persisted entry.
    with db.get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO journal_entries "
            "(date, memo, created_by, source_reference, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-07-31", "Artificial imbalance", "regression",
             "test:artificial-imbalance", "2026-07-31T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO journal_lines "
            "(journal_entry_id, account_id, debit_amount, credit_amount) "
            "VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, by_code["1010"]["id"], 100, 0),
        )
    imbalanced_sheet = ledger.balance_sheet("2026-07-31")
    failures += check("Balance Sheet flags an artificial imbalance",
                      not imbalanced_sheet["balances"]
                      and imbalanced_sheet["difference"] == Decimal("1"))

    db.upsert_plaid_transaction({
        "transaction_id": "plaid-bank-fee",
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": -1250,
        "merchant_name": "Bank fee",
        "name": "Monthly fee",
        "pending": 0,
        "category_primary": "BANK_FEES",
        "category_detailed": "BANK_FEES",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_transaction({
        "transaction_id": "plaid-software",
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": -2999,
        "merchant_name": "Example SaaS",
        "name": "Example SaaS",
        "pending": 0,
        "category_primary": "GENERAL_SERVICES",
        "category_detailed": "SOFTWARE",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_transaction({
        "transaction_id": "plaid-uncategorized",
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": -500,
        "merchant_name": "Unknown merchant",
        "name": "Unknown merchant",
        "pending": 0,
        "category_primary": "",
        "category_detailed": "",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    for transaction_id, account_id, amount_cents, detailed in (
        ("plaid-interest", "bank-1", 125, "INTEREST_EARNED"),
        ("plaid-bank-reward", "bank-1", 99, "BANK_REWARDS"),
        ("plaid-refund", "bank-1", 750, "REFUND"),
        ("plaid-unknown-deposit", "bank-1", 900, "OTHER_INCOME"),
        ("plaid-cashback-named-merchant", "bank-1", 80, "OTHER_INCOME"),
        ("plaid-transfer-out", "checking-1", -5000, "TRANSFER_OUT"),
        ("plaid-transfer-in", "savings-1", 5000, "TRANSFER_IN"),
        ("plaid-transfer-one-sided", "checking-1", -3000, "TRANSFER_OUT"),
        ("plaid-prior-deposit", "bank-1", 1100, "OTHER_INCOME"),
    ):
        db.upsert_plaid_transaction({
            "transaction_id": transaction_id, "account_id": account_id,
            "date": "2026-07-24", "authorized_date": None,
            "amount_cents": amount_cents, "merchant_name": transaction_id,
            "name": transaction_id, "pending": 0,
            "category_primary": "INCOME" if "TRANSFER" not in detailed else "TRANSFER",
            "category_detailed": detailed, "removed": 0,
            "synced_at": "2026-07-24T00:00:00+00:00",
        })
    # A merchant name alone must not decide income classification. Its generic
    # Plaid category keeps this deposit in the human-review account.
    db.upsert_plaid_transaction({
        "transaction_id": "plaid-cashback-named-merchant",
        "account_id": "bank-1", "date": "2026-07-24",
        "authorized_date": None, "amount_cents": 80,
        "merchant_name": "Cashback Supply Co.", "name": "Cash Back rebate",
        "pending": 0, "category_primary": "INCOME",
        "category_detailed": "OTHER_INCOME", "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_account({
        "account_id": "bank-1", "name": "Payout Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "0001", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.set_bank_ledger_account("plaid", "bank-1", by_code["1010"]["id"])
    db.upsert_plaid_account({
        "account_id": "checking-1", "name": "Operating Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "1111", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_account({
        "account_id": "savings-1", "name": "Reserve Savings",
        "official_name": None, "type": "depository", "subtype": "savings",
        "mask": "2222", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_transaction({
        "transaction_id": "plaid-shopify-payout",
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": 10000,
        "merchant_name": "Shopify Payments",
        "name": "Shopify payout",
        "pending": 0,
        "category_primary": "INCOME",
        "category_detailed": "",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_plaid_transaction({
        "transaction_id": "plaid-shopify-fee-payout",
        "account_id": "bank-1",
        "date": "2026-07-24",
        "authorized_date": None,
        "amount_cents": 9700,
        "merchant_name": "Shopify Payments",
        "name": "Shopify payout",
        "pending": 0,
        "category_primary": "INCOME",
        "category_detailed": "",
        "removed": 0,
        "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_shopify_payout({
        "payout_id": "payout-basic", "amount_cents": 10000,
        "date": "2026-07-24", "status": "paid", "currency": "USD",
        "fee_amount_cents": 0, "gross_amount_cents": 10000,
        "summary_json": "{}", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_shopify_payout({
        "payout_id": "payout-fee", "amount_cents": 9700,
        "date": "2026-07-24", "status": "paid", "currency": "USD",
        "fee_amount_cents": 300, "gross_amount_cents": 10000,
        "summary_json": "{}", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_shopify_payout({
        "payout_id": "payout-unmatched", "amount_cents": 1234,
        "date": "2026-07-24", "status": "paid", "currency": "USD",
        "fee_amount_cents": 0, "gross_amount_cents": 1234,
        "summary_json": "{}", "synced_at": "2026-07-24T00:00:00+00:00",
    })
    db.upsert_shopify_order({
        "order_id": "paid-order",
        "created_at": "2026-07-24 09:00:00",
        "total_price": 100,
        "subtotal_price": 100,
        "total_discounts": 0,
        "total_tax": 0,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "customer_id": "",
        "customer_email": "",
        "customer_name": "",
        "source": "online_store",
        "refund_total": 0,
        "line_items_json": "[]",
    })
    db.upsert_shopify_order({
        "order_id": "pending-order",
        "created_at": "2026-07-24 10:00:00",
        "total_price": 75,
        "subtotal_price": 75,
        "total_discounts": 0,
        "total_tax": 0,
        "financial_status": "pending",
        "fulfillment_status": "unfulfilled",
        "customer_id": "",
        "customer_email": "",
        "customer_name": "",
        "source": "online_store",
        "refund_total": 0,
        "line_items_json": "[]",
    })
    db.record_shopify_variant_cost({
        "variant_id": "variant-with-cost",
        "captured_at": "2026-07-24 00:00:00",
        "inventory_item_id": "inventory-1",
        "unit_cost_cents": 400,
        "cost_status": "available",
        "detail": "",
    })
    db.record_shopify_variant_cost({
        "variant_id": "variant-with-cost",
        "captured_at": "2026-07-26 00:00:00",
        "inventory_item_id": "inventory-1",
        "unit_cost_cents": 999,
        "cost_status": "available",
        "detail": "later cost must not be used for the earlier order",
    })
    db.upsert_shopify_order({
        "order_id": "cogs-order",
        "created_at": "2026-07-25 10:00:00",
        "total_price": 60,
        "subtotal_price": 60,
        "total_discounts": 0,
        "total_tax": 0,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "customer_id": "",
        "customer_email": "",
        "customer_name": "",
        "source": "online_store",
        "refund_total": 0,
        "line_items_json": (
            '[{"variant_id": "variant-with-cost", "title": "Costed item", '
            '"quantity": 3, "price": "20.00"}]'
        ),
    })
    db.upsert_shopify_order({
        "order_id": "missing-cost-order",
        "created_at": "2026-07-25 11:00:00",
        "total_price": 20,
        "subtotal_price": 20,
        "total_discounts": 0,
        "total_tax": 0,
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "customer_id": "",
        "customer_email": "",
        "customer_name": "",
        "source": "online_store",
        "refund_total": 0,
        "line_items_json": (
            '[{"variant_id": "variant-without-cost", "title": "No-cost item", '
            '"quantity": 1, "price": "20.00"}]'
        ),
    })
    # Simulate a deposit posted by the previous sync logic before it learned
    # about Uncategorized Income. The new sync must add an idempotent reclass.
    ledger.create_sourced_journal_entry(
        "2026-07-24", "Legacy Plaid deposit", [
            {"account_id": by_code["1010"]["id"], "debit_amount": "11.00"},
            {"account_id": by_code["4000"]["id"], "credit_amount": "11.00"},
        ], source_reference="plaid:transaction:plaid-prior-deposit",
        created_by="legacy-ledger-source-sync",
    )
    # Forward-only: an already-posted 4190 entry remains historical, even
    # though its source's category is now confident enough for future imports.
    legacy_reward = next(
        item for item in db.list_plaid_transactions_for_ledger()
        if item["transaction_id"] == "plaid-bank-reward"
    )
    ledger.create_sourced_journal_entry(
        legacy_reward["date"], "Legacy Plaid bank reward", [
            {"account_id": by_code["1010"]["id"], "debit_amount": "0.99"},
            {"account_id": by_code["4190"]["id"], "credit_amount": "0.99"},
        ], source_reference="plaid:transaction:plaid-prior-bank-reward",
        created_by="legacy-ledger-source-sync",
    )
    legacy_reward["transaction_id"] = "plaid-prior-bank-reward"
    db.upsert_plaid_transaction(legacy_reward)
    source_sync = ledger_sync.sync_ledger_from_sources()
    # Ordinary bank transactions are deliberately not here: they reach the
    # books by being approved in Review. What the pipeline still posts on its
    # own is transfers (no category to choose), Shopify orders, and payouts.
    failures += check("source sync creates transfer, Shopify, and payout entries",
                      source_sync["created"] == 8
                      and source_sync["plaid"]["created"] == 2
                      and source_sync["shopify"]["created"] == 4
                      and source_sync["shopify_payouts"]["created"] == 2)

    with db.get_connection() as conn:
        source_lines = {}
        for row in conn.execute(
            "SELECT si.source_reference, a.code, jl.debit_amount, jl.credit_amount "
            "FROM ledger_source_imports si "
            "JOIN journal_lines jl ON jl.journal_entry_id = si.journal_entry_id "
            "JOIN accounts a ON a.id = jl.account_id"
        ):
            source_lines.setdefault(row["source_reference"], []).append(dict(row))
        entry_count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
        cogs_skips = {row["source_reference"]: dict(row) for row in conn.execute(
            "SELECT * FROM ledger_source_import_skips"
        )}
    # Named rather than pattern-matched: the fixture deliberately pre-seeds
    # two legacy plaid: entries to represent history posted by an older build,
    # and those must stay put. What must NOT appear is anything this sync
    # posted from a bank transaction of its own accord.
    failures += check(
        "ordinary bank transactions are left for Review",
        not any(f"plaid:transaction:{name}" in source_lines for name in (
            "plaid-bank-fee", "plaid-software", "plaid-uncategorized",
            "plaid-interest", "plaid-bank-reward", "plaid-refund",
            "plaid-unknown-deposit", "plaid-cashback-named-merchant")))
    failures += check("paired Plaid transfers move between connected accounts",
                      {line["code"] for line in
                       source_lines["plaid:transfer:plaid-transfer-in:plaid-transfer-out"]}
                      == {"bank:plaid:checking-1", "bank:plaid:savings-1"})
    failures += check("one-sided Plaid transfers remain visible through clearing",
                      {line["code"] for line in
                       source_lines["plaid:transaction:plaid-transfer-one-sided:transfer"]}
                      == {"1090", "bank:plaid:checking-1"})
    failures += check("Shopify paid and pending use clearing correctly",
                      {line["code"] for line in source_lines["shopify:order:paid-order"]}
                      == {"1015", "4000"}
                      and {line["code"] for line in source_lines["shopify:order:pending-order"]}
                      == {"1100", "4000"}
                      )
    failures += check("Shopify payout clears Clearing into Cash",
                      {line["code"] for line in source_lines["shopify:payout:payout-basic"]}
                      == {"1010", "1015"}
                      and sum(line["debit_amount"] for line in
                              source_lines["shopify:payout:payout-basic"]) == 10000)
    failures += check("Shopify payout fees post to Payment Processing Fees",
                      {line["code"] for line in source_lines["shopify:payout:payout-fee"]}
                      == {"1010", "1015", "6100"}
                      and sum(line["debit_amount"] for line in
                              source_lines["shopify:payout:payout-fee"]) == 10000
                      and sum(line["credit_amount"] for line in
                              source_lines["shopify:payout:payout-fee"]) == 10000)
    failures += check("COGS is skipped until inventory acquisition is tracked",
                      cogs_skips["shopify:order:cogs-order:cogs"]["reason"]
                      == "inventory_acquisition_not_tracked")
    failures += check("orders missing cost data are logged and skipped",
                      cogs_skips["shopify:order:missing-cost-order:cogs"]["reason"]
                      == "missing_cost_data"
                      and "no cost snapshot" in
                      cogs_skips["shopify:order:missing-cost-order:cogs"]["detail"].lower())
    failures += check("unmatched Shopify payouts are logged",
                      cogs_skips["shopify:payout:payout-unmatched"]["reason"]
                      == "unmatched_payout")
    failures += check("all sourced journal entries are balanced",
                      all(sum(line["debit_amount"] for line in lines)
                          == sum(line["credit_amount"] for line in lines)
                          for lines in source_lines.values()))

    second_source_sync = ledger_sync.sync_ledger_from_sources()
    with db.get_connection() as conn:
        second_entry_count = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    failures += check("source sync is idempotent",
                      second_source_sync["created"] == 0
                      and second_source_sync["plaid"]["already_imported"] == 3
                      and second_source_sync["shopify"]["already_imported"] == 4
                      and second_source_sync["shopify_payouts"]["already_imported"] == 2
                      and second_source_sync["shopify_cogs"]["already_imported"] == 0
                      and second_entry_count == entry_count)

    # --- Retired behaviour, asserted directly rather than through the pipeline.
    # _sync_plaid and _reclassify_misclassified_plaid_deposits no longer run:
    # approval posts bank transactions now. Both are kept until Phase 5 of the
    # Wave offboarding, and their routing is still the written record of which
    # account each kind of transaction belongs to, so it stays covered. These
    # call the functions directly and go last, because they add entries the
    # idempotency check above deliberately counts.
    retired_summary = {"plaid": {"created": 0, "already_imported": 0,
                                 "skipped": 0, "reclassified": 0}}
    retired_accounts = ledger_sync._accounts_by_code()
    ledger_sync._sync_plaid(retired_accounts, retired_summary)
    ledger_sync._reclassify_misclassified_plaid_deposits(
        retired_accounts, retired_summary)
    with db.get_connection() as conn:
        retired_lines = {}
        for row in conn.execute(
            "SELECT si.source_reference, a.code "
            "FROM ledger_source_imports si "
            "JOIN journal_lines jl ON jl.journal_entry_id = si.journal_entry_id "
            "JOIN accounts a ON a.id = jl.account_id"
        ):
            retired_lines.setdefault(row["source_reference"], []).append(dict(row))

    def codes(reference):
        return {line["code"] for line in retired_lines.get(reference, [])}

    failures += check("[retired] Plaid maps bank fees and software correctly",
                      codes("plaid:transaction:plaid-bank-fee") == {"1010", "6020"}
                      and codes("plaid:transaction:plaid-software") == {"1010", "6030"})
    failures += check("[retired] Plaid fallback uses Uncategorized Expense",
                      codes("plaid:transaction:plaid-uncategorized")
                      == {"1010", "6080"})
    failures += check(
        "[retired] Plaid deposits use interest, rewards, refund, and uncategorized income",
        codes("plaid:transaction:plaid-interest") == {"1010", "4200"}
        and codes("plaid:transaction:plaid-bank-reward") == {"1010", "4210"}
        and codes("plaid:transaction:plaid-refund") == {"1010", "6090"}
        and codes("plaid:transaction:plaid-unknown-deposit") == {"1010", "4190"}
        and codes("plaid:transaction:plaid-cashback-named-merchant")
        == {"1010", "4190"})
    failures += check(
        "[retired] existing Uncategorized Income entries stay untouched",
        codes("plaid:transaction:plaid-prior-bank-reward") == {"1010", "4190"}
        and "ledger:reclass:plaid:transaction:plaid-prior-bank-reward"
        not in retired_lines)
    failures += check(
        "[retired] prior Plaid deposits are reclassified out of Sales Revenue",
        codes("ledger:reclass:plaid:transaction:plaid-prior-deposit")
        == {"4000", "4190"})

    if failures:
        print(f"FAIL: {failures} ledger checks failed.")
        return 1
    print("PASS: ledger regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
