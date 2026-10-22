"""
Plaid bank adapter — the second BankAdapter implementation.

Plaid is not one bank: a single Plaid integration reaches Wells Fargo
plus ~12,000 other institutions through Plaid's hosted Link flow. Each
Klearing user brings their own free Plaid account (Trial plan: 10 free
Production Items, Wells Fargo included) — there is no shared key and no
cost to the project.

How it differs from Mercury (a static pasted token):

  1. The user pastes their Plaid **Client ID** and **Secret** (per
     environment) — stored in the encrypted vault like every credential.
  2. They connect a bank through Plaid **Link** (Plaid's own secure
     widget, opened in Klearing's native window). Link returns a
     short-lived public_token.
  3. Klearing exchanges that public_token for a long-lived **access
     token**, one per connected bank ("Item"), also stored in the vault.
  4. Transactions are pulled with **/transactions/sync**.

Everything downstream (dedup, categorization, vendor memory, approval)
is untouched: fetch_transactions still yields the same
{external_id, date, amount, counterparty} shape, with Klearing's sign
convention (negative = money out).

Sign convention: Plaid uses POSITIVE for money leaving the account;
Klearing uses NEGATIVE for money out. We negate on the way in.

Read-only: this adapter never initiates a transfer. It only reads
accounts and transactions.
"""

import json

import requests

import banks
import connectivity

# Plaid retired the old "development" environment; sandbox (free test
# banks) and production (real banks, what the Trial plan uses) remain.
PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# A well-known Sandbox institution ("First Platypus Bank") for the
# no-login test-bank button and the Sandbox smoke test.
SANDBOX_INSTITUTION_ID = "ins_109508"

LINK_CLIENT_NAME = "Klearing"


def _client_id_field():
    return banks.CredentialField(
        key="client_id",
        label="Plaid Client ID",
        help_text=("From your Plaid dashboard: Developers → Keys. The same "
                   "Client ID works in every environment."),
        placeholder="e.g. 6f1c… (24 characters)",
        min_length=20,
        max_length=64,
        pattern=r"[A-Za-z0-9]+",
        format_error=("A Plaid Client ID is a single run of letters and "
                      "numbers. Something else came along in the paste — "
                      "copy just the Client ID."),
    )


def _secret_field():
    return banks.CredentialField(
        key="secret",
        label="Plaid Secret",
        help_text=("Developers → Keys. Use the Secret for the SAME "
                   "environment you pick below — Sandbox and Production "
                   "have different Secrets."),
        placeholder="e.g. a1b2… (30 characters)",
        min_length=20,
        max_length=64,
        pattern=r"[A-Za-z0-9]+",
        format_error=("A Plaid Secret is a single run of letters and "
                      "numbers. Copy just the Secret for your chosen "
                      "environment."),
    )


def _environment_field():
    return banks.CredentialField(
        key="environment",
        label="Plaid environment",
        help_text=("\"sandbox\" to try it with fake test banks (zero risk), "
                   "or \"production\" for your real bank."),
        placeholder="sandbox",
        pattern=r"sandbox|production",
        secret=False,
        format_error=('Environment must be exactly "sandbox" or '
                      '"production".'),
        normalizer=lambda value: (value or "").strip().lower(),
    )


class PlaidHTTPError(RuntimeError):
    """A non-200 Plaid response, carrying Plaid's structured error so the
    UI can show something a human can act on."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.error_code = (payload or {}).get("error_code")
        self.error_type = (payload or {}).get("error_type")
        message = (
            (payload or {}).get("error_message")
            or (payload or {}).get("display_message")
            or f"Plaid returned HTTP {status_code}"
        )
        self.plaid_message = message
        super().__init__(f"Plaid {self.error_code or status_code}: {message}")


class PlaidAdapter(banks.BankAdapter):
    id = "plaid"
    display_name = "Plaid (Wells Fargo + ~12,000 banks)"
    token_url = "https://dashboard.plaid.com/developers/keys"
    token_help = (
        "Plaid connects Wells Fargo and thousands of other banks. Create a "
        "free Plaid account, paste your Client ID and Secret here, then "
        "connect your bank through Plaid's secure Link on the next step."
    )

    def credential_fields(self):
        return [_client_id_field(), _secret_field(), _environment_field()]

    # ---- credential storage: access tokens live in the vault too

    def stored_access_tokens(self):
        """The per-bank Plaid access tokens, from the encrypted vault."""
        import secrets_store
        raw = secrets_store.get_secret(self.secret_name("access_tokens"))
        if not raw:
            return []
        try:
            tokens = json.loads(raw)
        except ValueError:
            return []
        return [t for t in tokens if isinstance(t, str) and t]

    def store_access_token(self, access_token):
        """Append a newly connected bank's access token to the vault
        (idempotent)."""
        import secrets_store
        tokens = self.stored_access_tokens()
        if access_token and access_token not in tokens:
            tokens.append(access_token)
            secrets_store.set_secret(self.secret_name("access_tokens"),
                                     json.dumps(tokens))

    def stored_credentials(self):
        """The pasted keys plus every connected bank's access token. None
        only when the keys themselves are missing (access_tokens may be
        empty — that just means no bank is connected yet)."""
        base = super().stored_credentials()
        if base is None:
            return None
        base["access_tokens"] = self.stored_access_tokens()
        return base

    def is_configured(self):
        """Configured means keys AND at least one connected bank — pasting
        the keys alone is not enough to pull transactions."""
        creds = self.stored_credentials()
        return bool(creds and creds.get("access_tokens"))

    # ---- Plaid HTTP

    def _host(self, credentials):
        env = (credentials.get("environment") or "sandbox").strip().lower()
        return PLAID_HOSTS.get(env, PLAID_HOSTS["sandbox"])

    def _request(self, credentials, path, extra=None):
        """POST to Plaid with client_id/secret injected. Returns parsed
        JSON or raises PlaidHTTPError."""
        body = {
            "client_id": credentials["client_id"],
            "secret": credentials["secret"],
        }
        if extra:
            body.update(extra)
        response = requests.post(
            f"{self._host(credentials)}{path}",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        connectivity.raise_for_rate_limit("Plaid", response.status_code)
        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise PlaidHTTPError(response.status_code, payload)
        return response.json()

    # ---- Link flow (used by the Setup wizard's Plaid connect step)

    def create_link_token(self, credentials, client_user_id="klearing-user",
                          redirect_uri=None):
        """A short-lived link_token that Plaid Link needs to open.

        redirect_uri is required for OAuth-based institutions (credit unions,
        large banks). It must be pre-registered in the Plaid Dashboard and must
        exactly match what is passed here. For the local pywebview app the
        value is http://localhost:<PORT>/plaid/oauth-return.
        """
        body = {
            "client_name": LINK_CLIENT_NAME,
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": client_user_id},
            "products": ["transactions"],
        }
        if redirect_uri:
            body["redirect_uri"] = redirect_uri
        data = self._request(credentials, "/link/token/create", body)
        return data["link_token"]

    def exchange_public_token(self, credentials, public_token):
        """Exchange a Link public_token for a long-lived access token."""
        data = self._request(credentials, "/item/public_token/exchange",
                             {"public_token": public_token})
        return data["access_token"]

    def create_sandbox_public_token(self, credentials,
                                    institution_id=SANDBOX_INSTITUTION_ID):
        """Sandbox only: mint a public_token without the Link UI, so the
        connect flow can be tested (and demoed) with zero login."""
        data = self._request(credentials, "/sandbox/public_token/create", {
            "institution_id": institution_id,
            "initial_products": ["transactions"],
        })
        return data["public_token"]

    def connect_via_public_token(self, credentials, public_token):
        """Exchange a public_token and persist the access token. Returns
        the access token."""
        access_token = self.exchange_public_token(credentials, public_token)
        self.store_access_token(access_token)
        return access_token

    # ---- BankAdapter contract

    def test_connection(self, credentials):
        """Validate the Client ID + Secret + environment by asking Plaid
        for a link_token (the cheapest call that proves the keys work in
        the chosen environment)."""
        try:
            self.create_link_token(credentials)
        except connectivity.RateLimitError as exc:
            return connectivity.fail(str(exc))
        except PlaidHTTPError as exc:
            if exc.error_code in ("INVALID_API_KEYS", "INVALID_SECRET",
                                  "INVALID_CLIENT_ID", "UNAUTHORIZED_ENVIRONMENT"):
                return connectivity.fail(
                    "Plaid rejected these keys for the chosen environment. "
                    "Check that the Secret matches the environment "
                    "(Sandbox vs Production have different Secrets), and "
                    "that both were pasted whole."
                )
            return connectivity.fail(exc.plaid_message)
        except Exception as exc:
            return connectivity.from_exception("Plaid", exc)

        connected = len(credentials.get("access_tokens") or [])
        if connected:
            return connectivity.ok(
                f"Keys valid. {connected} bank(s) connected."
            )
        return connectivity.ok(
            "Keys valid. Connect your bank on the next step."
        )

    def list_accounts(self, credentials):
        """Every account across every connected bank, as
        [{'id', 'name', 'balance'}]."""
        accounts = []
        for access_token in credentials.get("access_tokens") or []:
            data = self._request(credentials, "/accounts/get",
                                 {"access_token": access_token})
            for account in data.get("accounts", []):
                balances = account.get("balances") or {}
                balance = balances.get("current")
                if balance is None:
                    balance = balances.get("available")
                accounts.append({
                    "id": account["account_id"],
                    "name": account.get("name") or account.get("official_name")
                    or "Account",
                    "balance": float(balance) if balance is not None else 0.0,
                })
        return accounts

    def fetch_transactions(self, credentials, since_date=None):
        """Pull settled transactions from every connected bank via
        /transactions/sync, normalized to Klearing's shape.

        since_date is ignored: sync is cursor-based, and Klearing already
        dedups by external_id, so a full sync each run is correct (just
        re-offering rows that are filtered out downstream). Pending
        transactions are skipped — like the Mercury adapter, only settled
        activity is imported, because a pending amount can still change.
        """
        results = []
        seen = set()
        for access_token in credentials.get("access_tokens") or []:
            cursor = None
            # Bound the loop defensively; a healthy sync converges quickly.
            for _ in range(1000):
                extra = {"access_token": access_token, "count": 500}
                if cursor is not None:
                    extra["cursor"] = cursor
                data = self._request(credentials, "/transactions/sync", extra)
                # added = brand-new, modified = updated (e.g. pending that
                # settled); both represent current transactions.
                for txn in data.get("added", []) + data.get("modified", []):
                    if txn.get("pending"):
                        continue
                    txn_id = txn["transaction_id"]
                    if txn_id in seen:
                        continue
                    seen.add(txn_id)
                    results.append({
                        "external_id": txn_id,
                        "account_id": txn.get("account_id"),
                        "date": (txn.get("date")
                                 or txn.get("authorized_date") or "")[:10],
                        # Plaid: positive = money out. Klearing: negative =
                        # money out. Negate.
                        "amount": -float(txn["amount"]),
                        "counterparty": (txn.get("merchant_name")
                                         or txn.get("name")
                                         or "Unknown vendor"),
                    })
                cursor = data.get("next_cursor")
                if not data.get("has_more"):
                    break
        return results


ADAPTER = PlaidAdapter()
