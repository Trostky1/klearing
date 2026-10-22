"""Plaid Link Phase 1 integration.

This module is intentionally separate from the legacy Setup-wizard Plaid
adapter. Phase 1 reads app credentials from the same dotenv locations as
Shopify, supports non-OAuth institutions only, and stores the resulting
Item credentials in Klearing's encrypted credential store.

Phase 2 will add Plaid OAuth redirect handling and webhooks. This flow does
not send either a redirect_uri or webhook to Plaid.
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
import requests

import paths
import db
import secrets_store
from briefing_signals import BriefingSignal, make_briefing_signal
from insight_metrics import (InsightMetric, make_insight_metric,
                             rolling_periods, value_delta)

TOKEN_SECRET_NAME = "klearing_plaid_token"
ITEM_ID_SECRET_NAME = "klearing_plaid_item_id"
SYNC_CURSOR_SECRET_NAME = "klearing_plaid_sync_cursor"

DOTENV_PATH = (paths.data_dir() / ".env" if paths.is_frozen()
               else Path(__file__).resolve().parent / ".env")
load_dotenv(DOTENV_PATH, override=True)

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


def _amount_to_cents(value):
    """Convert Plaid's JSON amount to exact cents for SQLite storage."""
    try:
        amount = Decimal(str(value))
        rounded = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise PlaidAPIError(502, {
            "error_message": "Plaid returned an invalid transaction amount."
        })
    if not amount.is_finite() or amount != rounded:
        raise PlaidAPIError(502, {
            "error_message": (
                "Plaid returned a transaction amount with sub-cent precision.")
        })
    return int(rounded * 100)


def _persist_sync_records(data):
    """Cache sync rows locally; ledger posting happens separately and locally."""
    db.init_db()
    synced_at = datetime.now(timezone.utc).isoformat()
    for transaction in data.get("added", []) + data.get("modified", []):
        transaction_id = str(transaction.get("transaction_id") or "").strip()
        if not transaction_id:
            continue
        category = transaction.get("personal_finance_category") or {}
        if not isinstance(category, dict):
            category = {}
        transaction_date = (transaction.get("date")
                            or transaction.get("authorized_date") or "")[:10]
        if not transaction_date:
            continue
        db.upsert_plaid_transaction({
            "transaction_id": transaction_id,
            "account_id": str(transaction.get("account_id") or ""),
            "date": transaction_date,
            "authorized_date": (transaction.get("authorized_date") or "")[:10]
                               or None,
            # Plaid is positive-out; the local bank boundary is negative-out.
            "amount_cents": -_amount_to_cents(transaction.get("amount")),
            "merchant_name": transaction.get("merchant_name") or "",
            "name": transaction.get("name") or "",
            "pending": int(bool(transaction.get("pending"))),
            "category_primary": category.get("primary") or "",
            "category_detailed": category.get("detailed") or "",
            "removed": 0,
            "synced_at": synced_at,
        })
    for transaction in data.get("removed", []):
        transaction_id = str(transaction.get("transaction_id") or "").strip()
        if transaction_id:
            db.mark_plaid_transaction_removed(transaction_id, synced_at)


def _persist_accounts(data):
    """Cache account metadata used to map Plaid IDs to ledger accounts."""
    synced_at = datetime.now(timezone.utc).isoformat()
    for account in data.get("accounts", []):
        account_id = str(account.get("account_id") or "").strip()
        if not account_id:
            continue
        db.upsert_plaid_account({
            "account_id": account_id,
            "name": account.get("name") or "",
            "official_name": account.get("official_name"),
            "type": account.get("type") or "",
            "subtype": account.get("subtype") or "",
            "mask": account.get("mask"),
            "synced_at": synced_at,
        })


class PlaidConfigError(RuntimeError):
    """Plaid app credentials are missing or invalid for Phase 1."""


class PlaidAPIError(RuntimeError):
    """Plaid returned a structured API error or could not be reached."""

    def __init__(self, status_code, payload=None):
        payload = payload or {}
        self.status_code = status_code
        self.error_code = payload.get("error_code")
        message = (payload.get("error_message")
                   or payload.get("display_message")
                   or f"Plaid returned HTTP {status_code}")
        self.plaid_message = message
        super().__init__(self._safe_message())

    def _safe_message(self):
        if self.error_code:
            return f"Plaid {self.error_code}: {self.plaid_message}"
        return self.plaid_message


def _configured(value):
    value = (value or "").strip()
    return bool(value) and not (
        (value.startswith("<") and value.endswith(">"))
        or value.startswith("your_")
        or value in {"changeme", "replace-me"}
    )


def credential_status():
    """Safe configuration diagnostics; never return credential values."""
    environment = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    return {
        "path": str(DOTENV_PATH),
        "exists": DOTENV_PATH.is_file(),
        "client_id_present": _configured(os.environ.get("PLAID_CLIENT_ID")),
        "secret_present": _configured(os.environ.get("PLAID_SECRET")),
        "environment": environment if environment in PLAID_HOSTS else "invalid",
    }


def is_configured():
    """Whether this device has a complete Plaid Item connection."""
    try:
        return bool(secrets_store.get_secret(TOKEN_SECRET_NAME)
                    and secrets_store.get_secret(ITEM_ID_SECRET_NAME))
    except Exception:
        return False


def get_briefing_signals() -> list[BriefingSignal]:
    """Return useful Plaid facts from cached data without polling Plaid."""
    if not is_configured():
        return []

    db.init_db()
    pending = db.count_pending_plaid_transactions()
    if not pending:
        return []
    noun = "transaction is" if pending == 1 else "transactions are"
    return [make_briefing_signal(
        "informational",
        f"{pending} bank {noun} still pending in Plaid.",
        "https://my.plaid.com/",
    )]


def _plaid_category(transaction):
    """Stable, readable expense category from Plaid's cached taxonomy."""
    raw = (transaction.get("category_primary")
           or transaction.get("category_detailed") or "Uncategorized")
    return " ".join(str(raw).replace("_", " ").split()).title()


def _cash_position_metric():
    """Latest exact Plaid-bank snapshots already stored by reconciliation."""
    points = []
    if db.get_setting("active_bank") == "plaid":
        for row in db.list_reconcile_history(days=180):
            try:
                cents = _amount_to_cents(row["bank_balance"])
                point_date = date.fromisoformat(row["date"]).isoformat()
            except (PlaidAPIError, KeyError, TypeError, ValueError):
                continue
            points.append({
                "period_start": point_date,
                "period_end": point_date,
                "value": cents,
            })
    value, delta = value_delta(points)
    return make_insight_metric(
        "plaid_cash_position",
        "Cash position",
        value,
        delta,
        "plaid",
        "https://my.plaid.com/",
        "currency_cents",
        points,
    )


def _top_expense_metric(transactions):
    windows = rolling_periods()
    current_start, current_end = windows[-1]
    totals = {}
    for transaction in transactions:
        try:
            transaction_date = date.fromisoformat(transaction["date"])
            amount = int(transaction["amount_cents"])
        except (KeyError, TypeError, ValueError):
            continue
        taxonomy = " ".join((transaction.get("category_primary") or "",
                              transaction.get("category_detailed") or "")).upper()
        if (amount >= 0 or "TRANSFER" in taxonomy
                or not current_start <= transaction_date <= current_end):
            continue
        category = _plaid_category(transaction)
        totals[category] = totals.get(category, 0) + abs(amount)

    if not totals:
        return make_insight_metric(
            "plaid_top_expense_category", "Top expense category",
            None, None, "plaid", "https://my.plaid.com/",
            "currency_cents", [])

    top_category = min(totals, key=lambda category: (-totals[category], category))
    points = []
    for start, end in windows:
        total = 0
        for transaction in transactions:
            try:
                transaction_date = date.fromisoformat(transaction["date"])
                amount = int(transaction["amount_cents"])
            except (KeyError, TypeError, ValueError):
                continue
            if (amount < 0 and start <= transaction_date <= end
                    and _plaid_category(transaction) == top_category):
                taxonomy = " ".join((transaction.get("category_primary") or "",
                                      transaction.get("category_detailed") or "")).upper()
                if "TRANSFER" not in taxonomy:
                    total += abs(amount)
        points.append({
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": total,
        })
    value, delta = value_delta(points)
    return make_insight_metric(
        "plaid_top_expense_category",
        f"Top expense: {top_category}",
        value,
        delta,
        "plaid",
        "https://my.plaid.com/",
        "currency_cents",
        points,
    )


def get_insight_metrics() -> list[InsightMetric]:
    """Plaid metrics from settled cached rows and stored balance snapshots."""
    if not is_configured():
        return []
    db.init_db()
    transactions = db.list_plaid_transactions_for_ledger()
    return [_cash_position_metric(), _top_expense_metric(transactions)]


def _credentials():
    client_id = os.environ.get("PLAID_CLIENT_ID", "").strip()
    secret = os.environ.get("PLAID_SECRET", "").strip()
    environment = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    if not _configured(client_id) or not _configured(secret):
        raise PlaidConfigError(
            f"Plaid credentials are not configured. Add PLAID_CLIENT_ID and "
            f"PLAID_SECRET to {DOTENV_PATH}."
        )
    if environment not in PLAID_HOSTS:
        raise PlaidConfigError(
            "PLAID_ENV must be sandbox or production. Phase 1 defaults to sandbox."
        )
    return client_id, secret, environment


def _request(path, body):
    client_id, secret, environment = _credentials()
    payload = {"client_id": client_id, "secret": secret, **body}
    try:
        response = requests.post(
            f"{PLAID_HOSTS[environment]}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise PlaidAPIError(503, {"error_message": f"Plaid request failed: {exc}"})
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200:
        raise PlaidAPIError(response.status_code, data)
    return data


def create_link_token():
    """Create a non-OAuth Link token for the current local user."""
    data = _request("/link/token/create", {
        "client_name": "Klearing",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "klearing-local-user"},
        "products": ["transactions"],
    })
    token = data.get("link_token")
    if not token:
        raise PlaidAPIError(502, {"error_message": "Plaid returned no link token."})
    return token


def exchange_public_token(public_token):
    """Exchange Link's short-lived token and encrypt the Item credentials."""
    public_token = (public_token or "").strip()
    if not public_token:
        raise PlaidConfigError("Plaid did not return a public token.")
    data = _request("/item/public_token/exchange", {
        "public_token": public_token,
    })
    access_token = data.get("access_token")
    item_id = data.get("item_id")
    if not access_token or not item_id:
        raise PlaidAPIError(502, {
            "error_message": "Plaid returned incomplete Item credentials."
        })
    secrets_store.set_secret(TOKEN_SECRET_NAME, access_token)
    secrets_store.set_secret(ITEM_ID_SECRET_NAME, item_id)
    # A new Item starts a new cursor stream.
    secrets_store.delete_secret(SYNC_CURSOR_SECRET_NAME)
    return item_id


def sync(connection_id):
    """Fetch the next transaction page and persist its cursor securely."""
    connection_id = (connection_id or "").strip()
    item_id = secrets_store.get_secret(ITEM_ID_SECRET_NAME)
    access_token = secrets_store.get_secret(TOKEN_SECRET_NAME)
    if not item_id or not access_token:
        raise PlaidConfigError("No Plaid bank is connected yet.")
    if connection_id != item_id:
        raise PlaidConfigError("That Plaid connection is not available on this device.")

    body = {"access_token": access_token, "count": 500}
    cursor = secrets_store.get_secret(SYNC_CURSOR_SECRET_NAME)
    if cursor:
        body["cursor"] = cursor
    data = _request("/transactions/sync", body)
    _persist_sync_records(data)
    # Transaction Sync only carries an opaque account_id. Account metadata is
    # fetched separately so source posting can distinguish connected accounts.
    try:
        _persist_accounts(_request("/accounts/get", {"access_token": access_token}))
    except PlaidAPIError:
        # The normalized transaction batch is still safe to retain. Existing
        # account mappings keep source sync usable while a transient metadata
        # request is unavailable; an unknown account remains unposted.
        pass
    next_cursor = (data.get("next_cursor") or "").strip()
    if next_cursor:
        secrets_store.set_secret(SYNC_CURSOR_SECRET_NAME, next_cursor)
    else:
        secrets_store.delete_secret(SYNC_CURSOR_SECRET_NAME)
    return {
        "connection_id": item_id,
        "added": data.get("added", []),
        "modified": data.get("modified", []),
        "removed": data.get("removed", []),
        "has_more": bool(data.get("has_more")),
        "next_cursor_saved": bool(next_cursor),
    }
