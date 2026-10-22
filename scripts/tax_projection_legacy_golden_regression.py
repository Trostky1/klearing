"""Freeze exact outputs from the legacy Wave-backed Tax projection path."""

import csv
import io
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-legacy-goldens-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from backend.routers import tax  # noqa: E402
from backend.services import financial_statements, tax_projection  # noqa: E402
from tax_projection_golden_fixtures import LEGACY_PROJECTION_SCENARIOS  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def _reset():
    with db.get_connection() as conn:
        conn.execute("DELETE FROM kl_transactions")
        conn.execute("DELETE FROM kl_tax_settings")


def _seed(scenario):
    _reset()
    settings = scenario["settings"]
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, estimated_other_income, tax_year, business_type) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (
                settings["filing_status"],
                settings["state"],
                settings["estimated_other_income"],
                settings["tax_year"],
                settings["business_type"],
            ),
        )
        for row in scenario["transactions"]:
            conn.execute(
                "INSERT INTO kl_transactions "
                "(id, wave_id, date, description, merchant, amount_cents, currency, "
                "raw_wave_category, ai_category, ai_confidence, ai_reasoning, "
                "user_approved, user_override_category, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, 0.91, ?, ?, ?, ?)",
                (
                    row["id"],
                    f"wave-{row['id']}",
                    row["date"],
                    row["description"],
                    row["merchant"],
                    row["amount_cents"],
                    f"Wave {row['category']}",
                    row["category"],
                    "Synthetic legacy golden fixture",
                    row["approved"],
                    row.get("override_category"),
                    "2026-07-01T12:00:00+00:00",
                ),
            )


def _snapshot(result):
    """Select stable calculation outputs; omit clock-dependent past-due flags."""
    # ``state_income_tax_cents`` is a new, generic alias introduced by the
    # multi-state structure. These fixtures freeze the fields the retired
    # Wave-backed projection exposed, so a duplicate new surface must not be
    # mistaken for a historical money difference.
    legacy_tax_fields = (
        "federal_income_tax_cents", "self_employment_tax_cents",
        "social_security_tax_cents", "medicare_tax_cents",
        "additional_medicare_tax_cents", "iowa_income_tax_cents",
        "total_estimated_tax_cents",
    )
    return {
        "basis": result["basis"],
        "is_estimate_complete": result["is_estimate_complete"],
        "approved_transaction_count": result["approved_transaction_count"],
        "income": result["income"],
        "deductions": result["deductions"],
        "taxable_income": result["taxable_income"],
        "taxes": {key: result["taxes"][key] for key in legacy_tax_fields},
        "quarterly_estimates": [
            {"due_date": item["due_date"], "amount_cents": item["amount_cents"]}
            for item in result["quarterly_estimates"]
        ],
    }


def _project(scenario):
    as_of = date.fromisoformat(scenario["as_of"])
    statement = financial_statements.income_statement(
        date(as_of.year, 1, 1).isoformat(), scenario["as_of"])
    return tax_projection.projection(
        scenario["settings"], as_of, statement,
        scenario["section_179_cents"],
        scenario["home_office_square_feet"],
    )


def _archive_checks(scenario):
    failures = 0
    listed = tax.list_legacy_transactions("all")["transactions"]
    statuses = {row["id"]: row["approval_status"] for row in listed}
    expected_statuses = {
        row["id"]: {0: "pending", 1: "approved", 2: "rejected"}[row["approved"]]
        for row in scenario["transactions"]
    }
    failures += check(
        "legacy Wave rows remain visible with exact approval states",
        statuses == expected_statuses,
    )

    response = tax.export_legacy_tax_transactions_csv(
        "2026-01-01", scenario["as_of"])
    exported = list(csv.DictReader(io.StringIO(response.body.decode("utf-8"))))
    approved = [row for row in scenario["transactions"] if row["approved"] == 1]
    failures += check(
        "legacy CSV exports every approved row and no unapproved row",
        {row["transaction_id"] for row in exported} == {row["id"] for row in approved}
        and len(exported) == len(approved),
    )
    exported_by_id = {row["transaction_id"]: row for row in exported}
    failures += check(
        "legacy CSV preserves exact cents and effective human category",
        all(
            exported_by_id[row["id"]]["amount"] == f"{row['amount_cents'] / 100:.2f}"
            and exported_by_id[row["id"]]["schedule_c_category"]
            == row.get("override_category", row["category"])
            for row in approved
        ),
    )
    return failures


def run(capture=False):
    failures = 0
    db.init_db()
    captured = {}
    for scenario in LEGACY_PROJECTION_SCENARIOS:
        _seed(scenario)
        snapshot = _snapshot(_project(scenario))
        captured[scenario["name"]] = snapshot
        if not capture:
            failures += check(
                f"{scenario['name']} matches every legacy money golden",
                snapshot == scenario["expected"],
            )

    _seed(LEGACY_PROJECTION_SCENARIOS[0])
    failures += _archive_checks(LEGACY_PROJECTION_SCENARIOS[0])

    if capture:
        print(json.dumps(captured, indent=4, sort_keys=True))
        return 0
    if failures:
        print(f"FAIL: {failures} legacy tax golden checks failed.")
        return 1
    print("PASS: Legacy Wave-backed Tax golden checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(capture="--capture" in sys.argv[1:]))
