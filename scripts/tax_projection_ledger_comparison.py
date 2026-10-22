"""Compare frozen Wave projection goldens with equivalent native Ledger data."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-ledger-comparison-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ledger  # noqa: E402
import tax_categories  # noqa: E402
from backend.routers import tax  # noqa: E402
from backend.services import financial_statements, tax_projection  # noqa: E402
from tax_projection_golden_fixtures import LEGACY_PROJECTION_SCENARIOS  # noqa: E402


CATEGORY_ACCOUNT = {
    "gross_receipts": "4000",
    "returns_and_allowances": "4900",
    "supplies": "5000",
    "contract_labor": "6060",
    "office_expense": "6040",
    "legal_and_professional": "6050",
    "owner_draw": "3100",
}


def _reset():
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ledger_source_imports")
        conn.execute("DELETE FROM journal_lines")
        conn.execute("DELETE FROM journal_entries")
        conn.execute("DELETE FROM kl_transactions")
        conn.execute("DELETE FROM kl_tax_settings")
        conn.execute(
            "UPDATE accounts SET tax_category = NULL "
            "WHERE type IN ('revenue', 'expense')"
        )
        conn.executemany(
            "UPDATE accounts SET tax_category = ? WHERE code = ?",
            [(category, code) for code, category
             in tax_categories.DEFAULT_ACCOUNT_CATEGORIES.items()],
        )


def _seed_settings(settings):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, estimated_other_income, tax_year, business_type) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (
                settings["filing_status"], settings["state"],
                settings["estimated_other_income"], settings["tax_year"],
                settings["business_type"],
            ),
        )


def _seed_legacy(scenario):
    with db.get_connection() as conn:
        for row in scenario["transactions"]:
            conn.execute(
                "INSERT INTO kl_transactions "
                "(id, date, amount_cents, ai_category, user_approved, "
                "user_override_category) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["date"], row["amount_cents"],
                    row["category"], row["approved"], row.get("override_category"),
                ),
            )


def _post(row):
    if row["approved"] != 1:
        return
    category = row.get("override_category", row["category"])
    cents = abs(int(row["amount_cents"]))
    if category == "transfer":
        debit_code, credit_code = "1000", "1010"
    elif category == "gross_receipts":
        debit_code, credit_code = "1010", CATEGORY_ACCOUNT[category]
    else:
        debit_code, credit_code = CATEGORY_ACCOUNT[category], "1010"
    ledger.create_journal_entry(
        row["date"], row["description"], [
            {"account_id": ledger.account_by_code(debit_code)["id"],
             "debit_amount": f"{cents / 100:.2f}"},
            {"account_id": ledger.account_by_code(credit_code)["id"],
             "credit_amount": f"{cents / 100:.2f}"},
        ], created_by="tax-ledger-comparison",
        source_reference=f"comparison:{row['id']}",
    )


def _seed_ledger(scenario):
    categories = {
        row.get("override_category", row["category"])
        for row in scenario["transactions"] if row["approved"] == 1
    }
    for category in categories:
        code = CATEGORY_ACCOUNT.get(category)
        if code and ledger.account_by_code(code)["type"] in {"revenue", "expense"}:
            ledger.set_account_tax_category(
                ledger.account_by_code(code)["id"], category)
    for row in scenario["transactions"]:
        _post(row)


def _legacy_projection(scenario):
    as_of = date.fromisoformat(scenario["as_of"])
    statement = financial_statements.income_statement(
        date(as_of.year, 1, 1).isoformat(), scenario["as_of"])
    return tax_projection.projection(
        scenario["settings"], as_of, statement,
        scenario["section_179_cents"], scenario["home_office_square_feet"],
    )


def _ledger_projection(scenario):
    return tax.tax_projection_endpoint(
        as_of=scenario["as_of"],
        section_179_cents=scenario["section_179_cents"],
        home_office_square_feet=scenario["home_office_square_feet"],
    )


def _money_rows(result):
    rows = {
        "YTD net income": result["income"]["ytd_net_income_cents"],
        "Annualized net income": result["income"]["annualized_net_income_cents"],
        "Federal income tax": result["taxes"]["federal_income_tax_cents"],
        "Self-employment tax": result["taxes"]["self_employment_tax_cents"],
        "Iowa income tax": result["taxes"]["iowa_income_tax_cents"],
        "Total estimated tax": result["taxes"]["total_estimated_tax_cents"],
    }
    for index, quarter in enumerate(result["quarterly_estimates"], 1):
        rows[f"Quarter {index} ({quarter['due_date']})"] = quarter["amount_cents"]
    return rows


def _money(cents):
    return "N/A" if cents is None else f"${cents / 100:,.2f}"


def run():
    db.init_db()
    failures = 0
    rows = []
    for scenario in LEGACY_PROJECTION_SCENARIOS:
        _reset()
        _seed_settings(scenario["settings"])
        _seed_legacy(scenario)
        _seed_ledger(scenario)
        legacy = _money_rows(_legacy_projection(scenario))
        native = _money_rows(_ledger_projection(scenario))
        for field, old_value in legacy.items():
            new_value = native[field]
            delta = None if old_value is None or new_value is None else new_value - old_value
            matches = old_value == new_value
            failures += 0 if matches else 1
            rows.append((scenario["name"], field, old_value, new_value, delta, matches))

    headings = ("Scenario", "Field", "Legacy Wave", "Native Ledger", "Delta", "Result")
    printable = [
        (
            scenario, field, _money(old), _money(new),
            "N/A" if delta is None else _money(delta), "MATCH" if matches else "DIFF",
        )
        for scenario, field, old, new, delta, matches in rows
    ]
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in printable))
        for index in range(len(headings))
    ]
    print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(headings)))
    print("-+-".join("-" * width for width in widths))
    for row in printable:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))

    if failures:
        print(f"FAIL: {failures} legacy/native money discrepancies require review.")
        return 1
    print("PASS: Every legacy/native money value matches exactly; every delta is $0.00.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
