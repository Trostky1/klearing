"""Phase 5 proof that Wave is absent while historical audit data survives."""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-wave-removal-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import accountant_export  # noqa: E402
import app  # noqa: E402
import business_registry  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import setup_wizard  # noqa: E402


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}")


def run():
    db.init_db()
    print("Runtime removal")
    check("wave_client.py is deleted", not (ROOT / "wave_client.py").exists())
    check("Wave module cannot be imported", importlib.util.find_spec("wave_client") is None)
    check("production modules do not expose Wave client",
          all("wave_client" not in module.__dict__
              for module in (app, setup_wizard, accountant_export)))
    check("setup uses native Business step",
          setup_wizard.STEPS[1] == "Business"
          and hasattr(setup_wizard, "setup_save_business")
          and not hasattr(setup_wizard, "setup_save_wave"))

    print("Legacy identity migration")
    db.set_setting("wave_businesses", json.dumps([
        {"id": "legacy-business-id", "name": "Preserved Co",
         "anchor_account_name": "Cash on Hand"}]))
    db.set_setting("wave_active_business_id", "legacy-business-id")
    db.set_setting("wave_categories_cache", "stale")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (name, encrypted_value, is_file, updated_at) "
            "VALUES ('wave.api_token', X'00', 0, '2026-08-12')")
    migrated = business_registry.active_business()
    check("existing business ID and name are preserved",
          migrated == {"id": "legacy-business-id", "name": "Preserved Co"})
    check("obsolete Wave settings are deleted",
          all(db.get_setting(key) is None for key in (
              "wave_businesses", "wave_active_business_id",
              "wave_categories_cache")))
    check("obsolete Wave credential is deleted",
          db.get_credential("wave.api_token") is None)

    print("Native reconciliation")
    checking = ledger.account_by_code("1010")
    equity = ledger.account_by_code("3000")
    ledger.create_sourced_journal_entry(
        "2026-08-12", "Opening cash",
        [{"account_id": checking["id"], "debit_amount": "250.00",
          "credit_amount": "0"},
         {"account_id": equity["id"], "debit_amount": "0",
          "credit_amount": "250.00"}],
        source_reference="phase5:opening", created_by="phase-5-regression")
    original_config = app.cash_alert.get_config
    original_match = app.cash_alert.matched_cash_accounts
    db.insert_reconcile_snapshot("2026-08-11", 9999.0, 1.0, 9998.0, True)
    try:
        app.cash_alert.get_config = lambda: {"cash_account_ids": ["bank-1"]}
        app.cash_alert.matched_cash_accounts = lambda _ids: [
            {"id": "bank-1", "name": "Checking", "balance": 240.0}]
        data = app.gather_reconciliation()
        app.record_reconcile_snapshot(data)
    finally:
        app.cash_alert.get_config = original_config
        app.cash_alert.matched_cash_accounts = original_match
    check("reconciliation compares ledger cash to bank",
          data["books_balance"] == 250.0
          and data["bank_balance"] == 240.0
          and data["residual"] == 10.0)
    history = db.list_reconcile_history(days=30)
    check("native snapshot is explicitly marked as native-era",
          db.get_setting(app.NATIVE_RECONCILE_SINCE_KEY) == "2026-08-12"
          and history[-1]["wave_balance"] == 250.0)
    check("pre-cutover reconciliation rows stay historical",
          accountant_export._nearest_snapshot_on_or_before("2026-08-11") is None)

    print("Historical audit preservation")
    item_id = db.insert_pending_item(
        "manual", "Historical Vendor", 12.0, "2026-08-01",
        "Office Expense", direction="expense")
    db.set_status(item_id, "approved", wave_transaction_id="historical-wave-id",
                  business_id="legacy-business-id")
    saved = db.get_item(item_id)
    check("wave_transaction_id audit column remains",
          saved["wave_transaction_id"] == "historical-wave-id")
    check("historical business stamp still resolves",
          saved["business_id"] == "legacy-business-id"
          and business_registry.business_name_by_id(saved["business_id"])
          == "Preserved Co")

    print("Sparse legacy identity migration")
    db.delete_setting(business_registry.BUSINESSES_KEY)
    db.delete_setting(business_registry.ACTIVE_BUSINESS_KEY)
    db.set_setting("business_name", "Sparse Legacy Co")
    db.set_setting("wave_active_business_id", "sparse-legacy-id")
    sparse = business_registry.active_business()
    check("active-only legacy business ID is preserved",
          sparse == {"id": "sparse-legacy-id", "name": "Sparse Legacy Co"})

    if failures:
        print(f"FAIL: {failures} of {checks} Phase 5 checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} Phase 5 Wave-removal checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
