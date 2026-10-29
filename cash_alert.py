"""
Cash / runway alert for Klearing.

Runs as part of the daily check (check_bank.run_daily). It is
entirely opt-in: if the user hasn't set a dollar threshold or a runway
threshold in Settings, evaluate() returns None and nothing happens.

When at least one threshold is set, it:
  - sums the current balance across only the bank accounts the user
    marked as business cash in Settings,
  - computes the average daily burn from approved expense transactions
    over the trailing 30 days,
  - derives runway = balance / daily burn (infinite if burn <= 0),
  - and, if either configured threshold is currently breached, returns
    an alert that send_alert_email() turns into a warning email.

This check is independent of whether new bank transactions were
found, so the alert fires every day the condition remains true.
"""

import json
from datetime import date, timedelta

import banks
import paths
import db
import email_utils
from config import APP_NAME, APP_URL

# Settings keys (settings table). Empty / missing value means "unset".
BALANCE_THRESHOLD_KEY = "alert_balance_threshold"
RUNWAY_DAYS_KEY = "alert_runway_days"
CASH_ACCOUNTS_KEY = "cash_accounts"

BURN_WINDOW_DAYS = 30


def _parse_positive_float(value):
    """A setting parsed as a positive number, or None if unset/blank/invalid."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def get_config():
    """Read the alert configuration from the settings table."""
    raw_accounts = db.get_setting(CASH_ACCOUNTS_KEY)
    try:
        cash_account_ids = json.loads(raw_accounts) if raw_accounts else []
    except (ValueError, TypeError):
        cash_account_ids = []
    return {
        "balance_threshold": _parse_positive_float(db.get_setting(BALANCE_THRESHOLD_KEY)),
        "runway_days": _parse_positive_float(db.get_setting(RUNWAY_DAYS_KEY)),
        "cash_account_ids": cash_account_ids,
    }


def _demo_cash_accounts():
    """The demo has no bank, so its "bank" balance is the books' own cash."""
    import ledger
    balance = next((row["normal_balance"] for row in ledger.trial_balance()
                    if row["code"] == "1010"), 0)
    return [{"id": "demo-checking", "name": "Operating Checking",
             "balance": float(balance)}]


def matched_cash_accounts(cash_account_ids):
    """The selected business-cash accounts themselves
    ([{'id', 'name', 'balance'}]) — Reconcile's per-account breakdown
    needs the rows, not just their sum."""
    if paths.is_demo():
        return _demo_cash_accounts()
    adapter = banks.active_adapter()
    if adapter is None:
        raise RuntimeError("No bank is connected, so balances can't be read.")
    credentials = adapter.stored_credentials()
    if credentials is None:
        raise RuntimeError(
            f"{adapter.display_name} credentials are missing — re-connect "
            f"it on the Connections page."
        )
    selected = set(cash_account_ids)
    accounts = adapter.list_accounts(credentials)
    matched = [a for a in accounts if a["id"] in selected]
    if selected and not matched:
        # The saved selection belongs to a different bank (the user
        # switched adapters). A silent $0 here would fire a false
        # low-cash alarm every day — fail loudly instead.
        raise RuntimeError(
            "None of the selected business-cash accounts exist at "
            f"{adapter.display_name} — re-pick them in Settings."
        )
    return matched


def current_cash_balance(cash_account_ids):
    """Sum balances across only the selected bank accounts, via the
    active bank adapter."""
    return sum(a["balance"]
               for a in matched_cash_accounts(cash_account_ids))


def average_daily_burn():
    """Expenses over the trailing 30 days, divided by 30. Approved AND
    still-pending items both count: the money left the bank either way,
    and excluding an approval backlog would overstate runway exactly
    when the user isn't looking."""
    cutoff = (date.today() - timedelta(days=BURN_WINDOW_DAYS)).isoformat()
    total = sum(
        item["amount"]
        for item in db.list_items()
        if item["direction"] == "expense"
        and item["status"] in ("approved", "pending")
        and item["date"] >= cutoff
    )
    return total / BURN_WINDOW_DAYS


def evaluate():
    """
    Return an alert dict if a configured threshold is currently
    breached, otherwise None. Returns None immediately (no bank
    call) if neither threshold is set — the feature is inactive until
    the user configures it.
    """
    db.init_db()
    config = get_config()
    if config["balance_threshold"] is None and config["runway_days"] is None:
        return None  # feature not configured — silently inactive

    balance = current_cash_balance(config["cash_account_ids"])
    burn = average_daily_burn()
    runway_days = balance / burn if burn > 0 else None  # None = infinite

    breaches = []
    if (config["balance_threshold"] is not None
            and balance < config["balance_threshold"]):
        breaches.append("balance")
    if (config["runway_days"] is not None
            and runway_days is not None
            and runway_days < config["runway_days"]):
        breaches.append("runway")

    if not breaches:
        return None

    return {
        "balance": balance,
        "burn": burn,
        "runway_days": runway_days,
        "breaches": breaches,
        "balance_threshold": config["balance_threshold"],
        "runway_threshold": config["runway_days"],
    }


def format_alert_section(alert):
    """
    Plain-language body describing the breached threshold(s), without a
    footer — used standalone (with a footer appended) or as one section
    of a combined transactions + alert email.
    """
    runway = alert["runway_days"]
    runway_text = (
        f"{runway:.1f} days" if runway is not None
        else "effectively unlimited (no expenses in the last 30 days)"
    )

    lines = [
        "Heads up: your business cash is running low.",
        "",
        f"Current cash balance: ${alert['balance']:,.2f}",
        f"Average daily spend (last {BURN_WINDOW_DAYS} days): ${alert['burn']:,.2f}",
        f"Estimated runway: {runway_text}",
        "",
        "Threshold(s) breached:",
    ]
    if "balance" in alert["breaches"]:
        lines.append(
            f"  - Balance ${alert['balance']:,.2f} is below your "
            f"${alert['balance_threshold']:,.2f} alert level."
        )
    if "runway" in alert["breaches"]:
        lines.append(
            f"  - Runway {runway:.1f} days is below your "
            f"{alert['runway_threshold']:.0f}-day alert level."
        )
    return "\n".join(lines)


def format_alert_body(alert):
    """Standalone alert email body (section + review footer)."""
    return format_alert_section(alert) + f"\n\nReview at {APP_URL}"


def send_alert_email(alert):
    """Email the low-cash warning. Returns True if an email was sent."""
    return email_utils.send_email(
        f"⚠️ {APP_NAME}: Low Cash Alert",
        format_alert_body(alert),
    )
