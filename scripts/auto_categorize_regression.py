"""Offline regression checks for the two-tier category suggestions.

The suite uses an isolated database and mocks Ollama responses. It never
starts a server, calls Wave, or touches port 8642.
"""

import gc
import os
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-categorize-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import cli  # noqa: E402
import db  # noqa: E402
import ollama_client  # noqa: E402
from src import categorize  # noqa: E402


checks = 0
failures = 0


def check(name, condition, detail=""):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def run():
    db.init_db()

    print("Rule matching")
    category, confidence, source = categorize.categorize(
        "Amazon Web Services", 42, "expense", db)
    check("longer vendor pattern wins", category == "Software & Subscriptions")
    check("rule suggestion is high confidence", confidence == "high")
    check("rule suggestion records rule source", source == "rule")

    category, _confidence, _source = categorize.categorize(
        "Amazon Marketplace", 42, "expense", db)
    check("Amazon fallback pattern remains available", category == "Office Expense")

    print("Ollama fallback")
    original_available = ollama_client.is_available
    original_generate = ollama_client.generate_category
    try:
        ollama_client.is_available = lambda: False
        result = categorize.categorize("Unknown Vendor", 42, "expense", db)
        check("offline Ollama returns no suggestion", result == (None, None, "none"))

        ollama_client.is_available = lambda: True
        ollama_client.generate_category = lambda prompt, categories: (
            "Software & Subscriptions", "ok")
        result = categorize.categorize("Unknown Vendor", 42, "expense", db)
        check("Ollama result is medium confidence",
              result == ("Software & Subscriptions", "medium", "ollama"))

        ollama_client.generate_category = lambda prompt, categories: (
            "", "timeout")
        result = categorize.categorize("Another Unknown", 42, "expense", db)
        check("Ollama timeout is explicit", result == (None, None, "timeout"))
    finally:
        ollama_client.is_available = original_available
        ollama_client.generate_category = original_generate

    print("Corrections and persistence")
    db.record_category_correction("acme", "Professional Services")
    result = categorize.categorize("Acme, Inc.", 42, "expense", db)
    check("user correction outranks vendor rules",
          result == ("Professional Services", "high", "rule"))

    pending_id = db.insert_pending_item(
        "manual", "Persisted Vendor", 12, "2026-07-14", None,
        category_confidence=None, category_source=None)
    columns = {row["name"] for row in db.get_connection().execute(
        "PRAGMA table_info(pending_items)").fetchall()}
    check("pending schema has confidence column", "category_confidence" in columns)
    check("pending schema has source column", "category_source" in columns)
    check("corrections table exists",
          bool(db.get_connection().execute(
              "SELECT name FROM sqlite_master WHERE name = 'category_corrections'"
          ).fetchone()))

    db.set_category_suggestion(pending_id, "Office Supplies", "high", "rule")
    persisted = db.get_item(pending_id)
    check("suggestion fields persist", persisted["category_confidence"] == "high"
          and persisted["category_source"] == "rule")

    print("Queue integration and UI")
    direction, category, confidence, source = app.check_bank.categorize_pending(
        "Slack", 12, "expense")
    check("queue helper uses rule categorization",
          (direction, category, confidence, source)
          == ("expense", "Software & Subscriptions", "high", "rule"))

    class FixtureAdapter:
        id = "fixture-bank"

        def stored_credentials(self):
            return {"token": "test"}

        def fetch_transactions(self, credentials, since_date=None):
            return [{"external_id": "queue-fixture", "counterparty": "Slack",
                     "amount": -12.0, "date": "2026-07-14",
                     "account_id": "account-1"}]

        def list_accounts(self, credentials):
            return [{"id": "account-1", "name": "Checking"}]

    original_adapter = app.check_bank.banks.active_adapter
    original_queue_categorize = app.check_bank.categorize_pending
    app.check_bank.banks.active_adapter = lambda: FixtureAdapter()
    app.check_bank.categorize_pending = lambda *args: (
        "expense", "Office Supplies", "high", "rule")
    try:
        inserted = app.check_bank.run_check()
    finally:
        app.check_bank.banks.active_adapter = original_adapter
        app.check_bank.categorize_pending = original_queue_categorize
    check("bank sync inserts an auto-categorized item", len(inserted) == 1)
    queued = db.get_item(inserted[0]["id"])
    check("bank sync persists confidence/source",
          queued["category_confidence"] == "high"
          and queued["category_source"] == "rule")

    items = [
        {"id": 2, "vendor": "Rule Vendor", "amount": 10, "direction": "expense",
         "date": "2026-07-14", "source": "bank", "suggested_category": "Office Supplies",
         "category_confidence": "high", "category_source": "rule"},
        {"id": 3, "vendor": "AI Vendor", "amount": 10, "direction": "expense",
         "date": "2026-07-14", "source": "bank", "suggested_category": "Software",
         "category_confidence": "medium", "category_source": "ollama"},
        {"id": 4, "vendor": "Unknown Vendor", "amount": 10, "direction": "expense",
         "date": "2026-07-14", "source": "bank", "suggested_category": None,
         "category_confidence": None, "category_source": None},
    ]
    # Native Chart of Accounts shape: flat lowercase type, plus the code
    # the dropdown submits as each option's value.
    categories = [{"code": "6040", "name": "Office Supplies",
                   "type": "expense"},
                  {"code": "6030", "name": "Software", "type": "expense"}]
    html = app._review_transactions_content(items, categories=categories)
    check("high confidence category renders as confirmed",
          '<span class="tag ok">Office Supplies</span>' in html)
    check("medium confidence category renders as a question",
          '<span class="tag warn">Software?</span>' in html)
    check("medium suggestion is not selected", 'value="" selected' in html)
    check("no-suggestion state stays editable", html.count(
        'value="" selected>Select a category') >= 1)
    hidden_html = app._review_transactions_content(
        items, categories=categories, show_auto_suggestions=False)
    check("Settings toggle hides stored suggestions",
          "Office Supplies</span>" not in hidden_html
          and "Software?</span>" not in hidden_html)

    print("CLI recategorization")
    cli_item = db.insert_pending_item(
        "manual", "CLI Vendor", 8, "2026-07-14", None)
    original_categorize = cli.categorizer.categorize
    cli.categorizer.categorize = lambda vendor, amount, direction, db: (
        "Office Supplies", "high", "rule")
    try:
        total = cli.recategorize_pending()
    finally:
        cli.categorizer.categorize = original_categorize
    check("CLI processes only missing suggestions", total == 1)
    check("CLI fills the pending suggestion", db.get_item(cli_item)["suggested_category"]
          == "Office Supplies")
    check("CLI leaves item pending", db.get_item(cli_item)["status"] == "pending")

    gc.collect()
    if failures:
        print(f"FAIL: {failures} of {checks} auto-categorization checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} auto-categorization checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
