"""Phase 4 proof that native books no longer depend on Wave read paths.

Runs against an isolated database. Conflicting legacy Wave cache and
reconciliation values are seeded deliberately: the checks only pass if the
categorizer and Accountant Export use the native Chart of Accounts and
ledger reports, and setup can finish without any Wave configuration.
"""

import asyncio
import csv
import io
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-wave-read-paths-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import accountant_export  # noqa: E402
import app  # noqa: E402
import app_lock  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import ollama_client  # noqa: E402
import setup_wizard  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from src import categorize  # noqa: E402


checks = 0
failures = 0


def check(label, condition, detail=""):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}" + (f" - {detail}" if detail else ""))


def _entry(entry_date, memo, debit_code, credit_code, amount, reference):
    debit = ledger.account_by_code(debit_code)
    credit = ledger.account_by_code(credit_code)
    return ledger.create_sourced_journal_entry(
        entry_date, memo,
        [
            {"account_id": debit["id"], "debit_amount": amount,
             "credit_amount": "0"},
            {"account_id": credit["id"], "debit_amount": "0",
             "credit_amount": amount},
        ],
        source_reference=reference, created_by="phase-4-regression",
    )


def _dashboard_passes_gate():
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [],
        "server": ("127.0.0.1", 18644), "client": ("127.0.0.1", 50000),
    }

    async def call_next(_request):
        return Response("dashboard", status_code=200)

    return asyncio.run(app.gatekeeper(Request(scope), call_next))


def run():
    db.init_db()

    print("Native categorization")
    ledger.create_account("Packaging Supplies", "expense", parent_code="6000")
    db.set_setting("wave_categories_cache", '{"categories":["Legacy Fake"]}')
    captured = {}
    original_available = ollama_client.is_available
    original_generate = ollama_client.generate_category
    try:
        ollama_client.is_available = lambda: True

        def generate(_prompt, categories):
            captured["categories"] = categories
            return "Packaging Supplies", "ok"

        ollama_client.generate_category = generate
        result = categorize.categorize(
            "Unrecognized Phase Four Vendor", 12, "expense", db)
    finally:
        ollama_client.is_available = original_available
        ollama_client.generate_category = original_generate
    check("custom native account is offered to the categorizer",
          "Packaging Supplies" in captured.get("categories", []))
    check("legacy Wave cache is ignored",
          "Legacy Fake" not in captured.get("categories", []))
    check("native category can be returned",
          result == ("Packaging Supplies", "medium", "ollama"))
    native_names = set(categorize.get_native_categories())
    check("every deterministic rule resolves to a native account",
          set(categorize.VENDOR_PATTERNS.values()) <= native_names)
    check("categorizer has no Wave module or cache constant",
          "wave_client" not in categorize.__dict__
          and not hasattr(categorize, "WAVE_CATEGORIES_KEY"))

    print("Native Accountant Export")
    db.set_setting("business_name", "Native Books Co")
    _entry("2026-06-10", "Native sale", "1010", "4000", "100.00",
           "phase4:sale")
    _entry("2026-06-12", "Native software", "6030", "1010", "30.00",
           "phase4:expense")
    conflicting_id = db.insert_pending_item(
        "manual", "Legacy detail row", 999.00, "2026-06-15",
        "Office Expense", direction="expense")
    db.set_status(conflicting_id, "approved")
    db.set_setting("native_reconciliation_since", "2026-06-01")
    db.insert_reconcile_snapshot("2026-06-30", 12345.00, 65.00, 999.00, True)

    data = accountant_export.gather_month_data("2026-06")
    check("statement totals come from native P&L, not queue rows",
          data["total_in"] == Decimal("100")
          and data["total_out"] == Decimal("30")
          and data["net"] == Decimal("70"))
    check("native account rollups drive the close package",
          dict(data["income_by_category"])["Sales Revenue"]["total"]
          == Decimal("100")
          and dict(data["expense_by_category"])["Software & Subscriptions"]["total"]
          == Decimal("30"))
    recon = data["reconciliation"]
    check("legacy Wave reconciliation value is ignored",
          "wave_balance" not in recon
          and recon["ledger_balance"] == Decimal("70")
          and recon["bank_balance"] == Decimal("65.0")
          and recon["residual"] == Decimal("5.0")
          and not recon["flagged"])
    rows = list(csv.DictReader(io.StringIO(
        accountant_export.build_qb_csv("2026-06"))))
    check("CSV account name comes from the native chart",
          rows and all(row["Account"] == "Checking" for row in rows))
    check("Accountant Export does not import Wave",
          "wave_client" not in accountant_export.__dict__)

    print("Wave-free setup completion")

    class ConfiguredBank:
        display_name = "Fixture Bank"

        @staticmethod
        def is_configured():
            return True

    original_password = app_lock.is_configured
    original_adapter = setup_wizard.banks.active_adapter
    try:
        app_lock.is_configured = lambda: True
        setup_wizard.banks.active_adapter = lambda: ConfiguredBank()
        setup_wizard.setup_save_business("Wave-free Books")
        finish_response = setup_wizard.setup_finish()
        app_lock.is_configured = lambda: False
        gate_response = _dashboard_passes_gate()
    finally:
        app_lock.is_configured = original_password
        setup_wizard.banks.active_adapter = original_adapter
    check("native business setup replaces the Wave step",
          setup_wizard.business_registry.active_business()["name"]
          == "Wave-free Books")
    check("setup finishes without Wave configuration",
          finish_response.status_code == 303
          and db.get_setting(setup_wizard.SETUP_COMPLETE_KEY) == "1")
    check("completed Wave-free setup allows dashboard access",
          gate_response.status_code == 200)

    if failures:
        print(f"FAIL: {failures} of {checks} Phase 4 checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} Phase 4 Wave read-path checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
