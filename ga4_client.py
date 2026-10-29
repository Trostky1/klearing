"""
GA4 Data API client for Klearing's Insights tab.

GA4 is optional: is_configured() gates every caller, so a user who
skipped it in Setup never sees an error, just no Insights data.

The service-account JSON key lives in the secrets store; the property
id (a plain number — not the G- Measurement ID) lives in settings.

This talks to the GA4 Data API over plain REST with a hand-built
service-account JWT instead of the google-analytics-data client
library. Reason: that library drags in grpc + protobuf, which roughly
triples the packaged app's size and is the classic source of
PyInstaller hidden-import breakage; the REST protocol underneath is
small, stable, and needs only `requests` + `cryptography` (already
shipped for the credential store).

All calls are read-only against GA4. The daily snapshot
(take_daily_snapshot) is the only thing that should call them in
normal operation; the Insights tab reads the stored snapshot instead
of hitting the API on page load.
"""

import base64
import json
import time
from datetime import datetime, timezone

import requests

import connectivity
import db
import paths
import secrets_store

SERVICE_ACCOUNT_SECRET = "ga4.service_account_json"
PROPERTY_ID_KEY = "ga4_property_id"
CONVERSION_EVENTS_KEY = "ga4_conversion_events"

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"

# Access token cache (module-level, per process).
_token = None
_token_expiry = 0.0


def reset_cache():
    """Drop the cached access token — call after the key is replaced
    or removed, so a token minted for the old key isn't reused."""
    global _token, _token_expiry
    _token = None
    _token_expiry = 0.0


class GA4ConfigError(RuntimeError):
    """A specific, user-facing problem with the GA4 configuration."""


def is_configured():
    if paths.is_demo():
        return True
    db.init_db()
    return (secrets_store.has_secret(SERVICE_ACCOUNT_SECRET)
            and bool(db.get_setting(PROPERTY_ID_KEY)))


def conversion_events():
    """Event names the user asked to track (Settings), oldest first."""
    db.init_db()
    raw = db.get_setting(CONVERSION_EVENTS_KEY)
    if not raw:
        return []
    try:
        events = json.loads(raw)
    except ValueError:
        return []
    return [e for e in events if isinstance(e, str) and e.strip()]


def parse_service_account_json(raw_bytes):
    """Validate a service-account key file and return its parsed dict.
    Raises GA4ConfigError with a specific, actionable message — this is
    where 'wrong kind of Google JSON file' gets caught."""
    try:
        info = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GA4ConfigError(
            "That file isn't valid JSON. Upload the .json key file "
            "exactly as Google Cloud downloaded it."
        )
    if not isinstance(info, dict):
        raise GA4ConfigError("That JSON file doesn't look like a key file.")
    if "installed" in info or "web" in info:
        raise GA4ConfigError(
            "This is an OAuth client file, not a service account key. "
            "In Google Cloud: IAM & Admin → Service Accounts → your "
            "account → Keys → Add key → Create new key → JSON."
        )
    if info.get("type") != "service_account":
        raise GA4ConfigError(
            'This JSON file has type "%s", but a service account key '
            'has type "service_account". Download the key from IAM & '
            "Admin → Service Accounts → Keys." % info.get("type", "?")
        )
    for field in ("client_email", "private_key", "token_uri"):
        if not info.get(field):
            raise GA4ConfigError(
                f'The key file is missing its "{field}" field — it may '
                f"be truncated. Download a fresh key from Google Cloud."
            )
    return info


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _fetch_access_token(info):
    """Exchange a signed JWT for a short-lived access token."""
    if paths.is_demo():
        raise RuntimeError("The demo is not connected to Google Analytics.")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": info["client_email"],
        "scope": SCOPE,
        "aud": info["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        _b64url(json.dumps(header).encode("utf-8"))
        + b"."
        + _b64url(json.dumps(claims).encode("utf-8"))
    )
    private_key = serialization.load_pem_private_key(
        info["private_key"].encode("utf-8"), password=None
    )
    signature = private_key.sign(
        signing_input, padding.PKCS1v15(), hashes.SHA256()
    )
    assertion = (signing_input + b"." + _b64url(signature)).decode("ascii")

    response = requests.post(
        info["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=20,
    )
    connectivity.raise_for_rate_limit("GA4", response.status_code)
    payload = response.json() if response.content else {}
    if response.status_code != 200:
        error = payload.get("error", "")
        description = payload.get("error_description", response.text)
        if error == "invalid_grant":
            raise GA4ConfigError(
                "Google rejected the service account key (invalid_grant). "
                "The key may have been deleted in Google Cloud, or the "
                "computer's clock may be badly wrong. Create a fresh key "
                "and upload it again."
            )
        raise RuntimeError(f"Google token exchange failed: {error} {description}")
    return payload["access_token"], now + int(payload.get("expires_in", 3600))


def _get_token():
    """A valid access token for the stored key, cached per process."""
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 300:
        return _token
    raw = secrets_store.get_secret_bytes(SERVICE_ACCOUNT_SECRET)
    if raw is None:
        raise GA4ConfigError("GA4 isn't connected (no service account key).")
    info = parse_service_account_json(raw)
    _token, _token_expiry = _fetch_access_token(info)
    return _token


def _property_id():
    db.init_db()
    property_id = (db.get_setting(PROPERTY_ID_KEY) or "").strip()
    if not property_id:
        raise GA4ConfigError("GA4 isn't connected (no property id).")
    return property_id


def _run_report(body, token=None, property_id=None):
    """POST one runReport request; returns the parsed response dict."""
    token = token or _get_token()
    property_id = property_id or _property_id()
    response = requests.post(
        f"{DATA_API_BASE}/properties/{property_id}:runReport",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    connectivity.raise_for_rate_limit("GA4", response.status_code)
    payload = response.json() if response.content else {}
    if response.status_code != 200:
        error = payload.get("error", {})
        raise GA4APIError(
            response.status_code,
            error.get("status", ""),
            error.get("message", response.text),
        )
    return payload


class GA4APIError(RuntimeError):
    def __init__(self, http_status, status, message):
        self.http_status = http_status
        self.status = status
        super().__init__(f"GA4 API error ({status or http_status}): {message}")


def _two_ranges(days):
    """Current trailing window and the equal-length window before it."""
    return [
        {"startDate": f"{days - 1}daysAgo", "endDate": "today",
         "name": "current"},
        {"startDate": f"{2 * days - 1}daysAgo",
         "endDate": f"{days}daysAgo", "name": "prior"},
    ]


def _rows(payload):
    return payload.get("rows", [])


def _dim(row, index):
    values = row.get("dimensionValues", [])
    return values[index]["value"] if index < len(values) else ""


def _metric(row, index):
    values = row.get("metricValues", [])
    return int(values[index]["value"]) if index < len(values) else 0


def get_traffic_summary(days):
    """Users / sessions / page views for the trailing N days and the N
    days before that."""
    payload = _run_report({
        "dateRanges": _two_ranges(days),
        "metrics": [{"name": "totalUsers"}, {"name": "sessions"},
                    {"name": "screenPageViews"}],
    })
    summary = {
        "current": {"users": 0, "sessions": 0, "page_views": 0},
        "prior": {"users": 0, "sessions": 0, "page_views": 0},
    }
    for row in _rows(payload):
        # With two date ranges and no dimensions, each row's single
        # dimension value names the range.
        which = _dim(row, 0) or "current"
        if which not in summary:
            continue
        summary[which] = {
            "users": _metric(row, 0),
            "sessions": _metric(row, 1),
            "page_views": _metric(row, 2),
        }
    return summary


def get_event_counts(days, event_names):
    """eventCount per event name, current window vs the prior one.
    Returns {'current': {event: n}, 'prior': {event: n}}."""
    counts = {"current": {e: 0 for e in event_names},
              "prior": {e: 0 for e in event_names}}
    if not event_names:
        return counts
    payload = _run_report({
        "dateRanges": _two_ranges(days),
        "dimensions": [{"name": "eventName"}],
        "metrics": [{"name": "eventCount"}],
        "dimensionFilter": {"filter": {
            "fieldName": "eventName",
            "inListFilter": {"values": list(event_names)},
        }},
    })
    for row in _rows(payload):
        event = _dim(row, 0)
        which = _dim(row, 1) or "current"
        if which in counts and event in counts[which]:
            counts[which][event] = _metric(row, 0)
    return counts


def get_top_pages(days, limit=5):
    """Top pages by views for the trailing N days."""
    payload = _run_report({
        "dateRanges": [{"startDate": f"{days - 1}daysAgo",
                        "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"},
                      "desc": True}],
        "limit": str(limit),
    })
    return [{"path": _dim(row, 0), "views": _metric(row, 0)}
            for row in _rows(payload)]


def get_traffic_by_source(days, conversion_event=None):
    """Sessions per default channel group for the trailing N days, plus
    the count of one conversion event per channel (for a which-channel-
    converts-best view). Read-only, like every other report here."""
    date_range = [{"startDate": f"{days - 1}daysAgo", "endDate": "today"}]
    channels = {}
    sessions_payload = _run_report({
        "dateRanges": date_range,
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": "10",
    })
    for row in _rows(sessions_payload):
        channel = _dim(row, 0) or "(unassigned)"
        channels[channel] = {"sessions": _metric(row, 0), "conversions": 0}

    if conversion_event:
        conv_payload = _run_report({
            "dateRanges": date_range,
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "eventCount"}],
            "dimensionFilter": {"filter": {
                "fieldName": "eventName",
                "stringFilter": {"value": conversion_event},
            }},
        })
        for row in _rows(conv_payload):
            channel = _dim(row, 0) or "(unassigned)"
            channels.setdefault(channel, {"sessions": 0, "conversions": 0})
            channels[channel]["conversions"] = _metric(row, 0)

    return [{"channel": channel, "sessions": data["sessions"],
             "conversions": data["conversions"]}
            for channel, data in channels.items()]


def _primary_conversion_event(events):
    """The funnel-end event used for conversion-by-source: waitlist_joined
    if tracked, else the last configured event, else None."""
    if "waitlist_joined" in events:
        return "waitlist_joined"
    return events[-1] if events else None


def take_daily_snapshot():
    """Pull everything for 7- and 30-day windows and store it as one
    JSON snapshot. Called by the daily check; the Insights tab reads
    this instead of querying GA4 live."""
    db.init_db()
    events = conversion_events()
    primary = _primary_conversion_event(events)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conversion_events": events,
        "primary_conversion_event": primary,
        "windows": {},
    }
    for days in (7, 30):
        window = {
            "traffic": get_traffic_summary(days),
            "events": get_event_counts(days, events),
            "top_pages": get_top_pages(days),
        }
        # Channel breakdown on the 30-day window only (the Growth tab's
        # conversion-by-source view); cheap enough for a daily job.
        if days == 30:
            window["by_source"] = get_traffic_by_source(days, primary)
        snapshot["windows"][str(days)] = window
    db.save_ga4_snapshot(
        date=datetime.now(timezone.utc).date().isoformat(),
        snapshot_json=json.dumps(snapshot),
    )
    print("GA4 snapshot saved.")
    return snapshot


def validate_property_id_format(property_id):
    """None if it looks like a Property ID; otherwise a specific error.
    Catches the classic Measurement-ID mixup before any live call."""
    property_id = (property_id or "").strip()
    if not property_id:
        return "Property ID is empty. Paste it in (don't retype it)."
    if property_id.upper().startswith("G-"):
        return (
            "That's a Measurement ID (starts with G-), which belongs to "
            "a data stream. The Property ID is a plain number — in GA4: "
            "Admin → Property → Property details, top right corner."
        )
    if property_id.lower().startswith("properties/"):
        property_id = property_id.split("/", 1)[1]
    if not property_id.isdigit():
        return (
            "A Property ID is a plain number (usually 9–10 digits). "
            "In GA4: Admin → Property → Property details, top right "
            "corner."
        )
    return None


def test_connection(json_bytes, property_id):
    """Live check for the Setup wizard: validates the key file, gets a
    token, and runs a one-metric report against the property."""
    try:
        info = parse_service_account_json(json_bytes)
    except GA4ConfigError as exc:
        return connectivity.fail(str(exc))

    format_error = validate_property_id_format(property_id)
    if format_error:
        return connectivity.fail(format_error)
    property_id = property_id.strip().split("/")[-1]

    try:
        token, _ = _fetch_access_token(info)
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except GA4ConfigError as exc:
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("Google's token service", exc)

    try:
        _run_report(
            {"dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
             "metrics": [{"name": "sessions"}]},
            token=token, property_id=property_id,
        )
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except GA4APIError as exc:
        if exc.http_status == 403:
            return connectivity.fail(
                "The key works, but this service account has no access "
                f"to property {property_id}. In GA4: Admin → Property "
                "access management → Add users, and add "
                f"{info['client_email']} with the Viewer role."
            )
        if exc.http_status in (400, 404):
            return connectivity.fail(
                f"Google couldn't find property {property_id}. "
                "Double-check the Property ID — a plain number from "
                "Admin → Property details, NOT the G- Measurement ID."
            )
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("GA4", exc)

    return connectivity.ok(
        f"Connected. The service account can read property {property_id}."
    )
