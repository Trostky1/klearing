"""Offline regression coverage for the Plaid Phase 1 API surface.

This test uses fake Plaid responses and an isolated Klearing data directory.
It never contacts Plaid and never needs real credentials.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(label, condition):
    if condition:
        print(f"PASS: {label}")
        return 0
    print(f"FAIL: {label}")
    return 1


class FakeRequest:
    async def json(self):
        return {"public_token": "public-sandbox-token"}


def main():
    failures = 0
    with tempfile.TemporaryDirectory(prefix="klearing-plaid-phase1-",
                                      ignore_cleanup_errors=True) as data_dir:
        os.environ["KLEARING_DATA_DIR"] = data_dir
        os.environ["PLAID_CLIENT_ID"] = "sandbox-client-id"
        os.environ["PLAID_SECRET"] = "sandbox-secret"
        os.environ["PLAID_ENV"] = "sandbox"

        import db
        import plaid_client
        import secrets_store
        import app

        db.init_db()
        calls = []
        responses = [
            {"link_token": "link-sandbox-token"},
            {"access_token": "access-sandbox-token", "item_id": "item-123"},
            {"added": [{"transaction_id": "txn-1", "account_id": "acct-1",
                        "date": "2026-07-24", "amount": 12.5,
                        "merchant_name": "Example Merchant", "pending": False,
                        "personal_finance_category": {"primary": "BANK_FEES",
                                                      "detailed": "BANK_FEES"}}],
             "modified": [], "removed": [], "has_more": True,
             "next_cursor": "cursor-1"},
            {"accounts": [{"account_id": "acct-1", "name": "Checking",
                            "official_name": "Example Checking",
                            "type": "depository", "subtype": "checking",
                            "mask": "1234"}]},
            {"added": [], "modified": [], "removed": [], "has_more": False,
             "next_cursor": "cursor-2"},
            {"accounts": [{"account_id": "acct-1", "name": "Checking",
                            "official_name": "Example Checking",
                            "type": "depository", "subtype": "checking",
                            "mask": "1234"}]},
        ]
        original_request = plaid_client._request

        def fake_request(path, body):
            calls.append((path, dict(body)))
            return responses.pop(0)

        plaid_client._request = fake_request
        try:
            token = plaid_client.create_link_token()
            failures += check("link token returned", token == "link-sandbox-token")
            link_path, link_body = calls[0]
            failures += check("link token uses Sandbox", link_path == "/link/token/create")
            failures += check("link token omits OAuth fields",
                              "redirect_uri" not in link_body and "webhook" not in link_body)

            exchange_response = app.plaid_exchange
            result = asyncio.run(exchange_response(FakeRequest()))
            payload = json.loads(result.body)
            failures += check("exchange returns connection id",
                              payload == {"ok": True, "connection_id": "item-123"})
            failures += check("access token uses required secure-store name",
                              secrets_store.get_secret(plaid_client.TOKEN_SECRET_NAME)
                              == "access-sandbox-token")
            failures += check("item id uses required secure-store name",
                              secrets_store.get_secret(plaid_client.ITEM_ID_SECRET_NAME)
                              == "item-123")
            failures += check("complete Plaid Item reports configured",
                              plaid_client.is_configured())

            first = plaid_client.sync("item-123")
            second = plaid_client.sync("item-123")
            failures += check("first sync returns transaction data",
                              first["added"][0]["transaction_id"] == "txn-1")
            cached = db.list_plaid_transactions_for_ledger()
            failures += check("sync caches normalized Plaid data for ledger import",
                              len(cached) == 1
                              and cached[0]["transaction_id"] == "txn-1"
                              and cached[0]["amount_cents"] == -1250
                              and cached[0]["category_detailed"] == "BANK_FEES")
            with db.get_connection() as conn:
                account_subtype = conn.execute(
                    "SELECT subtype FROM bank_accounts "
                    "WHERE source = 'plaid' AND account_id = ?",
                    ("acct-1",),
                ).fetchone()["subtype"]
            failures += check("sync caches Plaid account metadata",
                              account_subtype == "checking")
            sync_calls = [(path, body) for path, body in calls
                          if path == "/transactions/sync"]
            failures += check("second sync sends the persisted cursor",
                              sync_calls[-1][1].get("cursor") == "cursor-1")
            failures += check("latest cursor is persisted",
                              secrets_store.get_secret(plaid_client.SYNC_CURSOR_SECRET_NAME)
                              == "cursor-2")

            route_paths = {route.path for route in app.app.routes
                           if hasattr(route, "path")}
            failures += check("Phase 1 routes are registered",
                              {"/plaid/link-token", "/plaid/exchange",
                               "/plaid/sync/{connection_id}"} <= route_paths)
            source = (ROOT / "app.py").read_text(encoding="utf-8")
            failures += check("Connections page has Connect Bank UI",
                              "id=\"plaid-connect-btn\"" in source
                              and "/plaid/link-token" in source
                              and "/plaid/exchange" in source)
            failures += check("Connections page has Plaid Sync now UI",
                              "id=\"plaid-sync-btn\"" in source
                              and "/plaid/sync/" in source
                              and "Synced ' + count" in source)
        finally:
            plaid_client._request = original_request

    return failures


if __name__ == "__main__":
    sys.exit(main())
