"""Small, opt-in Slack incoming-webhook adapter.

Slack is intentionally webhook-only here. The webhook is encrypted by the
same credential store as the other integrations; no OAuth flow or bot token
is needed. Messages contain summaries and alerts, never transaction rows.
"""

import calendar
import json
from datetime import date, timedelta

import requests

import connectivity
import db
import secrets_store

SECRET_NAME = "klearing_slack_webhook"
DAILY_DIGEST_KEY = "slack_daily_digest_enabled"
INSTANT_ALERTS_KEY = "slack_instant_alerts_enabled"
RECON_ALERT_KEY = "slack_last_reconcile_alert"
INVOICE_ALERTS_KEY = "slack_invoice_alerts"
SUBSCRIPTIONS_KEY = "slack_seen_subscriptions"
RUNWAY_ALERT_KEY = "slack_runway_below_30"
DOCS_URL = "https://api.slack.com/messaging/webhooks"
WEBHOOK_PREFIX = "https://hooks.slack.com/services/"
FOOTER = "via Klearing"


class SlackHTTPError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"Slack returned HTTP {status}: {body[:300]}")


def get_webhook():
    return secrets_store.get_secret(SECRET_NAME)


def is_configured():
    try:
        return bool(get_webhook())
    except Exception:
        return False


def validate_webhook_format(webhook):
    webhook = (webhook or "").strip()
    if not webhook:
        return "Slack webhook URL is empty. Paste the incoming webhook URL."
    if not webhook.startswith(WEBHOOK_PREFIX):
        return ("That does not look like a Slack incoming webhook URL. "
                "Paste the URL beginning with "
                "https://hooks.slack.com/services/.")
    return None


def _enabled(key):
    return db.get_setting(key) == "1"


def daily_digest_enabled():
    return _enabled(DAILY_DIGEST_KEY)


def instant_alerts_enabled():
    return _enabled(INSTANT_ALERTS_KEY)


def set_preferences(daily_digest, instant_alerts):
    db.set_setting(DAILY_DIGEST_KEY, "1" if daily_digest else "0")
    db.set_setting(INSTANT_ALERTS_KEY, "1" if instant_alerts else "0")


def _post(webhook, text):
    response = requests.post(webhook, json={"text": text}, timeout=20)
    connectivity.raise_for_rate_limit("Slack", response.status_code)
    if response.status_code < 200 or response.status_code >= 300:
        raise SlackHTTPError(response.status_code, response.text)
    return True


def _message(lines):
    clean = "\n".join(str(line).strip() for line in lines if str(line).strip())
    if not clean.endswith(FOOTER):
        clean += f"\n\n{FOOTER}"
    return clean[:3500]


def test_connection(webhook):
    """Send a real, clearly labelled test message to the webhook."""
    format_error = validate_webhook_format(webhook)
    if format_error:
        return connectivity.fail(format_error)
    try:
        _post(webhook.strip(), _message(["Klearing Slack connection test."]))
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except SlackHTTPError as exc:
        return connectivity.fail(
            "Slack could not accept the test message. Check the webhook "
            "on the Connections page and try again.",
            hint=f"Slack webhook docs: {DOCS_URL}",
        )
    except Exception as exc:
        return connectivity.from_exception("Slack", exc)
    return connectivity.ok("Slack connected. The test message was sent.")


def _latest_reconcile():
    try:
        rows = db.list_reconcile_history(days=30)
    except Exception:
        return None, None
    if not rows:
        return None, None
    return rows[-1], rows[-2] if len(rows) > 1 else None


def _money(value):
    return f"${abs(value):,.2f}" if value is not None else "unavailable"


def _runway():
    current, _ = _latest_reconcile()
    if not current:
        return None
    try:
        import cash_alert
        burn = cash_alert.average_daily_burn()
        return current["bank_balance"] / burn if burn > 0 else None
    except Exception:
        return None


def _vendor_jumps():
    """Return only vendors with a meaningful month-over-month jump."""
    today = date.today()
    start = today.replace(day=1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    previous_day = min(today.day, calendar.monthrange(
        previous_start.year, previous_start.month)[1])
    previous_cutoff = previous_start.replace(day=previous_day)
    current = {}
    previous = {}
    for item in db.list_items(status="approved"):
        if item.get("direction") != "expense":
            continue
        try:
            when = date.fromisoformat(item["date"][:10])
        except (ValueError, TypeError, KeyError):
            continue
        bucket = current if start <= when <= today else previous
        if previous_start <= when <= previous_cutoff:
            bucket[item["vendor"]] = bucket.get(item["vendor"], 0) + item["amount"]
    jumps = []
    for vendor, total in current.items():
        prior = previous.get(vendor, 0)
        if prior > 0 and total > prior * 1.3:
            jumps.append((total / prior - 1, vendor))
    return sorted(jumps, reverse=True)[:5]


def _daily_message(insight=None):
    current, _ = _latest_reconcile()
    if current and current["flagged"]:
        reconciliation = f"flagged ({_money(current['residual'])} residual)"
    elif current:
        reconciliation = "clean"
    else:
        reconciliation = "not checked"

    pending = db.list_items(status="pending")
    pending_total = sum(item.get("amount", 0) for item in pending)
    runway = _runway()
    runway_text = f"{runway:.0f} days" if runway is not None else "unavailable"
    jumps = _vendor_jumps()
    jump_text = (", ".join(f"{vendor} +{pct * 100:.0f}%" for pct, vendor in jumps)
                 if jumps else "none detected")
    lines = [
        "Daily digest",
        f"Reconciliation: {reconciliation}",
        f"Cash runway: {runway_text}",
        f"Pending items: {len(pending)} ({_money(pending_total)} total)",
        f"Vendor spend jumps: {jump_text}",
    ]
    if insight:
        lines.append(f"Daily AI insight: {str(insight).strip()[:1200]}")
    return _message(lines)


def send_daily_digest(insight=None):
    if not is_configured() or not daily_digest_enabled():
        return False
    _post(get_webhook(), _daily_message(insight))
    return True


def _load_json_setting(key, default):
    try:
        value = json.loads(db.get_setting(key) or "")
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError):
        return default


def _save_json_setting(key, value):
    db.set_setting(key, json.dumps(value))


def _instant_events():
    events = []
    current, previous = _latest_reconcile()
    if (current and current["flagged"] and not (previous and previous["flagged"])
            and db.get_setting(RECON_ALERT_KEY) != current["date"]):
        events.append((
            "reconcile",
            ["Reconciliation newly flagged.",
             f"Residual: {_money(current['residual'])}.",
             "Review the Reconcile page."]
        ))

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    invoice_seen = _load_json_setting(INVOICE_ALERTS_KEY, [])
    for invoice in db.list_invoice_reminders():
        key = f"{invoice['id']}:{invoice['due_date']}"
        if (invoice.get("due_date") == yesterday
                and invoice.get("status") not in ("paid", "skipped")
                and key not in invoice_seen):
            events.append((
                ("invoice", key),
                ["Invoice overdue today.",
                 f"Customer: {invoice.get('customer_name') or 'customer'}.",
                 f"Amount due: {invoice.get('amount_due') or 'unavailable'}."]
            ))

    subscription_seen = _load_json_setting(SUBSCRIPTIONS_KEY, [])
    try:
        import recurring
        subscriptions = recurring.detect_subscriptions()
    except Exception:
        subscriptions = []
    for subscription in subscriptions:
        vendor = str(subscription.get("vendor") or "vendor")
        if vendor not in subscription_seen:
            events.append((
                ("subscription", vendor),
                ["New subscription detected.",
                 f"Vendor: {vendor}.",
                 f"Typical amount: {_money(subscription.get('typical_amount'))}."]
            ))

    runway = _runway()
    was_below = db.get_setting(RUNWAY_ALERT_KEY) == "1"
    if runway is not None and runway < 30 and not was_below:
        events.append((
            "runway",
            ["Cash runway dropped below 30 days.",
             f"Current runway: {runway:.0f} days."]
        ))
    return events, current, invoice_seen, subscription_seen, runway


def send_instant_alerts():
    if not is_configured() or not instant_alerts_enabled():
        return False
    events, current, invoice_seen, subscription_seen, runway = _instant_events()
    webhook = get_webhook()
    for event_key, lines in events:
        _post(webhook, _message(lines))
        if event_key == "reconcile":
            db.set_setting(RECON_ALERT_KEY, current["date"])
        elif isinstance(event_key, tuple) and event_key[0] == "invoice":
            invoice_seen.append(event_key[1])
            _save_json_setting(INVOICE_ALERTS_KEY, invoice_seen)
        elif isinstance(event_key, tuple) and event_key[0] == "subscription":
            subscription_seen.append(event_key[1])
            _save_json_setting(SUBSCRIPTIONS_KEY, subscription_seen)
    if runway is not None:
        db.set_setting(RUNWAY_ALERT_KEY, "1" if runway < 30 else "0")
    return bool(events)


def run_daily_notifications(insight=None):
    """Send enabled messages after the local daily calculations complete."""
    send_daily_digest(insight)
    send_instant_alerts()
    return True
