"""Offline checks for the Shopify adapter and its local SQLite contract."""

import hashlib
import hmac
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

_DATA = tempfile.TemporaryDirectory(prefix="klearing-shopify-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import shopify_client  # noqa: E402


def check(name, ok):
    print(("  PASS  " if ok else "  FAIL  ") + name)
    return not ok


def run():
    failures = 0
    db.init_db()
    failures += check("validates a myshopify domain", shopify_client.validate_config("token", "store.myshopify.com") is None)
    failures += check("rejects an unsafe store value", shopify_client.validate_config("token", "https://example.com") is not None)
    state = "test-state"
    auth_url = shopify_client._authorization_url("store.myshopify.com", "client-id", state)
    query = parse_qs(urlparse(auth_url).query)
    failures += check("builds an OAuth authorization URL", auth_url.startswith("https://store.myshopify.com/admin/oauth/authorize?"))
    failures += check("OAuth URL uses the localhost callback", query.get("redirect_uri") == [shopify_client.OAUTH_REDIRECT_URI])
    failures += check("OAuth URL requests every sync scope", set(query.get("scope", [""])[0].split(",")) == set(shopify_client.OAUTH_SCOPES))
    callback_pairs = [
        ("code", "one-time-code"),
        ("shop", "store.myshopify.com"),
        ("state", state),
        ("timestamp", str(int(time.time()))),
    ]
    callback_message = "&".join(f"{key}={value}" for key, value in sorted(callback_pairs))
    callback_pairs.append(("hmac", hmac.new(b"test-secret", callback_message.encode("utf-8"), hashlib.sha256).hexdigest()))
    raw_callback = "&".join(f"{key}={value}" for key, value in callback_pairs)
    failures += check("accepts a signed matching OAuth callback", shopify_client._validate_callback(raw_callback, "store.myshopify.com", state, "test-secret") is None)
    raw_callback_with_signature = raw_callback + "&signature=legacy-signature"
    failures += check("ignores a legacy signature parameter", shopify_client._validate_callback(raw_callback_with_signature, "store.myshopify.com", state, "test-secret") is None)
    failures += check("rejects an OAuth callback with the wrong state", shopify_client._validate_callback(raw_callback, "store.myshopify.com", "wrong-state", "test-secret") is not None)
    db.upsert_shopify_order({"order_id": "1", "created_at": "2026-07-01 10:00:00", "total_price": 50, "subtotal_price": 45, "total_discounts": 0, "total_tax": 5, "financial_status": "paid", "fulfillment_status": "fulfilled", "customer_id": "c1", "customer_email": "a@example.test", "customer_name": "A Test", "source": "online_store", "refund_total": 0, "line_items_json": "[]"})
    db.upsert_shopify_order({"order_id": "1", "created_at": "2026-07-01 10:00:00", "total_price": 55, "subtotal_price": 50, "total_discounts": 0, "total_tax": 5, "financial_status": "paid", "fulfillment_status": "fulfilled", "customer_id": "c1", "customer_email": "a@example.test", "customer_name": "A Test", "source": "online_store", "refund_total": 0, "line_items_json": "[]"})
    rows = db.list_shopify_orders("2026-07-01", "2026-07-02")
    failures += check("upserts Shopify orders by id", len(rows) == 1 and rows[0]["total_price"] == 55)
    original_list_all = shopify_client._list_all
    inventory_calls = []

    def fake_list_all(token, store, path, key, params=None):
        inventory_calls.append((path, key, params))
        return [{"id": "inventory-cost-1", "cost": "12.34"}]

    shopify_client._list_all = fake_list_all
    try:
        cost_summary = shopify_client._sync_variant_costs(
            "token", "store.myshopify.com", [{"variants": [{
                "id": "variant-cost-1", "inventory_item_id": "inventory-cost-1"
            }]}])
    finally:
        shopify_client._list_all = original_list_all
    cost_snapshot = db.get_shopify_variant_cost_at("variant-cost-1", "9999-12-31 23:59:59")
    failures += check("syncs and persists Shopify Inventory Item unit costs",
                      cost_summary == {"available": 1, "missing": 0, "unavailable": 0}
                      and inventory_calls[0][0] == "/inventory_items.json"
                      and inventory_calls[0][2] == {"ids": "inventory-cost-1"}
                      and cost_snapshot["unit_cost_cents"] == 1234
                      and cost_snapshot["cost_status"] == "available")
    original_list_all = shopify_client._list_all

    def fake_payout_list(token, store, path, key, params=None):
        return [{
            "id": "payout-test-1", "status": "paid", "date": "2026-07-24",
            "currency": "USD", "amount": "97.00",
            "summary": {"charges_gross_amount": "100.00",
                         "charges_fee_amount": "3.00"},
        }]

    shopify_client._list_all = fake_payout_list
    try:
        payout_summary = shopify_client._sync_payouts(
            "token", "store.myshopify.com")
    finally:
        shopify_client._list_all = original_list_all
    payout = next(row for row in db.list_shopify_payouts()
                  if row["payout_id"] == "payout-test-1")
    failures += check("syncs and persists Shopify payout summaries",
                      payout_summary == {"synced": 1, "paid": 1, "unpaid": 0}
                      and payout["amount_cents"] == 9700
                      and payout["fee_amount_cents"] == 300
                      and payout["gross_amount_cents"] == 10000)
    if failures:
        print(f"FAIL: {failures} Shopify checks failed."); return 1
    print("PASS: Shopify local adapter checks passed."); return 0


if __name__ == "__main__":
    raise SystemExit(run())
