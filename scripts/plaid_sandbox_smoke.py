"""
Live Plaid **Sandbox** smoke test — the real end-to-end flow against
sandbox.plaid.com with fake test banks (zero risk, read-only).

It never touches a real bank and needs no vault: it drives the same
PlaidAdapter methods Klearing uses, straight against the Sandbox API.

Run it with your own free Plaid Sandbox keys:

    # Windows PowerShell
    $env:PLAID_CLIENT_ID = "your_client_id"
    $env:PLAID_SECRET    = "your_sandbox_secret"
    venv/Scripts/python scripts/plaid_sandbox_smoke.py

    # macOS / Linux
    PLAID_CLIENT_ID=... PLAID_SECRET=... venv/bin/python scripts/plaid_sandbox_smoke.py

With no keys set it prints SKIP and exits 0, so it is safe to run in CI
or a fresh checkout.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from banks.plaid import PlaidAdapter  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    client_id = os.environ.get("PLAID_CLIENT_ID")
    secret = os.environ.get("PLAID_SECRET")
    if not client_id or not secret:
        print("SKIP: set PLAID_CLIENT_ID and PLAID_SECRET (Sandbox) to run "
              "the live Sandbox smoke test. Nothing was called.")
        return 0

    adapter = PlaidAdapter()
    creds = {"client_id": client_id, "secret": secret,
             "environment": "sandbox", "access_tokens": []}

    print("1/5 test_connection (link/token/create)…")
    result = adapter.test_connection(creds)
    if not result.ok:
        print("   FAIL:", result.message)
        return 1
    print("   ok:", result.message)

    print("2/5 sandbox public_token/create…")
    public_token = adapter.create_sandbox_public_token(creds)
    print("   got public_token:", public_token[:24], "…")

    print("3/5 item/public_token/exchange…")
    access_token = adapter.exchange_public_token(creds, public_token)
    creds["access_tokens"] = [access_token]
    print("   got access_token:", access_token[:24], "…")

    print("4/5 accounts/get…")
    accounts = adapter.list_accounts(creds)
    for account in accounts:
        print(f"   - {account['name']}: ${account['balance']:,.2f}")

    print("5/5 transactions/sync…")
    txns = adapter.fetch_transactions(creds)
    print(f"   pulled {len(txns)} settled transaction(s). First few "
          f"(Klearing shape, negative = money out):")
    for txn in txns[:5]:
        print(f"   - {txn['date']}  {txn['amount']:+.2f}  "
              f"{txn['counterparty']}  [{txn['external_id'][:10]}…]")

    print("\nPASS: full Sandbox flow works end to end "
          "(same path Mercury uses).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
