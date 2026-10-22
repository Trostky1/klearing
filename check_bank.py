"""
Pull new bank transactions into the pending-items queue, via whichever
bank adapter was chosen in Setup (banks/).

What it does on each run:
1. Asks the active bank adapter for transactions since the last check
   (timestamp stored per adapter in the meta table; first run fetches
   everything the bank returns).
2. Inserts each new transaction as a pending item, deduped by the
   bank's transaction id. Money-out becomes an "expense" item, money-in
   an "income" item — unless the counterparty name looks like a
   personal/savings account (see TRANSFER_KEYWORDS), in which case it
   becomes a "transfer" item suggesting the owner-contribution equity
   account, so personal top-ups never get counted as revenue.
3. Suggests a category: learned vendor memory first (fuzzy match on
   vendors the user has already approved), then the keyword maps
   below, then Uncategorized. Correcting the dropdown before approving
   is how the suggestions get smarter.
4. If new items were found, sends a plain-text email summary (skipped
   quietly when email isn't configured).

Nothing here posts to the ledger. Approval happens in the web app (app.py).

run_daily() is the scheduled entry point: every step runs
independently, failures are logged and consolidated into one alert
email, and a fully clean run stamps the heartbeat shown in Settings.
"""

import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from rapidfuzz import fuzz, process

import banks
import business_registry
import db
from config import APP_NAME, APP_URL
from email_utils import send_email  # shared SMTP; re-exported for callers
from src import categorize as categorizer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Keyword → native account, matched case-insensitively as a substring of
# the counterparty name. Deliberately empty by default: account names
# must match the local Chart of Accounts, and Klearing learns them from
# your approvals (vendor_memory) instead of guessing from a list built
# for someone else's business. Add entries here only if you fork.
EXPENSE_CATEGORY_KEYWORDS = {}
FALLBACK_EXPENSE_CATEGORY = "Uncategorized Expense"

INCOME_CATEGORY_KEYWORDS = {}
FALLBACK_INCOME_CATEGORY = "Uncategorized Income"

# Deposits whose counterparty name matches one of these look like the
# owner moving personal money into the business, not real revenue.
# The transfer suggestion is a local equity account for owner contributions;
# override it in the settings table if the business uses another name.
TRANSFER_KEYWORDS = ("savings", "personal")
DEFAULT_TRANSFER_ACCOUNT_NAME = "Owner Investment / Drawings"
TRANSFER_ACCOUNT_KEY = "transfer_account_name"

LAST_CHECKED_KEY_PREFIX = "bank_last_checked."
LAST_SUCCESS_KEY = "last_successful_run"


def _bank_amount_to_cents(value):
    """Parse one adapter amount exactly; bank inputs may never be rounded."""
    try:
        amount = Decimal(str(value))
        rounded = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Bank returned an invalid transaction amount.") from exc
    if not amount.is_finite() or amount != rounded:
        raise RuntimeError(
            "Bank returned a transaction amount with sub-cent precision.")
    return int(rounded * 100)


def _normalize_bank_transaction(transaction):
    """Replace an adapter's raw numeric amount at the ingestion boundary."""
    normalized = dict(transaction)
    cents = _bank_amount_to_cents(normalized.get("amount"))
    normalized["amount_cents"] = cents
    normalized["amount"] = Decimal(cents) / 100
    return normalized


def _cache_bank_rows(adapter, transactions, credentials, synced_at):
    """Persist adapter-normalized rows once for every local consumer."""
    accounts = []
    try:
        accounts = adapter.list_accounts(credentials)
    except Exception as exc:
        print(f"Bank account metadata warning: {exc}")
    for account in accounts:
        account_id = str(account.get("id") or "").strip()
        if account_id:
            db.upsert_bank_account({
                "source": adapter.id, "account_id": account_id,
                "name": account.get("name") or "", "official_name": None,
                # Generic adapters do not promise asset/liability metadata.
                "type": "", "subtype": "", "mask": None,
                "synced_at": synced_at,
            })
    for txn in transactions:
        db.upsert_bank_transaction({
            "source": adapter.id,
            "transaction_id": str(txn["external_id"]),
            "account_id": str(txn.get("account_id") or ""),
            "date": txn["date"], "authorized_date": None,
            "amount_cents": txn["amount_cents"],
            "merchant_name": txn.get("counterparty") or "",
            "name": txn.get("counterparty") or "", "pending": 0,
            "category_primary": "", "category_detailed": "", "removed": 0,
            "synced_at": synced_at,
        })
    return accounts


def transfer_account_name():
    db.init_db()
    stored = db.get_setting(TRANSFER_ACCOUNT_KEY)
    return (stored or "").strip() or DEFAULT_TRANSFER_ACCOUNT_NAME


def is_owner_transfer(vendor):
    """Whole-word match only — a customer named "Personal Training by
    Alex" is revenue, not an owner transfer. Even when this guesses
    wrong, approve() re-derives the final direction from the category
    the user actually picks, so the suggestion is never binding."""
    vendor_lower = vendor.lower()
    return any(re.search(rf"\b{re.escape(keyword)}\b", vendor_lower)
               for keyword in TRANSFER_KEYWORDS)


def signed_amount(item):
    sign = "-" if item["direction"] == "expense" else "+"
    return f"{sign}${item['amount']:,.2f}"


# Minimum rapidfuzz similarity (0-100) for a learned vendor match.
MEMORY_MATCH_THRESHOLD = 85


def suggest_from_memory(vendor, direction):
    """
    Fuzzy-match this vendor against vendors whose categories the user
    has already confirmed via Approve (vendor_memory table), for the
    same money direction and the ACTIVE business — an expense category
    learned from purchases must never be suggested for a deposit, and
    category names belong to one business's chart of accounts. Returns
    the learned category name, or None if nothing scores above the
    threshold. Pure local string matching — no API calls.
    """
    active = business_registry.active_business()
    rows = db.get_vendor_memory(
        direction=direction,
        business_id=active["id"] if active else "",
    )
    if not rows:
        return None
    by_name = {row["vendor_name"]: row for row in rows}
    match = process.extractOne(
        vendor, list(by_name), scorer=fuzz.WRatio,
        score_cutoff=MEMORY_MATCH_THRESHOLD,
    )
    if match is None:
        return None
    return by_name[match[0]]["category_name"]


def suggest_category(vendor, direction):
    # Learned memory first — a category the user actually confirmed
    # beats a static keyword guess.
    learned = suggest_from_memory(vendor, direction)
    if learned:
        return learned

    vendor_lower = vendor.lower()
    keywords = EXPENSE_CATEGORY_KEYWORDS if direction == "expense" else INCOME_CATEGORY_KEYWORDS
    fallback = FALLBACK_EXPENSE_CATEGORY if direction == "expense" else FALLBACK_INCOME_CATEGORY
    for keyword, category in keywords.items():
        if keyword in vendor_lower:
            return category
    return fallback


def classify(vendor, direction):
    """
    Decide (direction, suggested_category) for a transaction.

    Priority order:
    1. Owner-transfer detection (most specific rule — a deposit from a
       personal/savings account is never revenue, regardless of what
       vendor memory or keywords say)
    2. Learned vendor memory (fuzzy match, inside suggest_category)
    3. Static keyword list
    4. Uncategorized fallback
    """
    if direction == "income" and is_owner_transfer(vendor):
        return "transfer", transfer_account_name()
    return direction, suggest_category(vendor, direction)


def categorize_pending(vendor, amount, direction):
    """Classify a new queue item without ever auto-approving it."""
    final_direction, legacy_category = classify(vendor, direction)
    if final_direction == "transfer":
        return final_direction, legacy_category, "high", "rule"
    category, confidence, source = categorizer.categorize(
        vendor, amount, final_direction, db
    )
    return final_direction, category, confidence, source


def run_check():
    """
    Fetch new transactions from the active bank adapter and insert
    them as pending items. Returns the list of newly inserted items
    (dicts). Safe to call repeatedly — dedup is by the bank's
    transaction id.
    """
    db.init_db()
    adapter = banks.active_adapter()
    if adapter is None:
        raise RuntimeError(
            "No bank is connected yet. Finish Setup (or open Connections) "
            "to connect one."
        )
    credentials = adapter.stored_credentials()
    if credentials is None:
        raise RuntimeError(
            f"{adapter.display_name} is selected but its credentials are "
            f"missing. Re-connect it on the Connections page."
        )

    last_checked_key = LAST_CHECKED_KEY_PREFIX + adapter.id
    last_checked = db.get_meta(last_checked_key)
    since_date = last_checked[:10] if last_checked else None

    transactions = [
        _normalize_bank_transaction(transaction)
        for transaction in adapter.fetch_transactions(
            credentials, since_date=since_date)
    ]
    synced_at = datetime.now(timezone.utc).isoformat()
    accounts = _cache_bank_rows(adapter, transactions, credentials, synced_at)
    new_items = []
    for txn in transactions:
        if db.external_id_exists(adapter.id, txn["external_id"]):
            continue

        direction = "expense" if txn["amount"] < 0 else "income"
        vendor = txn["counterparty"]
        direction, category, confidence, category_source = categorize_pending(
            vendor, abs(txn["amount"]), direction
        )

        item = {
            "source": adapter.id,
            "vendor": vendor,
            "amount": abs(txn["amount"]),
            "date": txn["date"],
            "direction": direction,
            "suggested_category": category,
            "external_transaction_id": txn["external_id"],
            "account_id": txn.get("account_id"),
            "category_confidence": confidence if category else None,
            "category_source": category_source if category else None,
        }
        item["id"] = db.insert_pending_item(**item)
        new_items.append(item)

    account_ids = {item.get("account_id") for item in new_items
                   if item.get("account_id")}
    account_ids.update(str(account["id"]) for account in accounts)
    # Keep the health baseline in the existing settings table. This is
    # intentionally best-effort: a sync that returns no new rows still
    # records that the connection succeeded.
    for account_id in account_ids:
        prefix = f"data_health_{adapter.id}_{account_id}_"
        items = [item for item in db.list_items()
                 if item.get("source") in (adapter.id, "manual")
                 and item.get("account_id") == account_id]
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        prior = month[:5] + f"{int(month[5:]) - 1:02d}" if month[5:] != "01" else f"{int(month[:4]) - 1:04d}-12"
        current_count = sum(1 for item in items if item["date"].startswith(month))
        prior_count = sum(1 for item in items if item["date"].startswith(prior))
        expected = prior_count or current_count
        db.set_setting(prefix + "last_sync_date", synced_at)
        db.set_setting(prefix + "last_transaction_date",
                       max((item["date"] for item in items), default=""))
        db.set_setting(prefix + "expected_monthly_count", str(expected))
    db.set_meta(last_checked_key, synced_at)
    return new_items


REVIEW_FOOTER = f"Review at {APP_URL}"


def transactions_section(new_items):
    """The new-transactions list body, without a footer, so it can be
    used standalone or as one section of a combined email."""
    lines = [f"{len(new_items)} new transaction(s) waiting for approval:", ""]
    for item in new_items:
        lines.append(
            f"  - {item['date']}  {item['vendor']}  {signed_amount(item)}"
            f"  (suggested: {item.get('suggested_category') or 'none'})"
        )
    return "\n".join(lines)


def send_notification(new_items):
    """Email a plain-text summary of new pending items. Returns True
    if an email was sent."""
    body = transactions_section(new_items) + "\n\n" + REVIEW_FOOTER
    return send_email(
        f"[{APP_NAME}] {len(new_items)} new transaction(s) to review",
        body,
    )


def send_combined_notification(new_items, alert):
    """One email covering both new transactions and a breached cash
    alert (the both-at-once case). Returns True if an email was sent."""
    import cash_alert
    body = (
        "NEW TRANSACTIONS\n"
        "----------------\n"
        + transactions_section(new_items)
        + "\n\n"
        "LOW CASH ALERT\n"
        "--------------\n"
        + cash_alert.format_alert_section(alert)
        + "\n\n"
        + REVIEW_FOOTER
    )
    return send_email(
        f"⚠️ {APP_NAME}: New Transactions + Low Cash Alert",
        body,
    )


def run_daily() -> int:
    """
    The daily runner the scheduled task invokes. Every step runs
    inside error_alerts.run_step(): a failure in one step is logged to
    error.log and collected, but the remaining independent steps still
    run. At the end, one consolidated failure email goes out if
    anything broke; a fully clean run stamps the heartbeat
    (last_successful_run in the meta table, shown on Settings).
    """
    # Imported lazily to avoid circular imports (these import this module).
    import cash_alert
    import check_overdue_quotes
    import check_unpaid_invoices
    import error_alerts
    import ga4_client

    failures = []

    def step_ok(step_name):
        """True when no failure was recorded for this step —
        used to stamp the Connections page's "last tested" line."""
        return not any(name == step_name for name, _ in failures)

    new_items = error_alerts.run_step(
        "Bank transaction fetch", run_check, failures, default=[])
    if step_ok("Bank transaction fetch"):
        db.mark_connection_tested("bank")
    else:
        db.mark_connection_tested(
            "bank", ok=False,
            detail="The daily check couldn't fetch bank transactions.")

    if new_items:
        print(f"Found {len(new_items)} new transaction(s):")
        for item in new_items:
            print(f"  - {item['date']}  {item['vendor']}  {signed_amount(item)}"
                  f"  (suggested: {item.get('suggested_category') or 'none'})")
    else:
        print("No new transactions found.")

    # The cash alert runs every day the condition holds, whether or not
    # new transactions were found (and even when the fetch step failed,
    # since it reads balances with its own bank call).
    alert = error_alerts.run_step(
        "Cash alert", cash_alert.evaluate, failures, default=None)
    if alert is None:
        print("Cash alert: not configured, no threshold breached, or check failed.")

    def send_emails():
        if new_items and alert:
            print("Both fired: sending one combined email.")
            send_combined_notification(new_items, alert)
        elif new_items:
            send_notification(new_items)
        elif alert:
            print("Cash alert: threshold breached, sending alert email.")
            cash_alert.send_alert_email(alert)

    error_alerts.run_step("Email notifications", send_emails, failures)

    # Read-only drafting passes; each independent of everything above.
    error_alerts.run_step(
        "Quote follow-ups", check_overdue_quotes.run_followup_check, failures)
    error_alerts.run_step(
        "Invoice reminders", check_unpaid_invoices.run_invoice_check, failures)

    # Recurring invoices that have come due. This queues them for Review
    # and posts nothing — approval is a person's job, exactly as it is
    # for a manually authored invoice. Deliberately reusing this daily
    # job rather than adding a poller: recurrence is expressed in whole
    # days, so nothing finer than a daily check could ever find work a
    # daily one would miss. The app-launch check (main.py) covers the
    # case where the machine was off when this was due to run.
    def recurring_invoice_check():
        import recurring_invoices
        summary = recurring_invoices.run_due_checks()
        if summary["queued"]:
            print(f"Recurring invoices: queued {summary['queued']} for "
                  f"review across {summary['templates']} template(s).")
        else:
            print("Recurring invoices: nothing due.")
        for skipped in summary["skipped_templates"]:
            print(f"  - skipped '{skipped['name']}': {skipped['reason']}")

    error_alerts.run_step(
        "Recurring invoices", recurring_invoice_check, failures)

    # GA4 snapshot for the Insights tab, only when GA4 was connected —
    # it is optional, and skipping must not read as a failure.
    if ga4_client.is_configured():
        error_alerts.run_step(
            "GA4 snapshot", ga4_client.take_daily_snapshot, failures)
        if step_ok("GA4 snapshot"):
            db.mark_connection_tested("ga4")
        else:
            db.mark_connection_tested(
                "ga4", ok=False,
                detail="The daily GA4 snapshot failed.")
    else:
        print("GA4 snapshot: not configured — skipping.")

    # Reconciliation snapshot for the Reconcile page's residual history
    # — only when everything it compares is configured (a
    # half-configured install must not alert about it every day).
    def reconcile_snapshot():
        # Lazy import: app imports this module at load; importing app
        # back at call time is safe, at module load it would be a cycle.
        import app
        app.record_reconcile_snapshot()

    if (banks.active_adapter() is not None
            and cash_alert.get_config()["cash_account_ids"]):
        error_alerts.run_step(
            "Reconciliation snapshot", reconcile_snapshot, failures)
    else:
        print("Reconciliation snapshot: bank or cash accounts "
              "not configured — skipping.")

    # Daily AI insight for the Home banner — optional like GA4, and
    # generate_daily_insight() never raises (offline Ollama returns
    # None), so a missing model can't fail the run or send alerts.
    import notion_client
    import ollama_client
    import shopify_client
    import slack_client
    import stripe_client
    insight = None
    if ollama_client.is_available():
        insight = ollama_client.generate_daily_insight()
        print("Daily insight: stored." if insight
              else "Daily insight: no insight produced.")
    else:
        print("Daily insight: Ollama not running — skipping.")

    # Optional outbound integrations are separate final steps. A webhook or
    # Notion outage must never change the result of the bookkeeping checks.
    if slack_client.is_configured():
        error_alerts.run_step(
            "Slack notifications",
            lambda: slack_client.run_daily_notifications(insight), failures)
        db.mark_connection_tested("slack")
    else:
        print("Slack notifications: not configured - skipping.")

    if notion_client.is_configured():
        error_alerts.run_step(
            "Notion snapshot", notion_client.update_running_snapshot, failures)
        db.mark_connection_tested("notion")
    else:
        print("Notion snapshot: not configured - skipping.")

    if shopify_client.is_configured():
        error_alerts.run_step("Shopify sync", shopify_client.sync, failures)
        if step_ok("Shopify sync"):
            db.mark_connection_tested("shopify")
            # Feature A notification: this is deliberately after the one
            # provider refresh included in the scheduled run. Plaid remains
            # Home-only and must never be polled here.
            try:
                import briefing_notifications
                import briefing_signals
                briefing_notifications.send_briefing_notification(
                    briefing_signals.get_toast_briefing_signals())
            except Exception as exc:  # notification must not fail the books
                print(f"Briefing notification failed: {exc}")
        else:
            db.mark_connection_tested("shopify", ok=False,
                                      detail="The daily Shopify sync failed; it will retry next run.")
    else:
        print("Shopify sync: not configured - skipping.")

    # Poll Stripe for online invoice payments. This is the only network half
    # of invoice reconciliation; the bank cross-match runs locally inside
    # the Ledger sync. It proposes, it never posts -- the money is applied
    # by a click in the Review drawer.
    if stripe_client.is_configured():
        import invoice_reconcile
        error_alerts.run_step("Invoice payment poll",
                              invoice_reconcile.poll_stripe, failures)
    else:
        print("Invoice payment poll: Stripe not connected - skipping.")

    if failures:
        error_alerts.notify_failures(failures, context="Daily check")
        return 1

    db.set_meta(LAST_SUCCESS_KEY, datetime.now(timezone.utc).date().isoformat())
    print("Daily check completed cleanly; heartbeat updated.")
    return 0


if __name__ == "__main__":
    sys.exit(run_daily())
