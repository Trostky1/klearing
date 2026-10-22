"""SimpleFIN Bridge backend adapter.

This adapter is registered (see banks/__init__.py), but its credential field
is deliberately never rendered by the generic Setup wizard: the field's only
value, the Access URL, is not something a user can obtain by hand, so a
generic paste box would show them a field they have no way to fill in. It
only exists after a server-side exchange of a one-use setup token (see
``exchange_setup_token`` below). Users reach that exchange, plus account
mapping and cutover, through the dedicated ``/setup/simplefin`` flow, or by
reconnecting from the Connections page.
"""

import base64
import binascii
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

import banks
import connectivity


CREATE_URL = "https://bridge.simplefin.org/simplefin/create"
BRIDGE_URL = "https://bridge.simplefin.org/"
BRIDGE_HELP = (
    "Sign up at SimpleFIN Bridge and connect your bank there first. "
    "You'll come back here afterward to create a setup token."
)
MAX_HISTORY_DAYS = 90


class SimpleFINError(RuntimeError):
    """A safe, user-actionable SimpleFIN protocol or payload failure."""


def _https_url(value, label, require_credentials=False):
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SimpleFINError(f"{label} is not a valid URL.") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise SimpleFINError(f"{label} must be a complete HTTPS URL.")
    if parsed.fragment or parsed.query:
        raise SimpleFINError(f"{label} must not contain a query or fragment.")
    if require_credentials and (parsed.username is None or parsed.password is None):
        raise SimpleFINError(f"{label} is missing its access credentials.")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host += f":{port}"
    clean = urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    auth = ((unquote(parsed.username), unquote(parsed.password))
            if parsed.username is not None and parsed.password is not None
            else None)
    return clean, auth


def exchange_setup_token(setup_token):
    """Exchange one base64 setup token for its permanent Access URL."""
    token = str(setup_token or "").strip()
    if not token:
        raise SimpleFINError("The SimpleFIN setup token is empty.")
    try:
        encoded = token.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        claim_url = base64.b64decode(encoded, altchars=b"-_", validate=True).decode(
            "utf-8")
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise SimpleFINError(
            "The SimpleFIN setup token is not valid base64. Copy the whole token again."
        ) from exc
    claim_url, _auth = _https_url(claim_url, "The SimpleFIN claim URL")
    response = requests.post(claim_url, timeout=30)
    connectivity.raise_for_rate_limit("SimpleFIN", response.status_code)
    if response.status_code == 403:
        raise SimpleFINError(
            "SimpleFIN rejected this one-use token. It may already have been "
            "claimed; disable it in SimpleFIN Bridge and create a fresh token."
        )
    if response.status_code != 200:
        raise SimpleFINError(f"SimpleFIN token exchange returned HTTP "
                             f"{response.status_code}.")
    access_url = response.text.strip()
    _https_url(access_url, "The SimpleFIN Access URL", require_credentials=True)
    return access_url


def _start_date(value):
    today = date.today()
    earliest = today - timedelta(days=MAX_HISTORY_DAYS - 1)
    if value:
        try:
            requested = date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise SimpleFINError("SimpleFIN start date must be YYYY-MM-DD.") from exc
        requested = min(requested, today)
        return max(requested, earliest)
    return earliest


def _epoch_start(value):
    return int(datetime.combine(_start_date(value), time.min,
                                tzinfo=timezone.utc).timestamp())


def _safe_warning(value):
    return " ".join(str(value or "").split())[:300]


def _payload_warnings(payload):
    warnings = [_safe_warning(value) for value in payload.get("errors", [])
                if _safe_warning(value)]
    for error in payload.get("errlist", []):
        if isinstance(error, dict):
            message = _safe_warning(error.get("msg") or error.get("code"))
            if message:
                warnings.append(message)
    return warnings


def _decimal(value, label):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SimpleFINError(f"SimpleFIN returned an invalid {label}.") from exc
    if not amount.is_finite():
        raise SimpleFINError(f"SimpleFIN returned an invalid {label}.")
    return amount


class SimpleFINAdapter(banks.BankAdapter):
    id = "simplefin"
    display_name = "SimpleFIN Bridge"
    token_url = CREATE_URL
    token_help = (
        "Create a one-use setup token in SimpleFIN Bridge. Klearing exchanges "
        "it once and stores only the permanent encrypted Access URL."
    )
    # Sub-step A of the two-step connection flow: signing up at SimpleFIN
    # Bridge and connecting a bank there. This is a separate, earlier action
    # from token_url/token_help above (Sub-step B, creating the one-use
    # setup token) — the two links must never appear on the same screen
    # before Sub-step A is done, or a user goes looking for a token that
    # doesn't exist yet. See /setup/simplefin in setup_wizard.py.
    #
    # Unlike token_url/token_help, this is not declared on the BankAdapter
    # base class — nothing generic renders it, only /setup/simplefin's
    # Screen 1, which already hardcodes banks.get_adapter("simplefin").
    # Callers should read simplefin.BRIDGE_URL / simplefin.BRIDGE_HELP
    # directly rather than going through an adapter instance for it.
    bridge_url = BRIDGE_URL
    bridge_help = BRIDGE_HELP

    def credential_fields(self):
        # The dedicated /setup/simplefin flow stores the exchanged Access
        # URL, never the one-use setup token. The generic Setup wizard's
        # step-3 loop (setup_wizard.py::render_step_bank) special-cases this
        # adapter's id and skips rendering this field as a paste box — a
        # user has no way to type in a value they can only get from a
        # server-side token exchange.
        return [banks.CredentialField(
            key="access_url", label="SimpleFIN Access URL",
            help_text="Created by exchanging a one-use SimpleFIN setup token.",
            min_length=20, max_length=2048,
        )]

    def _accounts(self, credentials, since_date=None, balances_only=False):
        access_url, auth = _https_url(
            credentials.get("access_url"), "The SimpleFIN Access URL",
            require_credentials=True)
        params = {
            # Always present: Bridge can otherwise return healthy balances and
            # an empty transaction list, a silent-success failure mode.
            "start-date": _epoch_start(since_date),
            "version": 1,
        }
        if balances_only:
            params["balances-only"] = 1
        response = requests.get(
            f"{access_url}/accounts", auth=auth, params=params, timeout=30)
        connectivity.raise_for_rate_limit("SimpleFIN", response.status_code)
        if response.status_code == 403:
            raise SimpleFINError(
                "SimpleFIN access was rejected. Reconnect it with a fresh setup token."
            )
        if response.status_code != 200:
            raise SimpleFINError(f"SimpleFIN returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SimpleFINError("SimpleFIN returned a response that was not JSON.") from exc
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            raise SimpleFINError("SimpleFIN returned no account list.")
        warnings = _payload_warnings(payload)
        if warnings:
            print("SimpleFIN warning: " + " | ".join(warnings))
        if not accounts and warnings:
            raise SimpleFINError("SimpleFIN returned no usable accounts: "
                                 + " | ".join(warnings))
        return accounts

    def test_connection(self, credentials):
        try:
            accounts = self.list_accounts(credentials)
        except Exception as exc:
            return connectivity.from_exception("SimpleFIN", exc)
        return connectivity.ok(
            f"Connected. SimpleFIN returned {len(accounts)} USD account(s).")

    def list_accounts(self, credentials):
        result = []
        for account in self._accounts(credentials, balances_only=True):
            if str(account.get("currency") or "USD").upper() != "USD":
                print(f"SimpleFIN warning: skipped non-USD account "
                      f"{_safe_warning(account.get('name') or account.get('id'))}.")
                continue
            account_id = str(account.get("id") or "").strip()
            if not account_id:
                print("SimpleFIN warning: skipped an account with no ID.")
                continue
            balance = _decimal(account.get("balance"), "account balance")
            result.append({
                "id": account_id,
                "name": str(account.get("name") or "Unnamed account"),
                "balance": float(balance),
            })
        return result

    def fetch_transactions(self, credentials, since_date=None):
        results = []
        seen = set()
        for account in self._accounts(credentials, since_date=since_date):
            if str(account.get("currency") or "USD").upper() != "USD":
                print(f"SimpleFIN warning: skipped transactions for non-USD account "
                      f"{_safe_warning(account.get('name') or account.get('id'))}.")
                continue
            account_id = str(account.get("id") or "").strip()
            if not account_id:
                print("SimpleFIN warning: skipped transactions for an account with no ID.")
                continue
            for transaction in account.get("transactions") or []:
                if transaction.get("pending"):
                    continue
                transaction_id = str(transaction.get("id") or "").strip()
                try:
                    posted = int(transaction.get("posted") or 0)
                    amount = _decimal(transaction.get("amount"),
                                      "transaction amount")
                except (SimpleFINError, TypeError, ValueError) as exc:
                    print(f"SimpleFIN warning: skipped malformed transaction: {exc}")
                    continue
                if not transaction_id or posted <= 0 or amount == 0:
                    print("SimpleFIN warning: skipped a transaction missing an ID, "
                          "posted date, or non-zero amount.")
                    continue
                external_id = f"simplefin:{account_id}:{transaction_id}"
                if external_id in seen:
                    continue
                seen.add(external_id)
                extra = transaction.get("extra") or {}
                counterparty = (transaction.get("description")
                                or (extra.get("payee") if isinstance(extra, dict) else None)
                                or (extra.get("memo") if isinstance(extra, dict) else None)
                                or "Unknown transaction")
                results.append({
                    "external_id": external_id,
                    "account_id": account_id,
                    "date": datetime.fromtimestamp(
                        posted, tz=timezone.utc).date().isoformat(),
                    # SimpleFIN already follows the canonical sign.
                    "amount": float(amount),
                    "counterparty": str(counterparty),
                })
        return results


ADAPTER = SimpleFINAdapter()
