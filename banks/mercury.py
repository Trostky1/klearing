"""
Mercury bank adapter — the first BankAdapter implementation.

API calls are the same ones the original check_mercury.py made,
normalized to the adapter interface. A read-only Mercury token is
enough; Klearing never moves money.
"""

import requests

import banks
import connectivity

MERCURY_API_BASE = "https://api.mercury.com/api/v1"

# Mercury tokens look like:  secret-token:mercury_<environment>_<random>
# The dashboard shows them WITH the "secret-token:" prefix, but that
# part is easy to lose when copying — if the rest is intact we can
# repair it instead of failing.
TOKEN_PREFIX = "secret-token:mercury_"


def normalize_token(value):
    if value.startswith("mercury_"):
        return "secret-token:" + value
    return value


def _get(token, path, params=None):
    response = requests.get(
        f"{MERCURY_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "accept": "application/json",
        },
        params=params,
        timeout=15,
    )
    if response.status_code != 200:
        raise MercuryHTTPError(response.status_code, response.text, path)
    return response.json()


class MercuryHTTPError(RuntimeError):
    def __init__(self, status_code, body, path):
        self.status_code = status_code
        super().__init__(
            f"Mercury returned HTTP {status_code} for {path}: {body}"
        )


class MercuryAdapter(banks.BankAdapter):
    id = "mercury"
    display_name = "Mercury"
    token_url = "https://app.mercury.com/settings/tokens"
    token_help = (
        "In Mercury: click your organization name (top left) → All "
        "Settings → Tokens → Create an API Token, with Read Only access. "
        "Read Only is all Klearing needs. If the direct link lands on your "
        "dashboard instead, use that menu path."
    )

    def credential_fields(self):
        return [
            banks.CredentialField(
                key="api_token",
                label="Mercury API token",
                help_text=(
                    "Paste it, don't retype it — the token only shows "
                    "once, and a single missing character breaks it."
                ),
                placeholder="secret-token:mercury_…",
                prefix=TOKEN_PREFIX,
                min_length=60,
                max_length=140,
                pattern=r"secret-token:mercury_[A-Za-z0-9_]{30,}",
                normalizer=normalize_token,
                format_error=(
                    "A Mercury token only contains letters, numbers, "
                    "colons, underscores, and dashes. Something else came "
                    "along in the paste — copy it again."
                ),
            ),
        ]

    def test_connection(self, credentials):
        token = credentials["api_token"]
        try:
            accounts = _get(token, "/accounts").get("accounts", [])
        except MercuryHTTPError as exc:
            if exc.status_code in (401, 403):
                return connectivity.fail(
                    "Mercury rejected the token (HTTP "
                    f"{exc.status_code}). It may have been revoked, or a "
                    "character changed in the copy — create a fresh "
                    "Read Only token and paste it again."
                )
            return connectivity.fail(str(exc))
        except Exception as exc:
            return connectivity.from_exception("Mercury", exc)

        names = ", ".join(a.get("name", "?") for a in accounts) or "none"
        return connectivity.ok(
            f"Connected. Mercury returned {len(accounts)} account(s): {names}."
        )

    def list_accounts(self, credentials):
        accounts = _get(credentials["api_token"], "/accounts").get("accounts", [])
        result = []
        for account in accounts:
            balance = account.get("currentBalance")
            if balance is None:
                balance = account.get("availableBalance") or 0.0
            result.append({
                "id": account["id"],
                "name": account.get("name", "Unnamed account"),
                "balance": balance,
            })
        return result

    def fetch_transactions(self, credentials, since_date=None):
        token = credentials["api_token"]
        accounts = _get(token, "/accounts").get("accounts", [])

        page_size = 500
        transactions = []
        for account in accounts:
            offset = 0
            while True:
                params = {"limit": page_size, "offset": offset}
                if since_date:
                    # Overlap is fine — the caller dedups by external_id.
                    params["start"] = since_date
                payload = _get(token, f"/account/{account['id']}/transactions",
                               params)
                batch = payload.get("transactions", [])
                for txn in batch:
                    # Only settled transactions. Pending authorizations can
                    # change amount before settling (and dedup would then
                    # freeze the wrong figure), and failed/cancelled ones
                    # never moved money at all — importing either would put
                    # wrong numbers one Approve away from the books.
                    status = txn.get("status")
                    if status is not None and status != "sent":
                        continue
                    amount = txn.get("amount")
                    if amount is None or amount == 0:
                        continue  # nothing actually moved
                    transactions.append({
                        "external_id": txn["id"],
                        "account_id": account["id"],
                        "date": (txn.get("postedAt") or txn.get("createdAt") or "")[:10],
                        "amount": amount,
                        "counterparty": (
                            txn.get("counterpartyName")
                            or txn.get("bankDescription")
                            or "Unknown vendor"
                        ),
                    })
                if len(batch) < page_size:
                    break
                offset += page_size
        return transactions
