"""Offline regression coverage for the shared Insights metric registry."""

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-insight-metrics-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import insight_metrics  # noqa: E402
import inventory  # noqa: E402
import plaid_client  # noqa: E402
import shopify_client  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def shopify_order(order_id, order_date, total, status="paid", refund=0):
    db.upsert_shopify_order({
        "order_id": order_id, "created_at": order_date.isoformat(),
        "total_price": total, "subtotal_price": total,
        "total_discounts": 0, "total_tax": 0,
        "financial_status": status, "fulfillment_status": "fulfilled",
        "customer_id": "", "customer_email": "", "customer_name": "",
        "source": "online_store", "refund_total": refund,
        "line_items_json": "[]",
    })


def plaid_transaction(transaction_id, transaction_date, amount, category,
                      pending=0, removed=0):
    db.upsert_plaid_transaction({
        "transaction_id": transaction_id, "account_id": "bank-1",
        "date": transaction_date.isoformat(), "authorized_date": None,
        "amount_cents": amount, "merchant_name": "Fixture",
        "name": "Fixture", "pending": pending,
        "category_primary": category, "category_detailed": "",
        "removed": removed, "synced_at": date.today().isoformat(),
    })


def main():
    failures = 0
    db.init_db()
    today = date.today()
    windows = insight_metrics.rolling_periods(today)
    prior_start, prior_end = windows[-2]
    current_start, current_end = windows[-1]

    # Provider configuration is local and deterministic in this harness.
    shopify_client.is_configured = lambda: True
    shopify_client.get_store = lambda: "fixture.myshopify.com"
    plaid_client.is_configured = lambda: True

    shopify_order("prior", prior_start + timedelta(days=2), 100)
    shopify_order("current", current_start + timedelta(days=2), 225, refund=25)
    shopify_order("voided", current_start + timedelta(days=3), 999, status="voided")

    plaid_transaction("prior-software", prior_start + timedelta(days=2),
                      -3000, "SOFTWARE")
    plaid_transaction("current-software", current_start + timedelta(days=2),
                      -6000, "SOFTWARE")
    plaid_transaction("current-food", current_start + timedelta(days=3),
                      -2000, "FOOD_AND_DRINK")
    plaid_transaction("current-deposit", current_start + timedelta(days=4),
                      50000, "INCOME")
    plaid_transaction("pending-large", current_start + timedelta(days=5),
                      -999999, "TRAVEL", pending=1)

    db.set_setting("active_bank", "plaid")
    db.insert_reconcile_snapshot(
        (today - timedelta(days=1)).isoformat(), 1200, 1200, 0, False)
    db.insert_reconcile_snapshot(today.isoformat(), 1300, 1300, 0, False)

    inventory.record_acquisition(
        "variant-1", 10, "4.00", prior_start.isoformat(),
        funding_account_code="1010")
    inventory.record_sale(
        "variant-1", 2, current_start.isoformat(),
        "shopify:order:inventory-fixture:cogs")

    metrics = insight_metrics.get_insight_metrics()
    by_id = {metric["id"]: metric for metric in metrics}
    failures += check(
        "registry collects Shopify, Plaid, and Inventory providers",
        {metric["source"] for metric in metrics}
        == {"shopify", "plaid", "inventory"})
    failures += check(
        "Shopify revenue is net, excludes voids, and carries a same-unit delta",
        by_id["shopify_revenue"]["value"] == 20000
        and by_id["shopify_revenue"]["delta"] == 10000)
    failures += check(
        "Plaid cash position uses stored exact balance snapshots",
        by_id["plaid_cash_position"]["value"] == 130000
        and by_id["plaid_cash_position"]["delta"] == 10000)
    failures += check(
        "Plaid top expense ignores inflows and pending rows",
        by_id["plaid_top_expense_category"]["value"] == 6000
        and by_id["plaid_top_expense_category"]["delta"] == 3000
        and by_id["plaid_top_expense_category"]["label"]
        == "Top expense: Software")
    failures += check(
        "Inventory value follows dated FIFO layers",
        by_id["inventory_value"]["value"] == 3200
        and by_id["inventory_value"]["delta"] == -800)
    failures += check(
        "every metric has HTTPS source navigation and structured history",
        all(metric["deep_link"].startswith("https://")
            and isinstance(metric["time_series"], list)
            and all({"period_start", "period_end", "value"} == set(point)
                    for point in metric["time_series"])
            for metric in metrics))
    failures += check(
        "base metrics deep-link to their real source platform destinations",
        by_id["shopify_revenue"]["deep_link"]
        == "https://fixture.myshopify.com/admin/orders"
        and by_id["plaid_cash_position"]["deep_link"]
        == "https://my.plaid.com/"
        and by_id["plaid_top_expense_category"]["deep_link"]
        == "https://my.plaid.com/"
        and by_id["inventory_value"]["deep_link"]
        == "https://fixture.myshopify.com/admin/products")

    try:
        insight_metrics.make_insight_metric(
            "Bad id", "Bad", 1, None, "shopify", "http://example.test",
            "count", [])
        rejected = False
    except ValueError:
        rejected = True
    failures += check("contract rejects invalid ids and non-HTTPS links", rejected)

    if failures:
        print(f"FAIL: {failures} Insights metric checks failed.")
        return 1
    print("PASS: Insights metric checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
