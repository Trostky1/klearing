"""Offline regression checks for the Slack and Notion integrations.

No server is started and no real webhook or Notion request is made. The
temporary data directory keeps settings and error logs away from a developer
profile while the adapter contracts are exercised.
"""

import os
import sys
import tempfile
import gc
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-integrations-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import notion_client  # noqa: E402
import slack_client  # noqa: E402


def check(name, condition, detail=""):
    global failures, checks
    checks += 1
    if condition:
        print(f"  PASS  {name}")
        return
    failures += 1
    print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def _close_data():
    return {
        "reconciliation": {
            "opening_balance": 1000,
            "closing_balance": 1200,
            "ledger_balance": 1200,
            "bank_balance": 1200,
            "residual": 0,
            "flagged": False,
            "checked": True,
            "closing_date": "2026-06-30",
        },
        "total_in": 500,
        "total_out": 300,
        "net": 200,
        "income_by_category": [("Sales", {"count": 2, "total": 500})],
        "expense_by_category": [("Software", {"count": 1, "total": 300})],
        "top_vendors": [{"vendor": "Hosting", "total": 300, "jump_pct": None}],
        "pending": [],
        "subscriptions": [],
        "invoice_aging": [],
        "no_documentation": [],
    }


def run():
    global failures, checks
    failures = 0
    checks = 0
    db.init_db()

    print("Slack adapter")
    check("Slack webhook format accepts incoming webhook",
          slack_client.validate_webhook_format(
              "https://hooks.slack.com/services/T/B/X") is None)
    check("Slack webhook format rejects non-webhook URL",
          slack_client.validate_webhook_format("https://example.test") is not None)

    sent = []
    slack_client.get_webhook = lambda: "https://hooks.slack.com/services/T/B/X"
    original_slack_adapter_post = slack_client._post
    slack_client._post = lambda webhook, text: sent.append((webhook, text)) or True
    db.set_setting(slack_client.DAILY_DIGEST_KEY, "1")
    db.set_setting(slack_client.INSTANT_ALERTS_KEY, "0")
    check("Slack digest is gated by the user toggle",
          slack_client.send_daily_digest("Check the cash buffer.") is True)
    check("Slack digest includes the footer",
          sent and sent[-1][1].endswith("via Klearing"))
    check("Slack digest includes no transaction row fields",
          "external_transaction_id" not in sent[-1][1]
          and "account_number" not in sent[-1][1])

    db.set_setting(slack_client.DAILY_DIGEST_KEY, "0")
    before = len(sent)
    check("Disabled Slack digest sends nothing",
          slack_client.send_daily_digest() is False and len(sent) == before)

    print("Notion adapter")
    check("Notion close blocks contain all close sections",
          all(any(block.get("type") == "heading_2" and
                  section in str(block) for block in notion_client.build_close_blocks(_close_data()))
              for section in ("Native Ledger Summary", "Reconciliation Status",
                              "Revenue by Account", "Expenses by Account",
                              "Top Vendors", "Pending Items (at close)",
                              "Subscriptions", "Invoice Aging", "Exceptions Log")))
    check("Notion close blocks include tables",
          any(block.get("type") == "table"
              for block in notion_client.build_close_blocks(_close_data())))
    check("Notion config requires both token and parent page",
          notion_client.validate_config("short", "") is not None)
    check("Notion config accepts a complete pair",
          notion_client.validate_config("ntn_" + "x" * 30, "parent-page") is None)

    print("Shared failure handling")
    class Response:
        status_code = 429
        text = "rate limited"
        content = b"{}"

    original_request = notion_client.requests.request
    notion_client.requests.request = lambda *args, **kwargs: Response()
    try:
        try:
            notion_client._request("token", "GET", "/pages/parent")
        except Exception as exc:
            rate_limited = isinstance(exc, notion_client.connectivity.RateLimitError)
        else:
            rate_limited = False
    finally:
        notion_client.requests.request = original_request
    check("Notion HTTP 429 becomes shared rate-limit error", rate_limited)

    original_post = slack_client.requests.post
    slack_client._post = original_slack_adapter_post
    slack_client.requests.post = lambda *args, **kwargs: Response()
    try:
        try:
            slack_client._post("https://hooks.slack.com/services/T/B/X", "test")
        except Exception as exc:
            rate_limited = isinstance(exc, slack_client.connectivity.RateLimitError)
        else:
            rate_limited = False
    finally:
        slack_client.requests.post = original_post
    check("Slack HTTP 429 becomes shared rate-limit error", rate_limited)

    gc.collect()

    if failures:
        print(f"FAIL: {failures} of {checks} integration checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} integration checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
