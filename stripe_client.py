"""
Stripe API client (fixed, built-in integration — optional to connect).

Connecting Stripe adds: a Stripe row + in-transit note on Reconcile, an
automatic "Stripe processing fees" vendor row, a Stripe revenue trend
on Growth, and a Connections card.

Almost entirely read-only. The ONE write is minting a hosted payment
link for an invoice (create_payment_link) so a customer can pay it —
Klearing still never creates charges, refunds, or payouts, and never
touches card data: the link redirects to Stripe's own hosted page, so
the integration stays SAQ A. Payment is detected later by polling, not
by an inbound webhook (this app has no public URL to receive one).

The customer pays into their OWN Stripe account — Klearing is a client
of that account, not a platform or payment facilitator. The secret key
lives in the OS-keychain-backed secrets store under
"klearing_stripe_secret"; a restricted key (rk_) scoped to Prices +
Payment Links write and the read endpoints above is sufficient and
recommended. A key starting "sk_test_" is test mode — surfaced with a
warn tag on Connections, since test-mode numbers must never be mistaken
for real revenue.
"""

import time
from datetime import date, datetime, timedelta, timezone

import requests

import connectivity
import secrets_store

# Render-path calls (Vendors' fee row, Growth's revenue line) hit this
# on every page view — a short cache keeps those pages fast without
# going stale enough to matter. Reconcile deliberately bypasses it
# ("checked just now" semantics).
_CACHE = {}
_CACHE_TTL = 600  # seconds


def _cached(name, fn):
    now = time.time()
    hit = _CACHE.get(name)
    if hit and hit[0] > now:
        return hit[1]
    value = fn()
    _CACHE[name] = (now + _CACHE_TTL, value)
    return value


def reset_cache():
    _CACHE.clear()

API_BASE = "https://api.stripe.com/v1"
SECRET_NAME = "klearing_stripe_secret"

LIVE_PREFIX = "sk_live_"
TEST_PREFIX = "sk_test_"


class StripeHTTPError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"Stripe returned HTTP {status}: {body[:300]}")
        self.status = status


def get_key():
    return secrets_store.get_secret(SECRET_NAME)


def is_configured():
    return bool(get_key())


def is_test_mode(key=None):
    key = key or get_key() or ""
    return key.startswith(TEST_PREFIX)


def validate_key_format(key):
    """None if it looks like a secret key; otherwise a specific error.
    Never makes a network call."""
    key = (key or "").strip()
    if not key:
        return "Stripe secret key is empty. Paste it in (don't retype it)."
    if key.startswith("pk_"):
        return ('That is a PUBLISHABLE key ("pk_..."). Klearing needs the '
                'SECRET key ("sk_live_..."), from the Stripe dashboard '
                "under Developers → API keys.")
    if not key.startswith((LIVE_PREFIX, TEST_PREFIX, "rk_")):
        return ('A Stripe secret key starts with "sk_live_" or "sk_test_" '
                f'— yours starts with "{key[:8]}". Copy the whole key '
                "again.")
    return None


def _get(key, path, params=None):
    response = requests.get(f"{API_BASE}{path}", params=params or {},
                            auth=(key, ""), timeout=20)
    connectivity.raise_for_rate_limit("Stripe", response.status_code)
    if response.status_code != 200:
        raise StripeHTTPError(response.status_code, response.text)
    return response.json()


def _post(key, path, data):
    """The single write path. Stripe takes form-encoded bodies, including
    the bracketed nested keys (product_data[name], line_items[0][price]),
    which requests sends verbatim from a flat dict."""
    response = requests.post(f"{API_BASE}{path}", data=data,
                             auth=(key, ""), timeout=20)
    connectivity.raise_for_rate_limit("Stripe", response.status_code)
    if response.status_code not in (200, 201):
        raise StripeHTTPError(response.status_code, response.text)
    return response.json()


def test_connection(key):
    """Live check against /account. Returns connectivity.TestResult."""
    format_error = validate_key_format(key)
    if format_error:
        return connectivity.fail(format_error)
    try:
        account = _get(key, "/account")
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except StripeHTTPError as exc:
        if exc.status == 401:
            return connectivity.fail(
                "Stripe rejected the key. It may have been rolled or "
                "revoked — create a new secret key under Developers → "
                "API keys and paste it again.")
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("Stripe", exc)
    name = account_display_name(account)
    mode = "TEST mode" if is_test_mode(key) else "live mode"
    return connectivity.ok(f'Stripe connected: "{name}" ({mode}).')


def account_display_name(account=None):
    """The connected account's human name, best-effort."""
    if account is None:
        account = _get(get_key(), "/account")
    profile = account.get("business_profile") or {}
    settings = (account.get("settings") or {}).get("dashboard") or {}
    return (profile.get("name") or settings.get("display_name")
            or account.get("email") or account.get("id") or "Stripe account")


def _cents(amount):
    return (amount or 0) / 100.0


def balance():
    """{'available': $, 'in_transit': $} — pending is money Stripe holds
    that hasn't settled to the bank yet (what Reconcile cares about)."""
    data = _get(get_key(), "/balance")
    available = sum(_cents(b.get("amount")) for b in data.get("available", []))
    pending = sum(_cents(b.get("amount")) for b in data.get("pending", []))
    return {"available": available, "in_transit": pending}


def _list_all(key, path, params, cap=500):
    """Auto-paginate a Stripe list endpoint, newest first, hard-capped."""
    items = []
    params = dict(params, limit=100)
    while len(items) < cap:
        data = _get(key, path, params)
        chunk = data.get("data", [])
        items.extend(chunk)
        if not data.get("has_more") or not chunk:
            break
        params["starting_after"] = chunk[-1]["id"]
    return items[:cap]


def _since_epoch(days):
    return int((datetime.now(timezone.utc) - timedelta(days=days))
               .timestamp())


# --------------------------------------------------------- payment links
# The one write. A Payment Link needs a Price, and a Price can create its
# Product inline, so this is two POSTs: a Price for the invoice's gross
# amount, then a Link referencing it. The invoice is represented as a
# single "Invoice <number>" line rather than itemised -- the itemisation
# is already on the PDF, and one line keeps the Stripe-side objects
# minimal and the amount unambiguous.


def create_payment_link(invoice_number, amount_cents, invoice_id,
                        currency="usd", key=None):
    """Mint a hosted payment link for one invoice.

    Returns {"id", "url"}. metadata carries invoice_id and invoice_number
    so the payment poller (check_bank / ledger reconciliation) can match a
    completed Checkout Session back to the invoice without guessing.

    Raises StripeHTTPError; a 403 there almost always means a restricted
    key without Prices/Payment Links write scope -- the caller turns that
    into a plain-language message.
    """
    key = key or get_key()
    if not key:
        raise StripeHTTPError(401, "Stripe is not connected.")
    if amount_cents <= 0:
        raise StripeHTTPError(400, "A payment link needs a positive amount.")

    price = _post(key, "/prices", {
        "currency": currency,
        "unit_amount": int(amount_cents),
        "product_data[name]": f"Invoice {invoice_number}",
    })
    link = _post(key, "/payment_links", {
        "line_items[0][price]": price["id"],
        "line_items[0][quantity]": 1,
        "metadata[invoice_id]": str(invoice_id),
        "metadata[invoice_number]": invoice_number,
    })
    return {"id": link["id"], "url": link["url"]}


def payments_for_link(payment_link_id, key=None):
    """Completed Checkout Sessions against one payment link.

    The detection half of the polling model: a customer paying a link
    creates a Checkout Session, and a settled one has
    payment_status == "paid". Returns
    [{'session_id', 'amount_total_cents', 'paid', 'payment_intent'}].
    """
    key = key or get_key()
    sessions = _list_all(key, "/checkout/sessions",
                         {"payment_link": payment_link_id})
    results = []
    for session in sessions:
        results.append({
            "session_id": session.get("id"),
            "amount_total_cents": session.get("amount_total") or 0,
            "paid": session.get("payment_status") == "paid",
            "payment_intent": session.get("payment_intent"),
        })
    return results


def payment_intent_fee(payment_intent_id, key=None):
    """The exact processor fee in cents for a paid PaymentIntent.

    Read, not estimated: expands to the settling balance transaction and
    returns its fee. Returns 0 if it cannot be resolved yet (a charge not
    yet settled has no balance transaction) rather than guessing -- the
    caller can retry on a later poll.
    """
    if not payment_intent_id:
        return 0
    key = key or get_key()
    intent = _get(key, f"/payment_intents/{payment_intent_id}",
                  {"expand[]": "latest_charge.balance_transaction"})
    charge = intent.get("latest_charge")
    if not isinstance(charge, dict):
        return 0
    balance_txn = charge.get("balance_transaction")
    if not isinstance(balance_txn, dict):
        return 0
    return int(balance_txn.get("fee") or 0)


def recent_charges(days=180):
    """Paid charges: [{'amount', 'customer', 'date', 'refunded'}]."""
    raw = _list_all(get_key(), "/charges",
                    {"created[gte]": _since_epoch(days)})
    charges = []
    for c in raw:
        if not c.get("paid") or c.get("status") != "succeeded":
            continue
        charges.append({
            "amount": _cents(c.get("amount")),
            "amount_refunded": _cents(c.get("amount_refunded")),
            "customer": (c.get("billing_details") or {}).get("name")
            or c.get("customer") or "",
            "date": datetime.fromtimestamp(
                c.get("created", 0), timezone.utc).date().isoformat(),
            "refunded": bool(c.get("refunded")),
        })
    return charges


def recent_refunds(days=180):
    raw = _list_all(get_key(), "/refunds",
                    {"created[gte]": _since_epoch(days)})
    return [{"amount": _cents(r.get("amount")),
             "reason": r.get("reason") or "",
             "date": datetime.fromtimestamp(
                 r.get("created", 0), timezone.utc).date().isoformat()}
            for r in raw]


def recent_disputes(days=180):
    raw = _list_all(get_key(), "/disputes",
                    {"created[gte]": _since_epoch(days)})
    return [{"amount": _cents(d.get("amount")),
             "status": d.get("status") or "",
             "date": datetime.fromtimestamp(
                 d.get("created", 0), timezone.utc).date().isoformat()}
            for d in raw]


def recent_payouts(days=180):
    """Payouts to the bank, newest first."""
    raw = _list_all(get_key(), "/payouts",
                    {"created[gte]": _since_epoch(days)})
    return [{"amount": _cents(p.get("amount")),
             "status": p.get("status") or "",
             "date": datetime.fromtimestamp(
                 p.get("arrival_date") or p.get("created", 0),
                 timezone.utc).date().isoformat()}
            for p in raw]


def fees_in_range(start_iso, end_iso):
    """(total fees $, transaction count) from balance transactions in a
    date range — the Vendors "processing fees" row. Cached."""
    return _cached(f"fees:{start_iso}:{end_iso}",
                   lambda: _fees_in_range_live(start_iso, end_iso))


def _fees_in_range_live(start_iso, end_iso):
    key = get_key()
    start = int(datetime.combine(
        date.fromisoformat(start_iso), datetime.min.time(),
        timezone.utc).timestamp())
    end = int(datetime.combine(
        date.fromisoformat(end_iso), datetime.max.time(),
        timezone.utc).timestamp())
    raw = _list_all(key, "/balance_transactions",
                    {"created[gte]": start, "created[lte]": end})
    total = sum(_cents(t.get("fee")) for t in raw)
    count = sum(1 for t in raw if (t.get("fee") or 0) > 0)
    return total, count


def monthly_revenue(months=6):
    """[(month_label, total $)] oldest first — succeeded charge amounts
    net of refunds, grouped by calendar month. Growth's Stripe line.
    Cached."""
    return _cached(f"monthly_revenue:{months}",
                   lambda: _monthly_revenue_live(months))


def _monthly_revenue_live(months=6):
    charges = recent_charges(days=months * 31 + 5)
    by_month = {}
    for c in charges:
        month = c["date"][:7]
        by_month[month] = (by_month.get(month, 0.0)
                           + c["amount"] - c["amount_refunded"])
    out = []
    today = date.today()
    year, month = today.year, today.month
    seq = []
    for k in range(months):
        mm, yy = month - k, year
        while mm <= 0:
            mm += 12
            yy -= 1
        seq.append((yy, mm))
    for yy, mm in reversed(seq):
        key = f"{yy:04d}-{mm:02d}"
        out.append((date(yy, mm, 1).strftime("%b"),
                    round(by_month.get(key, 0.0), 2)))
    return out
