"""Offline coverage for canonical native-Ledger Tax statement endpoints."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-statements-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import db  # noqa: E402
import ledger  # noqa: E402
from backend.routers import tax  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def _post(memo, debit_code, credit_code, cents):
    ledger.create_journal_entry(
        "2026-07-15", memo, [
            {"account_id": ledger.account_by_code(debit_code)["id"],
             "debit_amount": f"{cents / 100:.2f}"},
            {"account_id": ledger.account_by_code(credit_code)["id"],
             "credit_amount": f"{cents / 100:.2f}"},
        ], created_by="tax-statements-regression",
    )


def run():
    failures = 0
    db.init_db()
    ledger.set_account_tax_category(ledger.account_by_code("5000")["id"], "supplies")
    ledger.set_account_tax_category(ledger.account_by_code("6070")["id"], "rent_other")
    _post("Revenue", "1010", "4000", 100_000)
    _post("Returns", "4900", "1010", 5_000)
    _post("Supplies", "5000", "1010", 30_000)
    _post("Rent", "6070", "1010", 15_000)
    _post("Advertising", "6010", "1010", 5_000)
    _post("Owner draw", "3100", "1010", 8_000)

    statement = tax.income_statement("2026-07-01", "2026-07-31")
    failures += check(
        "income statement uses native Ledger entries and exact cents",
        statement["basis"] == "native_double_entry_ledger"
        and statement["income"] == {
            "gross_receipts_cents": 100_000,
            "returns_and_allowances_cents": 5_000,
            "unresolved_revenue_cents": 0,
            "net_revenue_cents": 95_000,
        }
        and statement["source_entry_count"] == 6,
    )
    failures += check(
        "income statement separates COGS and operating expenses",
        statement["cost_of_goods_sold"]["total_cents"] == 30_000
        and statement["operating_expenses"]["total_cents"] == 20_000
        and statement["gross_profit_cents"] == 65_000
        and statement["net_income_cents"] == 45_000,
    )
    failures += check(
        "mapped statement is complete and margins use net revenue",
        statement["is_tax_complete"] is True
        and round(statement["gross_margin"], 4) == round(65_000 / 95_000, 4)
        and round(statement["net_margin"], 4) == round(45_000 / 95_000, 4),
    )
    balance = tax.balance_sheet("2026-07-31")
    failures += check(
        "balance endpoint exposes the complete balanced native Ledger position",
        balance["basis"] == "native_double_entry_ledger"
        and balance["is_complete"] is True
        and balance["is_balanced"] is True
        and balance["assets_total_cents"] == 37_000
        and balance["current_earnings_cents"] == 45_000
        and balance["equity_accounts_total_cents"] == -8_000
        and balance["equity_total_cents"] == 37_000
        and balance["difference_cents"] == 0,
    )
    try:
        tax.income_statement("2026-08-01", "2026-07-01")
    except HTTPException as exc:
        bad_range = exc
    else:
        bad_range = None
    failures += check(
        "invalid statement periods return HTTP 400",
        bad_range is not None and bad_range.status_code == 400,
    )
    if failures:
        print(f"FAIL: {failures} Tax statement checks failed.")
        return 1
    print("PASS: Tax statement checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
