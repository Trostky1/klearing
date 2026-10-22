"""Offline coverage for fail-closed native-Ledger Tax projections."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-projection-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import app  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
from backend.routers import tax  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def _post(entry_date, memo, debit_code, credit_code, cents):
    debit = ledger.account_by_code(debit_code)
    credit = ledger.account_by_code(credit_code)
    ledger.create_journal_entry(
        entry_date, memo, [
            {"account_id": debit["id"], "debit_amount": f"{cents / 100:.2f}"},
            {"account_id": credit["id"], "credit_amount": f"{cents / 100:.2f}"},
        ], created_by="tax-projection-regression",
    )


def run():
    failures = 0
    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, estimated_other_income, tax_year, business_type) "
            "VALUES (1, 'single', 'IA', 0, 2026, 'sole_prop')")
    _post("2026-12-31", "Income", "1010", "4000", 10_000_000)
    _post("2026-12-31", "Office expense", "6040", "1010", 2_000_000)

    result = tax.tax_projection_endpoint(
        as_of="2026-12-31", section_179_cents=1_000_000,
        home_office_square_feet=100)
    failures += check(
        "projection uses native Ledger net income only",
        result["basis"] == "native_double_entry_ledger"
        and result["income"]["ytd_net_income_cents"] == 8_000_000
        and result["income"]["annualized_net_income_cents"] == 8_000_000
        and result["source_entry_count"] == 2
        and "approved_transaction_count" not in result,
    )
    failures += check(
        "Section 179 and simplified home office reduce projected income",
        result["deductions"]["section_179_allowed_cents"] == 1_000_000
        and result["deductions"]["home_office_deduction_cents"] == 50_000,
    )
    failures += check(
        "sole proprietor projection includes SE tax, QBI, and Iowa flat tax",
        result["taxes"]["self_employment_tax_cents"] > 0
        and result["deductions"]["qbi_deduction_cents"] > 0
        and result["taxes"]["iowa_income_tax_cents"] > 0
        and result["taxes"]["state_income_tax_cents"] == result["taxes"]["iowa_income_tax_cents"]
        and result["state"] == {
            "code": "IA", "name": "Iowa", "available": True,
            "tax_cents": result["taxes"]["iowa_income_tax_cents"],
            "warning": None, "data_tier": 2,
            "last_verified_tax_year": 2026, "local_tax_mode": "none",
        }
        and result["assumptions"]["iowa_flat_rate"] == 0.038,
    )
    failures += check(
        "quarterly estimate contains all statutory 2026 due dates",
        [item["due_date"] for item in result["quarterly_estimates"]]
        == ["2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15"],
    )
    try:
        tax.tax_projection_endpoint(section_179_cents=-1)
    except HTTPException as exc:
        bad_deduction = exc
    else:
        bad_deduction = None
    failures += check(
        "negative deduction inputs return HTTP 400",
        bad_deduction is not None and bad_deduction.status_code == 400,
    )

    original_today = tax._today
    tax._today = lambda: date(2027, 1, 2)
    try:
        try:
            tax.tax_projection_endpoint(as_of="2026-12-31")
        except HTTPException as exc:
            stale = exc
        else:
            stale = None
        try:
            tax.export_projection_pdf(as_of="2026-12-31")
        except HTTPException as exc:
            stale_export = exc
        else:
            stale_export = None
    finally:
        tax._today = original_today
    failures += check(
        "a prior tax year blocks projections before returning money values",
        stale is not None
        and stale.status_code == 409
        and stale.detail["code"] == "tax_year_stale"
        and stale.detail["title"] == "Projections have not been updated for 2027 yet"
        and not any(key in stale.detail for key in ("income", "taxes", "quarterly_estimates")),
    )
    failures += check(
        "projection PDF refuses a stale tax year too",
        stale_export is not None
        and stale_export.status_code == 409
        and stale_export.detail["code"] == "tax_year_stale",
    )

    # A nonzero unmapped account blocks before any calculation result exists.
    _post("2026-12-31", "Unresolved payroll", "6060", "1010", 100_000)
    try:
        tax.tax_projection_endpoint(as_of="2026-12-31")
    except HTTPException as exc:
        blocked = exc
    else:
        blocked = None
    failures += check(
        "unresolved nonzero account returns structured HTTP 409 with no money",
        blocked is not None
        and blocked.status_code == 409
        and blocked.detail["code"] == "tax_categorization_required"
        and blocked.detail["unresolved_account_count"] == 1
        and blocked.detail["unresolved_accounts"][0]["code"] == "6060"
        and not any(key in blocked.detail for key in ("income", "taxes", "quarterly_estimates")),
    )
    try:
        tax.export_projection_pdf(as_of="2026-12-31")
    except HTTPException as exc:
        blocked_export = exc
    else:
        blocked_export = None
    failures += check(
        "projection PDF refuses the same unresolved calculation",
        blocked_export is not None
        and blocked_export.status_code == 409
        and blocked_export.detail["code"] == "tax_categorization_required",
    )
    projection_html = app._tax_projections_content()
    workspace_js = (Path(__file__).resolve().parent.parent
                    / "frontend" / "tax" / "workspace.js").read_text(encoding="utf-8")
    failures += check(
        "projection UI starts with every money surface and PDF link disabled",
        'id="tax-projection-output" hidden' in projection_html
        and 'id="tax-projection-export" class="btn-link" aria-disabled="true"'
        in projection_html
        and 'id="tax-projection-blocked"' in projection_html
        and "resetOutput();" in workspace_js
        and "output.hidden = true;" in workspace_js
        and 'exportLink.removeAttribute("href");' in workspace_js,
    )
    failures += check(
        "projection UI renders reusable fail-closed states without inline styles",
        "No estimate is shown." in blocked.detail["message"]
        and 'id="tax-projection-blocked-title"' in projection_html
        and 'id="tax-projection-blocked-action"' in projection_html
        and "tax_year_stale" in workspace_js
        and "style=" not in projection_html,
    )
    ledger.set_account_tax_category(ledger.account_by_code("6060")["id"], "wages")
    with db.get_connection() as conn:
        conn.execute("UPDATE kl_tax_settings SET business_type = 's_corp' WHERE id = 1")
    s_corp = tax.tax_projection_endpoint(as_of="2026-12-31")
    failures += check(
        "entity types needing payroll review are disclosed",
        s_corp["is_estimate_complete"] is False
        and s_corp["taxes"]["self_employment_tax_cents"] == 0,
    )
    if failures:
        print(f"FAIL: {failures} Tax projection checks failed.")
        return 1
    print("PASS: Tax projection checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
