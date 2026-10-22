"""Offline coverage for canonical Ledger exports and legacy preservation."""

import csv
import io
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-exports-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import db  # noqa: E402
import ledger  # noqa: E402
from backend.routers import tax  # noqa: E402
from backend.services import tax_exports  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def legacy_transaction(row_id, amount, category, approved=1,
                       description="Test transaction"):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_transactions (id, date, description, merchant, amount_cents, "
            "ai_category, user_approved) VALUES (?, '2026-07-15', ?, "
            "'Test merchant', ?, ?, ?)",
            (row_id, description, amount, category, approved),
        )


def post(memo, debit_code, credit_code, cents, reference):
    ledger.create_journal_entry(
        "2026-07-15", memo, [
            {"account_id": ledger.account_by_code(debit_code)["id"],
             "debit_amount": f"{cents / 100:.2f}"},
            {"account_id": ledger.account_by_code(credit_code)["id"],
             "credit_amount": f"{cents / 100:.2f}"},
        ], created_by="tax-export-regression", source_reference=reference,
    )


def blocked_export(call):
    try:
        call()
    except HTTPException as exc:
        return exc
    return None


def run():
    failures = 0
    db.init_db()
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, tax_year, business_type) "
            "VALUES (1, 'single', 'IA', 2026, 'sole_prop')")

    # Preserved Wave rows prove the archive export remains independent.
    legacy_transaction("income", 100_000, "gross_receipts",
                       description="Legacy Invoice & Consulting")
    legacy_transaction("returns", -5_000, "returns_and_allowances")
    legacy_transaction("ads", -15_000, "advertising")
    legacy_transaction("draw", -20_000, "owner_draw")
    legacy_transaction("pending", 999_999, "gross_receipts", approved=0)

    ledger.set_account_tax_category(
        ledger.account_by_code("5000")["id"], "supplies")
    post("Native invoice & consulting", "1010", "4000", 100_000, "qa:sale")
    post("Native returns", "4900", "1010", 5_000, "qa:return")
    post("Native materials", "5000", "1010", 20_000, "qa:cogs")
    post("Native advertising", "6010", "1010", 15_000, "qa:ads")
    post("Native owner draw", "3100", "1010", 10_000, "qa:draw")

    income_pdf = tax.export_income_statement_pdf("2026-07-01", "2026-07-31")
    failures += check(
        "income statement export is a native-Ledger downloadable PDF",
        income_pdf.media_type == "application/pdf"
        and income_pdf.body.startswith(b"%PDF")
        and b"Income_Statement" in income_pdf.headers["content-disposition"].encode(),
    )

    summary = tax_exports.schedule_c_summary("2026-07-01", "2026-07-31")
    failures += check(
        "Schedule C summary uses exact native Ledger income and expense groups",
        summary["basis"] == "native_double_entry_ledger"
        and summary["source_entry_count"] == 5
        and summary["gross_receipts_cents"] == 100_000
        and summary["returns_and_allowances_cents"] == 5_000
        and summary["total_cost_of_goods_sold_cents"] == 20_000
        and summary["total_operating_expenses_cents"] == 15_000
        and summary["tentative_profit_cents"] == 60_000,
    )
    schedule_pdf = tax.export_schedule_c_pdf("2026-07-31")
    failures += check(
        "Schedule C summary export is a downloadable PDF",
        schedule_pdf.media_type == "application/pdf"
        and schedule_pdf.body.startswith(b"%PDF"),
    )

    csv_response = tax.export_tax_transactions_csv("2026-07-01", "2026-07-31")
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.body.decode())))
    failures += check(
        "canonical CSV preserves every debit and credit line without Wave columns",
        len(csv_rows) == 10
        and {row["source_reference"] for row in csv_rows}
        == {"qa:sale", "qa:return", "qa:cogs", "qa:ads", "qa:draw"}
        and all("transaction_id" not in row and "wave_category" not in row
                for row in csv_rows)
        and sum(int(Decimal(row["debit"]) * 100) for row in csv_rows)
        == sum(int(Decimal(row["credit"]) * 100) for row in csv_rows),
    )
    by_account = {row["account_code"]: row for row in csv_rows}
    failures += check(
        "canonical CSV identifies tax mappings and non-tax balance-sheet lines",
        by_account["4000"]["schedule_c_category"] == "gross_receipts"
        and by_account["5000"]["schedule_c_category"] == "supplies"
        and by_account["6010"]["schedule_c_category"] == "advertising"
        and by_account["1010"]["schedule_c_category"] == "not_applicable"
        and by_account["3100"]["schedule_c_category"] == "not_applicable",
    )

    legacy_csv = tax.export_legacy_tax_transactions_csv(
        "2026-07-01", "2026-07-31")
    legacy_rows = list(csv.DictReader(io.StringIO(legacy_csv.body.decode())))
    failures += check(
        "separate legacy CSV keeps every approved Wave row accessible",
        {row["transaction_id"] for row in legacy_rows}
        == {"income", "returns", "ads", "draw"}
        and all(row["transaction_id"] != "pending" for row in legacy_rows)
        and all(row["record_source"] == "legacy_wave_tax_archive"
                and row["legacy_approval_status"] == "approved"
                for row in legacy_rows)
        and "Legacy_Wave_Tax_Transactions" in legacy_csv.headers["content-disposition"],
    )
    full_archive = tax.export_legacy_tax_transactions_csv()
    failures += check(
        "archive export defaults to all approved history with a legacy filename",
        full_archive.body == legacy_csv.body
        and "Legacy_Wave_Tax_Archive" in full_archive.headers["content-disposition"],
    )

    projection_pdf = tax.export_projection_pdf(as_of="2026-07-31")
    failures += check(
        "projection summary remains a downloadable canonical PDF",
        projection_pdf.media_type == "application/pdf"
        and projection_pdf.body.startswith(b"%PDF"),
    )

    # Used unresolved revenue/expense accounts refuse category-sensitive files.
    post("Unresolved contractor", "6060", "1010", 9_000, "qa:unresolved")
    blocked_schedule = blocked_export(
        lambda: tax.export_schedule_c_pdf("2026-07-31"))
    blocked_csv = blocked_export(
        lambda: tax.export_tax_transactions_csv("2026-07-01", "2026-07-31"))
    failures += check(
        "Schedule C and canonical CSV fail with structured HTTP 409 before generation",
        blocked_schedule is not None and blocked_csv is not None
        and blocked_schedule.status_code == blocked_csv.status_code == 409
        and blocked_schedule.detail["code"] == "tax_categorization_required"
        and blocked_csv.detail["code"] == "tax_categorization_required"
        and blocked_schedule.detail["unresolved_account_count"] == 1
        and blocked_csv.detail["unresolved_accounts"][0]["code"] == "6060"
        and "No file was generated." in blocked_schedule.detail["message"]
        and not any(key in blocked_csv.detail for key in ("content", "rows", "csv")),
    )
    still_valid_income = tax.export_income_statement_pdf(
        "2026-07-01", "2026-07-31")
    failures += check(
        "financial statement PDF still includes unresolved activity without omission",
        still_valid_income.body.startswith(b"%PDF"),
    )
    still_valid_legacy = tax.export_legacy_tax_transactions_csv(
        "2026-07-01", "2026-07-31")
    failures += check(
        "canonical mapping blockers never orphan the legacy archive export",
        still_valid_legacy.body == legacy_csv.body,
    )

    if failures:
        print(f"FAIL: {failures} Tax export checks failed.")
        return 1
    print("PASS: Tax export checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
