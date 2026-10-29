"""Optional, read-only Shopify Admin API adapter.

Shopify data is synced to the local SQLite database. Render paths never make
Shopify requests, so the app remains useful while Shopify is unavailable.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_plus, urlencode, urlparse

from dotenv import load_dotenv
import requests

import connectivity
import db
import paths
import secrets_store
from briefing_signals import BriefingSignal, make_briefing_signal
from insight_metrics import (InsightMetric, make_insight_metric,
                             rolling_periods, value_delta)

SECRET_NAME = "klearing_shopify_token"
STORE_KEY = "klearing_shopify_store"
API_VERSION = "2024-01"
REQUEST_INTERVAL = 0.5
MAX_RATE_LIMIT_RETRIES = 2
_last_request_at = 0.0
OAUTH_CALLBACK_HOST = "localhost"
OAUTH_CALLBACK_PORT = 47832
OAUTH_CALLBACK_PATH = "/shopify/callback"
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_CALLBACK_PORT}{OAUTH_CALLBACK_PATH}"
OAUTH_SCOPES = ("read_orders", "read_products", "read_customers", "read_inventory",
                "read_shopify_payments_payouts")
OAUTH_TIMEOUT_SECONDS = 300
_OAUTH_LOCK = threading.Lock()
DOTENV_PATH = (paths.data_dir() / ".env" if paths.is_frozen()
               else Path(__file__).resolve().parent / ".env")
load_dotenv(DOTENV_PATH, override=True)


def _configured(value):
    """Treat the documented placeholder values as unset credentials."""
    value = (value or "").strip()
    return bool(value) and not (
        value.startswith("<") and value.endswith(">")
        or value.startswith("your_")
        or value in {"changeme", "replace-me"}
    )


def credential_status():
    """Return safe dotenv diagnostics without exposing credential values."""
    return {
        "path": str(DOTENV_PATH),
        "exists": DOTENV_PATH.is_file(),
        "client_id_present": _configured(os.environ.get("KLEARING_SHOPIFY_CLIENT_ID")),
        "client_secret_present": _configured(os.environ.get("KLEARING_SHOPIFY_CLIENT_SECRET")),
    }


def _app_credentials():
    """Read the Shopify app's OAuth credentials without exposing them."""
    client_id = os.environ.get("KLEARING_SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("KLEARING_SHOPIFY_CLIENT_SECRET", "").strip()
    if not _configured(client_id):
        client_id = ""
    if not _configured(client_secret):
        client_secret = ""
    return client_id, client_secret


class ShopifyHTTPError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"Shopify returned HTTP {status}: {body[:300]}")


def get_token():
    return secrets_store.get_secret(SECRET_NAME)


def get_store():
    if paths.is_demo():
        return "summit-edge-demo.myshopify.com"
    return (db.get_setting(STORE_KEY) or "").strip().lower()


def _refuse_in_demo():
    if paths.is_demo():
        raise RuntimeError("The demo is not connected to Shopify.")


def is_configured():
    if paths.is_demo():
        return True
    try:
        return bool(get_token() and get_store())
    except Exception:
        return False


def get_briefing_signals() -> list[BriefingSignal]:
    """Return useful Shopify facts from the most recently cached sync."""
    if not is_configured():
        return []

    db.init_db()
    today = date.today()
    orders = db.list_shopify_orders()
    admin_orders_url = f"https://{get_store()}/admin/orders"

    stale_unfulfilled = []
    todays_orders = []
    for order in orders:
        try:
            order_date = date.fromisoformat(str(order["created_at"])[:10])
        except (TypeError, ValueError):
            continue
        fulfillment = (order.get("fulfillment_status") or "").lower()
        financial = (order.get("financial_status") or "").lower()
        if (fulfillment in {"", "unfulfilled", "partial"}
                and financial not in {"refunded", "voided"}
                and (today - order_date).days > 3):
            stale_unfulfilled.append(order)
        if order_date == today:
            todays_orders.append(order)

    signals: list[BriefingSignal] = []
    if stale_unfulfilled:
        count = len(stale_unfulfilled)
        noun = "order has" if count == 1 else "orders have"
        signals.append(make_briefing_signal(
            "urgent",
            f"{count} Shopify {noun} been waiting for fulfillment for more "
            "than three days.",
            admin_orders_url,
        ))

    if todays_orders:
        net = sum(
            Decimal(str(order["total_price"]))
            - Decimal(str(order.get("refund_total") or 0))
            for order in todays_orders
        )
        count = len(todays_orders)
        noun = "order" if count == 1 else "orders"
        net_text = (f"-${abs(net):,.2f}" if net < 0 else f"${net:,.2f}")
        signals.append(make_briefing_signal(
            "informational",
            f"Shopify recorded {count} {noun} totaling {net_text} today.",
            admin_orders_url,
        ))

    return signals


def _order_revenue_cents(order):
    """Net revenue for one postable Shopify order, in exact cents."""
    status = (order.get("financial_status") or "").strip().lower()
    if status in {"refunded", "voided"}:
        return 0
    try:
        net = (Decimal(str(order["total_price"]))
               - Decimal(str(order.get("refund_total") or 0)))
        if net <= 0:
            return 0
        return int((net * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return 0


def get_insight_metrics() -> list[InsightMetric]:
    """Shopify metrics from cached orders; never fetch during a read."""
    if not is_configured():
        return []

    db.init_db()
    orders = db.list_shopify_orders()
    points = []
    for start, end in rolling_periods():
        total = 0
        for order in orders:
            try:
                order_date = date.fromisoformat(str(order["created_at"])[:10])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= order_date <= end:
                total += _order_revenue_cents(order)
        points.append({
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": total,
        })

    value, delta = value_delta(points)
    store = get_store()
    deep_link = (f"https://{store}/admin/orders" if store
                 else "https://admin.shopify.com/")
    return [make_insight_metric(
        "shopify_revenue",
        "Shopify revenue",
        value,
        delta,
        "shopify",
        deep_link,
        "currency_cents",
        points,
    )]


def validate_store(store):
    store = (store or "").strip().lower().replace("https://", "").rstrip("/")
    if not store:
        return "Shopify store URL is empty. Enter your store name."
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", store):
        return "Use your Shopify store domain, for example store.myshopify.com (without https://)."
    return None


def validate_config(token, store):
    if not token or not token.strip():
        return "Shopify access token is empty. Connect Shopify again."
    return validate_store(store)


def _authorization_url(store, client_id, state):
    masked_client_id = (
        f"{client_id[:6]}...{client_id[-4:]}"
        if len(client_id) > 10 else "[too short to mask safely]"
    )
    print(
        f"Shopify OAuth authorize client_id: value={masked_client_id} "
        f"length={len(client_id)}",
        flush=True,
    )
    query = urlencode({
        "client_id": client_id,
        "scope": ",".join(OAUTH_SCOPES),
        "redirect_uri": OAUTH_REDIRECT_URI,
        "state": state,
    })
    return f"https://{store}/admin/oauth/authorize?{query}"


def _valid_callback_hmac(raw_query, client_secret):
    fragments = raw_query.split("&")
    received = [
        fragment.partition("=")[2]
        for fragment in fragments
        if fragment.startswith("hmac=")
    ]
    if len(received) != 1:
        return False
    signed_fragments = sorted(
        fragment for fragment in fragments
        if not fragment.startswith(("hmac=", "signature="))
    )
    message = "&".join(signed_fragments)
    expected = hmac.new(client_secret.encode("utf-8"), message.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received[0])


def _decode_callback_values(raw_query):
    return {
        unquote_plus(key): unquote_plus(value)
        for fragment in raw_query.split("&")
        if "=" in fragment
        for key, _, value in [fragment.partition("=")]
    }


def _validate_callback(raw_query, expected_store, expected_state, client_secret):
    """Validate Shopify's signed callback before accepting its code."""
    print("Shopify OAuth callback: validation started", flush=True)
    if not _valid_callback_hmac(raw_query, client_secret):
        print("Shopify OAuth callback: HMAC verification failed", flush=True)
        return "Shopify callback signature could not be verified."
    values = _decode_callback_values(raw_query)
    print("Shopify OAuth callback: HMAC verification passed", flush=True)
    if not secrets.compare_digest(values.get("state", ""), expected_state):
        print("Shopify OAuth callback: state check failed", flush=True)
        return "Shopify authorization state did not match. Start the connection again."
    print("Shopify OAuth callback: state check passed", flush=True)
    callback_store = (values.get("shop") or "").strip().lower()
    if callback_store != expected_store or validate_store(callback_store):
        print(f"Shopify OAuth callback: domain check failed saved={expected_store} callback={callback_store}", flush=True)
        return "Shopify returned a different or invalid store domain."
    print("Shopify OAuth callback: domain check passed", flush=True)
    try:
        timestamp = int(values.get("timestamp", ""))
    except ValueError:
        print("Shopify OAuth callback: timestamp check failed (not an integer)", flush=True)
        return "Shopify callback timestamp was invalid."
    if abs(time.time() - timestamp) > 600:
        print("Shopify OAuth callback: timestamp check failed (expired)", flush=True)
        return "Shopify authorization callback expired. Start the connection again."
    print("Shopify OAuth callback: timestamp check passed", flush=True)
    if values.get("error"):
        print(f"Shopify OAuth callback: provider returned error={values.get('error')}", flush=True)
        return values.get("error_description") or "Shopify authorization was cancelled."
    if not values.get("code"):
        print("Shopify OAuth callback: authorization code missing", flush=True)
        return "Shopify did not return an authorization code."
    print("Shopify OAuth callback: authorization code present", flush=True)
    return None


def _exchange_authorization_code(store, code):
    """Exchange a verified Shopify OAuth code for an offline access token."""
    store = (store or "").strip().lower().replace("https://", "").rstrip("/")
    client_id, client_secret = _app_credentials()
    if not client_id or not client_secret:
        print("Shopify OAuth token exchange: credentials missing", flush=True)
        return None, "Shopify client credentials are not configured."
    print("Shopify OAuth token exchange: credentials found", flush=True)
    try:
        response = requests.post(
            f"https://{store}/admin/oauth/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=25,
        )
        print(f"Shopify OAuth token exchange: HTTP status={response.status_code}", flush=True)
        try:
            data = response.json()
        except ValueError:
            data = {}
            print("Shopify OAuth token exchange: response was not JSON", flush=True)
        if response.status_code >= 400:
            detail = data.get("error_description") or data.get("error")
            print(f"Shopify OAuth token exchange: failed detail={detail or 'no error detail'}", flush=True)
            return None, detail or f"Shopify token exchange failed (HTTP {response.status_code})."
        token = data.get("access_token")
        if not token:
            print("Shopify OAuth token exchange: access_token missing from response", flush=True)
            return None, "Shopify token exchange returned no access token."
        granted_scopes = {scope.strip() for scope in data.get("scope", "").split(",")}
        missing_scopes = set(OAUTH_SCOPES) - granted_scopes
        if missing_scopes:
            print(f"Shopify OAuth token exchange: missing scopes={','.join(sorted(missing_scopes))}", flush=True)
            return None, "Shopify did not grant the required permissions: " + ", ".join(sorted(missing_scopes))
        print("Shopify OAuth token exchange: succeeded with required scopes", flush=True)
        return token, None
    except requests.RequestException as exc:
        print(f"Shopify OAuth token exchange: request failed error={exc}", flush=True)
        return None, f"Shopify token exchange failed: {exc}"


def authorize(store, timeout=OAUTH_TIMEOUT_SECONDS):
    """Run one browser-based Shopify OAuth authorization-code attempt."""
    store = (store or "").strip().lower().replace("https://", "").rstrip("/")
    error = validate_store(store)
    if error:
        return None, error
    client_id, client_secret = _app_credentials()
    if not client_id or not client_secret:
        return None, "Shopify app credentials are not configured."
    if not _OAUTH_LOCK.acquire(blocking=False):
        return None, "A Shopify authorization is already in progress."

    state = secrets.token_urlsafe(32)
    completed = threading.Event()
    outcome = {"token": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _respond(self, status, message):
            body = ("<!doctype html><title>Klearing</title>"
                    f"<p>{message}</p><p>You can return to Klearing.</p>").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            print(f"Shopify OAuth callback: received path={parsed.path}", flush=True)
            if parsed.path != OAUTH_CALLBACK_PATH:
                print("Shopify OAuth callback: path check failed", flush=True)
                self._respond(404, "This is not the Shopify callback URL.")
                return
            raw_query = parsed.query
            callback_values = _decode_callback_values(raw_query)
            print(f"Shopify OAuth callback: query keys={','.join(sorted(callback_values))}", flush=True)
            print(f"Shopify OAuth stores: saved={store} callback={callback_values.get('shop', '')}", flush=True)
            callback_error = _validate_callback(raw_query, store, state, client_secret)
            if callback_error:
                print(f"Shopify OAuth callback: rejected reason={callback_error}", flush=True)
                outcome["error"] = callback_error
                self._respond(400, "Shopify authorization could not be completed.")
            else:
                print("Shopify OAuth callback: validation passed; starting token exchange", flush=True)
                outcome["token"], outcome["error"] = _exchange_authorization_code(
                    store, callback_values["code"])
                self._respond(200 if outcome["token"] else 502,
                              "Shopify is connected." if outcome["token"]
                              else "Shopify token exchange failed.")
                print(f"Shopify OAuth callback: token exchange completed success={bool(outcome['token'])}", flush=True)
            completed.set()

    server = None
    thread = None
    try:
        try:
            server = ThreadingHTTPServer((OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT), CallbackHandler)
            server.daemon_threads = True
        except OSError as exc:
            return None, f"Klearing could not open the Shopify callback listener on port {OAUTH_CALLBACK_PORT}: {exc}"
        thread = threading.Thread(target=server.serve_forever, daemon=True,
                                  name="shopify-oauth-callback")
        thread.start()
        if not webbrowser.open(_authorization_url(store, client_id, state), new=1):
            return None, "Klearing could not open your browser for Shopify authorization."
        if not completed.wait(timeout):
            return None, "Shopify authorization timed out. Start the connection again."
        return outcome["token"], outcome["error"]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        _OAUTH_LOCK.release()


def _base(store=None):
    return f"https://{store or get_store()}/admin/api/{API_VERSION}"


def _wait_for_slot():
    global _last_request_at
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _get(token, store, path, params=None):
    _refuse_in_demo()
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        _wait_for_slot()
        response = requests.get(_base(store) + path, headers={
            "X-Shopify-Access-Token": token,
            "Accept": "application/json",
        }, params=params or {}, timeout=25)
        if response.status_code != 429:
            if response.status_code != 200:
                raise ShopifyHTTPError(response.status_code, response.text)
            return response
        if attempt == MAX_RATE_LIMIT_RETRIES:
            connectivity.log_rate_limit("Shopify")
            raise connectivity.RateLimitError("Shopify")
        try:
            wait = min(max(float(response.headers.get("Retry-After", "1")), 0.5), 10)
        except ValueError:
            wait = 1
        time.sleep(wait)


def _next_link(response):
    link = response.headers.get("Link", "")
    match = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return match.group(1) if match else None


def _list_all(token, store, path, key, params=None):
    _refuse_in_demo()
    rows, url, first = [], path, True
    while url:
        if first:
            response = _get(token, store, url, params)
            first = False
        else:
            # Pagination links are absolute Shopify URLs, but retain the
            # same headers, pacing, and bounded rate-limit behavior.
            for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
                _wait_for_slot()
                response = requests.get(url, headers={"X-Shopify-Access-Token": token,
                                                       "Accept": "application/json"}, timeout=25)
                if response.status_code != 429:
                    break
                if attempt == MAX_RATE_LIMIT_RETRIES:
                    connectivity.log_rate_limit("Shopify")
                    raise connectivity.RateLimitError("Shopify")
                try:
                    time.sleep(min(max(float(response.headers.get("Retry-After", "1")), 0.5), 10))
                except ValueError:
                    time.sleep(1)
            if response.status_code != 200:
                raise ShopifyHTTPError(response.status_code, response.text)
        rows.extend(response.json().get(key, []))
        url = _next_link(response)
    return rows


def test_connection(token, store):
    error = validate_config(token, store)
    if error:
        return connectivity.fail(error)
    try:
        shop = _get(token.strip(), store.strip().lower().replace("https://", "").rstrip("/"), "/shop.json").json().get("shop", {})
        _get(token.strip(), store.strip().lower().replace("https://", "").rstrip("/"), "/orders.json", {"limit": 1, "status": "any"})
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except ShopifyHTTPError as exc:
        if exc.status in (401, 403):
            return connectivity.fail("Shopify rejected the token. Check its Admin API read permissions and try again.")
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("Shopify", exc)
    return connectivity.ok(f'Shopify connected: "{shop.get("name") or store}" ({shop.get("plan_name") or "plan unavailable"}).')


def _iso(value):
    return (value or "").replace("T", " ")[:19]


def _cost_to_cents(value):
    try:
        cost = Decimal(str(value)).quantize(Decimal("0.01"),
                                             rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not cost.is_finite() or cost < 0:
        return None
    return int(cost * 100)


def _sync_variant_costs(token, store, products):
    """Persist cost snapshots without assigning today's cost to old orders."""
    variants = {}
    for product in products:
        for variant in product.get("variants") or []:
            variant_id = str(variant.get("id") or "").strip()
            if variant_id:
                variants[variant_id] = str(variant.get("inventory_item_id") or "").strip()

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    inventory_ids = sorted({inventory_id for inventory_id in variants.values()
                            if inventory_id})
    inventory_items = {}
    unavailable_detail = None
    for start in range(0, len(inventory_ids), 50):
        batch = inventory_ids[start:start + 50]
        try:
            rows = _list_all(token, store, "/inventory_items.json",
                             "inventory_items", {"ids": ",".join(batch)})
        except (ShopifyHTTPError, connectivity.RateLimitError) as exc:
            unavailable_detail = (
                "Could not read Shopify Inventory Item costs. Reconnect Shopify "
                "with read_inventory and confirm permission to view product costs: "
                f"{exc}"
            )
            break
        inventory_items.update({str(row.get("id")): row for row in rows})

    summary = {"available": 0, "missing": 0, "unavailable": 0}
    for variant_id, inventory_item_id in variants.items():
        item = inventory_items.get(inventory_item_id)
        if unavailable_detail:
            row = {
                "variant_id": variant_id, "captured_at": captured_at,
                "inventory_item_id": inventory_item_id or None,
                "unit_cost_cents": None, "cost_status": "unavailable",
                "detail": unavailable_detail,
            }
        elif not inventory_item_id:
            row = {
                "variant_id": variant_id, "captured_at": captured_at,
                "inventory_item_id": None, "unit_cost_cents": None,
                "cost_status": "missing",
                "detail": "Shopify variant has no inventory_item_id.",
            }
        elif not item:
            row = {
                "variant_id": variant_id, "captured_at": captured_at,
                "inventory_item_id": inventory_item_id, "unit_cost_cents": None,
                "cost_status": "missing",
                "detail": "Shopify returned no matching Inventory Item.",
            }
        else:
            cents = _cost_to_cents(item.get("cost"))
            row = {
                "variant_id": variant_id, "captured_at": captured_at,
                "inventory_item_id": inventory_item_id,
                "unit_cost_cents": cents,
                "cost_status": "available" if cents is not None else "missing",
                "detail": "" if cents is not None else "Inventory Item has no valid unit cost.",
            }
        # Only write when something actually changed. This used to
        # append a row for every variant on every sync, so a store with
        # 200 variants synced hourly grew ~4,800 identical snapshots a
        # day and the cost history became unreadable. The table is a
        # record of changes, not of polls.
        previous = db.latest_shopify_variant_cost(variant_id)
        unchanged = (previous is not None
                     and previous["unit_cost_cents"] == row["unit_cost_cents"]
                     and previous["cost_status"] == row["cost_status"])
        if unchanged:
            summary["unchanged"] = summary.get("unchanged", 0) + 1
        else:
            db.record_shopify_variant_cost(row)
        summary[row["cost_status"]] += 1
    return summary


def _summary_money_cents(summary, field_names):
    total = 0
    found = False
    for field_name in field_names:
        if field_name in summary and summary[field_name] not in (None, ""):
            cents = _cost_to_cents(summary[field_name])
            if cents is None:
                return None, True
            total += cents
            found = True
    return total, found


def _sync_payouts(token, store):
    """Cache Shopify Payments payouts and their fee/gross summary."""
    payouts = _list_all(token, store, "/shopify_payments/payouts.json",
                        "payouts", {"limit": 250})
    synced_at = datetime.now(timezone.utc).isoformat()
    fee_fields = (
        "charges_fee_amount", "refunds_fee_amount", "adjustments_fee_amount",
        "reserved_funds_fee_amount", "retried_payouts_fee_amount",
    )
    gross_fields = (
        "charges_gross_amount", "refunds_gross_amount",
        "adjustments_gross_amount", "reserved_funds_gross_amount",
        "retried_payouts_gross_amount",
    )
    summary = {"synced": 0, "paid": 0, "unpaid": 0}
    for payout in payouts:
        payout_id = str(payout.get("id") or "").strip()
        payout_date = (payout.get("date") or "")[:10]
        if not payout_id or not payout_date:
            continue
        payout_summary = payout.get("summary") or {}
        if not isinstance(payout_summary, dict):
            payout_summary = {}
        fields = {**payout, **payout_summary}
        amount_cents = _cost_to_cents(payout.get("amount"))
        fee_cents, fee_present = _summary_money_cents(fields, fee_fields)
        gross_cents, gross_present = _summary_money_cents(fields, gross_fields)
        if amount_cents is None:
            continue
        if fee_cents is None:
            fee_cents = 0
        if not gross_present or gross_cents is None:
            gross_cents = amount_cents + fee_cents
        db.upsert_shopify_payout({
            "payout_id": payout_id,
            "amount_cents": amount_cents,
            "date": payout_date,
            "status": (payout.get("status") or "").strip().lower(),
            "currency": payout.get("currency") or "",
            "fee_amount_cents": fee_cents,
            "gross_amount_cents": gross_cents,
            "summary_json": json.dumps({
                "raw": payout,
                "fee_fields_present": fee_present,
                "gross_fields_present": gross_present,
            }),
            "synced_at": synced_at,
        })
        summary["synced"] += 1
        if payout.get("status") == "paid":
            summary["paid"] += 1
        else:
            summary["unpaid"] += 1
    return summary


def sync():
    """Fetch Shopify records and upsert local, render-safe fields."""
    if not is_configured():
        return {"orders": 0, "products": 0, "customers": 0,
                "costs": {"available": 0, "missing": 0, "unavailable": 0},
                "payouts": {"synced": 0, "paid": 0, "unpaid": 0}}
    token, store = get_token(), get_store()
    orders = _list_all(token, store, "/orders.json", "orders", {"status": "any", "limit": 250, "fields": "id,created_at,total_price,subtotal_price,total_discounts,total_tax,financial_status,fulfillment_status,customer,source_name,refunds,line_items"})
    products = _list_all(token, store, "/products.json", "products", {"limit": 250, "fields": "id,title,product_type,vendor,status,created_at,variants"})
    customers = _list_all(token, store, "/customers.json", "customers", {"limit": 250, "fields": "id,email,first_name,last_name,orders_count,total_spent,created_at,tags,accepts_marketing"})
    for row in orders:
        customer = row.get("customer") or {}
        refunds = row.get("refunds") or []
        refund_total = sum(float(r.get("transactions", [{}])[0].get("amount") or 0) for r in refunds if r.get("transactions"))
        db.upsert_shopify_order({"order_id": str(row.get("id")), "created_at": _iso(row.get("created_at")), "total_price": float(row.get("total_price") or 0), "subtotal_price": float(row.get("subtotal_price") or 0), "total_discounts": float(row.get("total_discounts") or 0), "total_tax": float(row.get("total_tax") or 0), "financial_status": row.get("financial_status") or "", "fulfillment_status": row.get("fulfillment_status") or "unfulfilled", "customer_id": str(customer.get("id") or ""), "customer_email": customer.get("email") or "", "customer_name": " ".join(x for x in [customer.get("first_name"), customer.get("last_name")] if x), "source": row.get("source_name") or "online_store", "refund_total": refund_total, "line_items_json": __import__("json").dumps(row.get("line_items") or [])})
    for row in products:
        db.upsert_shopify_product({"product_id": str(row.get("id")), "title": row.get("title") or "Untitled product", "product_type": row.get("product_type") or "", "vendor": row.get("vendor") or "", "status": row.get("status") or "", "created_at": _iso(row.get("created_at")), "variants_json": __import__("json").dumps(row.get("variants") or [])})
    cost_summary = _sync_variant_costs(token, store, products)
    try:
        payout_summary = _sync_payouts(token, store)
    except (ShopifyHTTPError, connectivity.RateLimitError) as exc:
        # A store without Shopify Payments, or a temporary payout permission
        # issue, must not discard the otherwise valid order/product sync.
        payout_summary = {"synced": 0, "paid": 0, "unpaid": 0,
                          "unavailable": str(exc)}
    for row in customers:
        db.upsert_shopify_customer({"customer_id": str(row.get("id")), "email": row.get("email") or "", "first_name": row.get("first_name") or "", "last_name": row.get("last_name") or "", "orders_count": int(row.get("orders_count") or 0), "total_spent": float(row.get("total_spent") or 0), "created_at": _iso(row.get("created_at")), "tags": row.get("tags") or "", "accepts_marketing": int(bool(row.get("accepts_marketing")))})
    # Keep the shared contact records in step with the customer sync. This is
    # additive and idempotent (see db.backfill_contacts_from_shopify), and it
    # is deliberately not allowed to fail the sync: a contact-seeding problem
    # must not discard an otherwise valid order/product/customer pull.
    try:
        contact_summary = db.backfill_contacts_from_shopify()
    except Exception as exc:  # noqa: BLE001 - never lose a good sync over this
        contact_summary = {"created": 0, "linked": 0, "updated": 0,
                           "unavailable": str(exc)}
    db.set_setting("shopify_last_synced", datetime.now(timezone.utc).isoformat())
    return {"orders": len(orders), "products": len(products), "customers": len(customers),
            "costs": cost_summary, "payouts": payout_summary,
            "contacts": contact_summary}
