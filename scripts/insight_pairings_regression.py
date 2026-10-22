"""Offline regression coverage for Feature B's cross-source pairings."""

import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-insight-pairings-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import insight_metrics  # noqa: E402
import inventory  # noqa: E402
import ledger_sync  # noqa: E402
import plaid_client  # noqa: E402
import shopify_client  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def order(order_id, order_date, total, quantity, variant="variant-1"):
    db.upsert_shopify_order({
        "order_id": order_id, "created_at": order_date.isoformat(),
        "total_price": total, "subtotal_price": total,
        "total_discounts": 0, "total_tax": 0,
        "financial_status": "paid", "fulfillment_status": "fulfilled",
        "customer_id": "", "customer_email": "", "customer_name": "",
        "source": "online_store", "refund_total": 0,
        "line_items_json": json.dumps([{
            "variant_id": variant, "title": "Fixture", "quantity": quantity,
        }]),
    })


def plaid(transaction_id, transaction_date, amount, name, category="INCOME"):
    db.upsert_plaid_transaction({
        "transaction_id": transaction_id, "account_id": "bank-1",
        "date": transaction_date.isoformat(), "authorized_date": None,
        "amount_cents": amount, "merchant_name": name, "name": name,
        "pending": 0, "category_primary": category,
        "category_detailed": "", "removed": 0,
        "synced_at": date.today().isoformat(),
    })


def payout(payout_id, payout_date, amount):
    db.upsert_shopify_payout({
        "payout_id": payout_id, "amount_cents": amount,
        "date": payout_date.isoformat(), "status": "paid", "currency": "USD",
        "fee_amount_cents": 0, "gross_amount_cents": amount,
        "summary_json": "{}", "synced_at": date.today().isoformat(),
    })


def main():
    failures = 0
    db.init_db()
    windows = insight_metrics.rolling_periods()
    prior_start, _prior_end = windows[-2]
    current_start, _current_end = windows[-1]
    prior_date = prior_start + timedelta(days=3)
    current_date = current_start + timedelta(days=3)

    shopify_client.is_configured = lambda: True
    shopify_client.get_store = lambda: "fixture.myshopify.com"
    plaid_client.is_configured = lambda: True

    inventory.record_acquisition(
        "variant-1", 20, "40.00", (prior_start - timedelta(days=1)).isoformat(),
        funding_account_code="1010")
    order("prior-order", prior_date, 100, 1)
    order("current-order", current_date, 200, 2)

    plaid("prior-expense", prior_date, -2000, "Software", "SOFTWARE")
    plaid("current-expense", current_date, -10000, "Software", "SOFTWARE")
    plaid("prior-payout-bank", prior_date, 10000, "Shopify payout prior-payout")
    plaid("current-payout-bank", current_date, 20000,
          "Shopify payout current-payout")
    payout("prior-payout", prior_date, 10000)
    payout("current-payout", current_date, 20000)
    payout("unmatched-payout", current_date, 5000)

    ledger_sync.sync_ledger_from_sources()
    metrics = {metric["id"]: metric
               for metric in insight_metrics.get_insight_metrics()}
    failures += check(
        "payout pairing reuses exact bank matching and excludes unmatched payouts",
        metrics["shopify_payouts_matched"]["value"] == 20000
        and metrics["shopify_payouts_matched"]["delta"] == 10000)
    failures += check(
        "inventory margin uses only posted revenue and exact posted FIFO COGS",
        metrics["inventory_gross_margin"]["value"] == 60.0
        and metrics["inventory_gross_margin"]["delta"] == 0.0)
    failures += check(
        "expense growth is compared with revenue growth in plain language",
        metrics["expense_growth_vs_revenue"]["value"] == 300.0
        and metrics["expense_growth_vs_revenue"]["label"]
        == "Software costs are climbing faster than Shopify revenue")
    failures += check(
        "pairing metrics deep-link to the correct Shopify and Plaid pages",
        metrics["shopify_payouts_matched"]["deep_link"]
        == "https://fixture.myshopify.com/admin/settings/payments"
        and metrics["inventory_gross_margin"]["deep_link"]
        == "https://fixture.myshopify.com/admin/orders"
        and metrics["expense_growth_vs_revenue"]["deep_link"]
        == "https://my.plaid.com/")

    ambiguous_payout = {
        "payout_id": "ambiguous", "date": current_date.isoformat(),
        "amount_cents": 7777,
    }
    ambiguous_transactions = [
        {"transaction_id": "a", "date": current_date.isoformat(),
         "amount_cents": 7777, "merchant_name": "Shopify", "name": "Payout"},
        {"transaction_id": "b", "date": current_date.isoformat(),
         "amount_cents": 7777, "merchant_name": "Shopify", "name": "Payout"},
    ]
    matched, reason = ledger_sync.match_payout_to_plaid(
        ambiguous_payout, ambiguous_transactions)
    failures += check(
        "the shared payout matcher still refuses ambiguity",
        matched is None and reason == ledger_sync.SkipReason.AMBIGUOUS_PAYOUT_MATCH)

    # A newly posted order without tracked acquisition coverage invalidates
    # the whole current-period margin instead of creating a partial headline.
    order("uncosted-order", current_date + timedelta(days=1), 50, 1,
          variant="missing-variant")
    ledger_sync.sync_ledger_from_sources()
    after_gap = {metric["id"]: metric
                 for metric in insight_metrics.get_insight_metrics()}
    failures += check(
        "an uncosted order refuses the period margin rather than approximating",
        after_gap["inventory_gross_margin"]["value"] is None
        and after_gap["inventory_gross_margin"]["delta"] is None)

    if failures:
        print(f"FAIL: {failures} Insights pairing checks failed.")
        return 1
    print("PASS: Insights pairing checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
