"""
Gmail READ-ONLY client (fixed, built-in integration — optional).

Klearing reads Gmail to answer three narrow questions:
- did an overdue-invoice client send a payment-confirmation email?
- did a follow-up lead already reply?
- how many receipt emails are on file per vendor domain?

Scope is gmail.readonly and nothing else — Klearing never sends from,
labels, archives, or otherwise modifies the mailbox. This module is
deliberately separate from email_utils.py (the SMTP app-password
SENDING path): reading uses an OAuth token with a read-only scope,
stored under "klearing_gmail_token" in the OS-keychain-backed store.

The stored secret is the OAuth token JSON (access_token, and when
available refresh_token + client_id + client_secret + token_uri for
silent refresh). Generating it requires the user's own Google OAuth
client — done once, outside the app; Klearing only ever presents the
token, and only to googleapis.com.
"""

import json
import re
import time
from datetime import datetime, timezone

import requests

import connectivity
import db
import secrets_store

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SECRET_NAME = "klearing_gmail_token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

ADDRESS_KEY = "gmail_reader_address"
LAST_SYNC_KEY = "gmail_reader_last_sync"

PAYMENT_SUBJECT_QUERY = ('subject:("payment received" OR "invoice paid" '
                         'OR "receipt for")')
RECEIPT_QUERY = 'subject:(receipt OR "receipt for" OR "your invoice")'

_CACHE = {}
_CACHE_TTL = 600


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


def _load_token():
    raw = secrets_store.get_secret(SECRET_NAME)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"access_token": raw}
    except ValueError:
        return {"access_token": raw.strip()}


def is_configured():
    return _load_token() is not None


def validate_token_format(raw):
    """None if plausible, else a specific error. No network call."""
    raw = (raw or "").strip()
    if not raw:
        return ("Gmail token is empty. Paste the OAuth token JSON (or "
                "access token) in one go.")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return ("That JSON doesn't parse — part of it was probably "
                    "cut off in the paste. Copy the whole thing again.")
        if not data.get("access_token") and not data.get("refresh_token"):
            return ('The JSON has no "access_token" or "refresh_token" '
                    "field — paste the token JSON, not the client "
                    "credentials file.")
    return None


def _mark_synced():
    db.set_setting(LAST_SYNC_KEY, datetime.now().isoformat(
        timespec="minutes"))


def _access_token():
    """A usable access token, refreshing silently when the stored JSON
    carries refresh material. Raises on failure."""
    token = _load_token()
    if token is None:
        raise RuntimeError("Gmail (read-only) isn't connected.")
    access = token.get("access_token")
    if access and not _expired(token):
        return access
    if token.get("refresh_token") and token.get("client_id"):
        response = requests.post(
            token.get("token_uri") or "https://oauth2.googleapis.com/token",
            data={"grant_type": "refresh_token",
                  "refresh_token": token["refresh_token"],
                  "client_id": token["client_id"],
                  "client_secret": token.get("client_secret", "")},
            timeout=20)
        connectivity.raise_for_rate_limit("Gmail", response.status_code)
        if response.status_code == 200:
            fresh = response.json()
            token["access_token"] = fresh.get("access_token", "")
            token["expires_at"] = time.time() + int(
                fresh.get("expires_in", 3600))
            secrets_store.set_secret(SECRET_NAME, json.dumps(token))
            return token["access_token"]
    if access:
        return access  # possibly expired; the API call will 401 clearly
    raise RuntimeError("The stored Gmail token has no usable access "
                       "token and couldn't be refreshed.")


def _expired(token):
    expires_at = token.get("expires_at")
    return bool(expires_at) and time.time() > float(expires_at) - 60


def _get(path, params=None):
    response = requests.get(
        f"{API_BASE}{path}", params=params or {},
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=20)
    connectivity.raise_for_rate_limit("Gmail", response.status_code)
    if response.status_code != 200:
        raise RuntimeError(f"Gmail returned HTTP {response.status_code}: "
                           f"{response.text[:300]}")
    return response.json()


def test_connection(raw_token):
    """Live check against the profile endpoint. Caches the address for
    the Connections card. Returns connectivity.TestResult."""
    format_error = validate_token_format(raw_token)
    if format_error:
        return connectivity.fail(format_error)
    stored = secrets_store.get_secret(SECRET_NAME)
    try:
        secrets_store.set_secret(SECRET_NAME, raw_token.strip())
        profile = _get("/profile")
    except connectivity.RateLimitError as exc:
        if stored is not None:
            secrets_store.set_secret(SECRET_NAME, stored)
        else:
            secrets_store.delete_secret(SECRET_NAME)
        return connectivity.fail(str(exc))
    except Exception as exc:
        # Restore whatever was there before a failed swap-in.
        if stored is not None:
            secrets_store.set_secret(SECRET_NAME, stored)
        else:
            secrets_store.delete_secret(SECRET_NAME)
        if "401" in str(exc):
            return connectivity.fail(
                "Google rejected the token — it may have expired. "
                "Generate a fresh read-only token and paste it again.")
        return connectivity.from_exception("Gmail", exc)
    address = profile.get("emailAddress") or "unknown address"
    db.set_setting(ADDRESS_KEY, address)
    _mark_synced()
    return connectivity.ok(f"Gmail (read-only) connected: {address}.")


def amounts_match(a, b, tolerance=0.05):
    """True when two dollar amounts are within `tolerance` of the
    larger one — the 5%% proximity rule for payment matching."""
    a, b = abs(a), abs(b)
    if a == b:
        return True
    return abs(a - b) <= max(a, b) * tolerance


_AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")


def _amounts_in(text):
    out = []
    for raw in _AMOUNT_RE.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def _search(query, cap=10):
    """Message stubs for a Gmail query: [{'id', 'date', 'from',
    'snippet'}], newest first."""
    listing = _get("/messages", {"q": query, "maxResults": cap})
    stubs = []
    for ref in (listing.get("messages") or [])[:cap]:
        msg = _get(f'/messages/{ref["id"]}',
                   {"format": "metadata",
                    "metadataHeaders": ["From", "Subject"]})
        headers = {h["name"].lower(): h["value"]
                   for h in (msg.get("payload") or {}).get("headers", [])}
        stamp = int(msg.get("internalDate") or 0) / 1000.0
        stubs.append({
            "id": ref["id"],
            "date": datetime.fromtimestamp(
                stamp, timezone.utc).date().isoformat() if stamp else "",
            "from": headers.get("from", ""),
            "snippet": msg.get("snippet", ""),
        })
    _mark_synced()
    return stubs


def find_payment_confirmation(client_email, amount):
    """The newest payment-confirmation email from this client whose
    snippet mentions a dollar figure within 5%% of `amount` (or
    mentions no figure at all — subject match still counts). Returns
    {'date': ...} or None. Cached per client+amount."""
    key = f"pay:{client_email}:{amount:.2f}"
    return _cached(key,
                   lambda: _find_payment_live(client_email, amount))


def _find_payment_live(client_email, amount):
    stubs = _search(f"from:{client_email} {PAYMENT_SUBJECT_QUERY}")
    for stub in stubs:
        found = _amounts_in(stub["snippet"])
        if not found or any(amounts_match(amount, f) for f in found):
            return {"date": stub["date"]}
    return None


def find_reply(client_email, since_iso):
    """The newest email from this address since a date, or None —
    Follow-ups' "did they already reply" check. Cached."""
    key = f"reply:{client_email}:{since_iso}"
    return _cached(key, lambda: _find_reply_live(client_email, since_iso))


def _find_reply_live(client_email, since_iso):
    after = (since_iso or "")[:10].replace("-", "/")
    query = f"from:{client_email}"
    if after:
        query += f" after:{after}"
    stubs = _search(query, cap=3)
    if stubs:
        return {"date": stubs[0]["date"]}
    return None


_DOMAIN_RE = re.compile(r"@([\w.-]+)")


def receipt_counts_by_domain():
    """{sender_domain: receipt-email count} over recent receipt-pattern
    mail — Vendors' "receipts on file" note. Cached."""
    return _cached("receipt_counts", _receipt_counts_live)


def _receipt_counts_live():
    stubs = _search(RECEIPT_QUERY, cap=50)
    counts = {}
    for stub in stubs:
        match = _DOMAIN_RE.search(stub["from"])
        if match:
            domain = match.group(1).lower()
            counts[domain] = counts.get(domain, 0) + 1
    return counts


def domain_matches_vendor(domain, vendor):
    """True when a sender domain plausibly belongs to a vendor name —
    letters-only comparison ("Bambu Lab" ~ bambulab.com)."""
    root = (domain or "").lower().split(".")[0]
    name = re.sub(r"[^a-z0-9]", "", (vendor or "").lower())
    root = re.sub(r"[^a-z0-9]", "", root)
    if not root or not name:
        return False
    return root in name or name in root
