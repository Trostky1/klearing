"""Offline regression coverage for Feature A's backend signal contract."""

import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-briefing-signals-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import briefing_signals  # noqa: E402
import db  # noqa: E402
import plaid_client  # noqa: E402
import shopify_client  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return 0 if condition else 1


def _shopify_order(order_id, created_at, total, *, refund=0,
                   fulfillment="fulfilled"):
    return {
        "order_id": order_id,
        "created_at": created_at,
        "total_price": total,
        "subtotal_price": total,
        "total_discounts": 0,
        "total_tax": 0,
        "financial_status": "paid",
        "fulfillment_status": fulfillment,
        "customer_id": "",
        "customer_email": "",
        "customer_name": "",
        "source": "online_store",
        "refund_total": refund,
        "line_items_json": "[]",
    }


def _plaid_transaction(transaction_id, *, pending, removed=0):
    return {
        "transaction_id": transaction_id,
        "account_id": "account-1",
        "date": date.today().isoformat(),
        "authorized_date": None,
        "amount_cents": -1250,
        "merchant_name": "Example Merchant",
        "name": "Example Merchant",
        "pending": int(pending),
        "category_primary": "GENERAL_MERCHANDISE",
        "category_detailed": "GENERAL_MERCHANDISE_OTHER",
        "removed": int(removed),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def run():
    failures = 0
    db.init_db()
    today = date.today()

    db.upsert_shopify_order(_shopify_order(
        "old-unfulfilled", (today - timedelta(days=4)).isoformat(), 40,
        fulfillment="unfulfilled"))
    db.upsert_shopify_order(_shopify_order(
        "old-fulfilled", (today - timedelta(days=8)).isoformat(), 90))
    db.upsert_shopify_order(_shopify_order(
        "old-refunded", (today - timedelta(days=8)).isoformat(), 30,
        refund=30, fulfillment="unfulfilled") | {"financial_status": "refunded"})
    db.upsert_shopify_order(_shopify_order(
        "today-1", f"{today.isoformat()} 09:00:00", 50))
    db.upsert_shopify_order(_shopify_order(
        "today-2", f"{today.isoformat()} 11:00:00", 25, refund=5))

    db.upsert_plaid_transaction(_plaid_transaction("pending", pending=True))
    db.upsert_plaid_transaction(_plaid_transaction("settled", pending=False))
    db.upsert_plaid_transaction(_plaid_transaction(
        "removed-pending", pending=True, removed=1))

    original_shopify_configured = shopify_client.is_configured
    original_shopify_store = shopify_client.get_store
    original_plaid_configured = plaid_client.is_configured
    shopify_client.is_configured = lambda: True
    shopify_client.get_store = lambda: "example.myshopify.com"
    plaid_client.is_configured = lambda: True
    try:
        shopify = shopify_client.get_briefing_signals()
        plaid = plaid_client.get_briefing_signals()
        home = briefing_signals.get_home_briefing_signals()
        toast = briefing_signals.get_toast_briefing_signals()
    finally:
        shopify_client.is_configured = original_shopify_configured
        shopify_client.get_store = original_shopify_store
        plaid_client.is_configured = original_plaid_configured

    failures += check(
        "Shopify flags only orders unfulfilled for more than three days",
        shopify[0] == {
            "severity": "urgent",
            "plain_language_sentence": (
                "1 Shopify order has been waiting for fulfillment for more "
                "than three days."),
            "deep_link": "https://example.myshopify.com/admin/orders",
        })
    failures += check(
        "Shopify summarizes today's cached net sales",
        shopify[1] == {
            "severity": "informational",
            "plain_language_sentence": (
                "Shopify recorded 2 orders totaling $70.00 today."),
            "deep_link": "https://example.myshopify.com/admin/orders",
        })
    failures += check(
        "Plaid reports only active cached pending transactions",
        plaid == [{
            "severity": "informational",
            "plain_language_sentence": (
                "1 bank transaction is still pending in Plaid."),
            "deep_link": "https://my.plaid.com/",
        }])
    failures += check(
        "Home collection includes Shopify and Plaid",
        home == [*shopify, *plaid])
    failures += check(
        "toast collection includes Shopify only",
        toast == shopify)
    failures += check(
        "every signal satisfies the shared contract",
        all(set(signal) == {
            "severity", "plain_language_sentence", "deep_link"
        } and signal["severity"] in briefing_signals.SEVERITY_ORDER
            and signal["deep_link"].startswith("https://")
            for signal in home))

    shopify_client.is_configured = lambda: False
    plaid_client.is_configured = lambda: False
    try:
        failures += check(
            "disconnected providers emit no signals",
            not briefing_signals.get_home_briefing_signals())
    finally:
        shopify_client.is_configured = original_shopify_configured
        plaid_client.is_configured = original_plaid_configured

    if failures:
        print(f"FAIL: {failures} briefing signal checks failed.")
        return 1
    print("PASS: Feature A backend briefing signal checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
