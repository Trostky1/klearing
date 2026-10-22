"""Offline regression coverage for approved-ledger Financial Insights."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-financial-insights-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
from backend.services import financial_insights  # noqa: E402
from config import APP_VERSION  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def entry(row_id, row_date, amount_cents, account, cash, income=False):
    amount = f"{amount_cents / 100:.2f}"
    lines = ([
        {"account_id": cash["id"], "debit_amount": amount},
        {"account_id": account["id"], "credit_amount": amount},
    ] if income else [
        {"account_id": account["id"], "debit_amount": amount},
        {"account_id": cash["id"], "credit_amount": amount},
    ])
    ledger.create_journal_entry(
        row_date, row_id, lines, created_by="regression",
        source_reference=f"fixture:{row_id}",
    )


def seed():
    db.init_db()
    by_code = {row["code"]: row for row in ledger.list_accounts()}
    ledger.set_account_tax_category(by_code["5000"]["id"], "supplies")
    ledger.set_account_tax_category(by_code["6060"]["id"], "wages")
    ledger.set_account_tax_category(by_code["6070"]["id"], "employee_benefits")
    cash = by_code["1010"]
    rows = (
        ("may-income", "2026-05-01", 100000, by_code["4000"], True),
        ("may-supplies", "2026-05-02", 20000, by_code["5000"], False),
        ("may-wages", "2026-05-03", 10000, by_code["6060"], False),
        ("may-ads", "2026-05-04", 9000, by_code["6010"], False),
        ("jun-income", "2026-06-01", 120000, by_code["4000"], True),
        ("jun-supplies", "2026-06-02", 25000, by_code["5000"], False),
        ("jun-wages", "2026-06-03", 15000, by_code["6060"], False),
        ("jul-income", "2026-07-01", 150000, by_code["4000"], True),
        ("jul-supplies", "2026-07-02", 30000, by_code["5000"], False),
        ("jul-wages", "2026-07-03", 20000, by_code["6060"], False),
        ("jul-benefits", "2026-07-04", 5000, by_code["6070"], False),
    )
    for row in rows:
        entry(row[0], row[1], row[2], row[3], cash, income=row[4])
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_transactions "
            "(id, date, amount_cents, ai_category, user_approved) "
            "VALUES ('legacy-decoy', '2026-07-05', 999999, "
            "'gross_receipts', 1)"
        )
    db.upsert_shopify_customer({
        "customer_id": "customer-1", "email": "one@example.test",
        "first_name": "One", "last_name": "Customer", "orders_count": 2,
        "total_spent": 500.0, "created_at": "2026-05-10T00:00:00Z",
        "tags": "", "accepts_marketing": 0,
    })
    db.upsert_shopify_customer({
        "customer_id": "customer-2", "email": "two@example.test",
        "first_name": "Two", "last_name": "Customer", "orders_count": 1,
        "total_spent": 100.0, "created_at": "2026-07-10T00:00:00Z",
        "tags": "", "accepts_marketing": 0,
    })
    db.insert_invoice_reminder(
        "wave-invoice-1", "1001", "Customer", "customer@example.test",
        "300.00 USD", "2026-07-01", "Subject", "Body")
    db.insert_invoice_reminder(
        "wave-invoice-1", "1001", "Customer", "customer@example.test",
        "300.00 USD", "2026-07-01", "Subject again", "Body again")


def run():
    failures = 0
    seed()
    snapshot = financial_insights.financial_snapshot(
        as_of=date(2026, 7, 21), months=6, window_days=90)

    failures += check(
        "native Ledger baseline ignores the preserved Wave archive",
        snapshot["source_entry_count"] == 11
        and snapshot["basis"] == "native_double_entry_ledger"
        and snapshot["monthly_revenue_cents"] == round(370000 / 3))
    failures += check(
        "burn includes native Ledger COGS and operating expenses",
        snapshot["daily_burn_cents"] == round(134000 / 90)
        and snapshot["monthly_burn_cents"] == round(134000 / 90) * 30)
    failures += check(
        "six-month margin trend is derived from financial statements",
        len(snapshot["trends"]) == 6
        and snapshot["trends"][-1]["label"] == "Jul"
        and round(snapshot["trends"][-1]["gross_margin_pct"], 1) == 80.0)
    failures += check(
        "payroll ratio uses categorized Ledger wages and employee benefits",
        snapshot["payroll_cents"] == 50000
        and round(snapshot["payroll_ratio_pct"], 2) == round(50000 / 370000 * 100, 2))
    failures += check(
        "Shopify enriches LTV and acquisition cost without a new API call",
        snapshot["ltv_cents"] == 30000
        and snapshot["new_customer_count"] == 2
        and snapshot["cac_cents"] == 4500)
    failures += check(
        "DSO estimate uses the latest state once per tracked invoice",
        snapshot["tracked_open_invoice_count"] == 1
        and snapshot["overdue_receivables_cents"] == 30000
        and round(snapshot["dso_days"], 2) == round(30000 / 370000 * 90, 2))

    entry("unmapped", "2026-07-06", 1000, ledger.account_by_code("6030"),
          ledger.account_by_code("1010"))
    incomplete = financial_insights.financial_snapshot(
        as_of=date(2026, 7, 21), months=6, window_days=90)
    failures += check(
        "unresolved Ledger accounts preserve complete totals but suppress category metrics",
        incomplete["monthly_expenses_cents"] > snapshot["monthly_expenses_cents"]
        and incomplete["category_metrics_complete"] is False
        and incomplete["payroll_cents"] is None
        and incomplete["payroll_ratio_pct"] is None
        and incomplete["cac_cents"] is None,
    )

    scenario = financial_insights.apply_scenario(
        snapshot, revenue_change_pct=10, expense_change_pct=-10,
        one_time_cost_cents=10000, cash_balance_cents=100000)
    expected_revenue = round(snapshot["monthly_revenue_cents"] * 1.10)
    expected_recurring = round(snapshot["monthly_expenses_cents"] * 0.90)
    failures += check(
        "what-if scenario changes no stored transaction and calculates locally",
        scenario["revenue_cents"] == expected_revenue
        and scenario["recurring_expenses_cents"] == expected_recurring
        and scenario["net_income_cents"] == expected_revenue - expected_recurring - 10000
        and round(scenario["runway_days"], 2)
        == round(90000 / (expected_recurring / 30), 2))

    financial_page = app.render_insights_page(
        None, tab="financial", financial=snapshot)
    app.ga4_client.is_configured = lambda: False
    marketing_page = app.render_insights_page(None, tab="marketing")
    failures += check(
        "Insights exposes Financial and preserved Marketing tabs",
        all(marker in financial_page for marker in (
            "Financial", "Marketing", 'id="financial-insights"',
            'id="insights-scenario-form"'))
        and "GA4 isn't connected" in marketing_page)
    workspace = (ROOT / "frontend" / "insights" / "financial.js").read_text(
        encoding="utf-8")
    failures += check(
        "frontend uses safe text updates and the shared cash endpoint",
        ".textContent" in workspace and "innerHTML" not in workspace
        and "/api/insights/cash" in workspace)
    failures += check(
        "desktop package includes Financial Insights assets",
        '"frontend", "insights"' in (ROOT / "build" / "klearing.spec").read_text(
            encoding="utf-8"))
    routes = {route.path for route in app.app.routes if hasattr(route, "path")}
    failures += check(
        "cash and static frontend routes are registered",
        "/api/insights/cash" in routes
        and "/static/insights/financial.js" in routes)
    original_config = app.cash_alert.get_config
    original_balance = app.cash_alert.current_cash_balance
    try:
        app.cash_alert.get_config = lambda: {
            "cash_account_ids": ["checking"],
            "balance_threshold": None, "runway_days": None,
        }
        app.cash_alert.current_cash_balance = lambda account_ids: 1000.0
        cash = app.insights_cash()
        app.cash_alert.current_cash_balance = lambda account_ids: (_ for _ in ()).throw(
            RuntimeError("offline"))
        unavailable_cash = app.insights_cash()
    finally:
        app.cash_alert.get_config = original_config
        app.cash_alert.current_cash_balance = original_balance
    failures += check(
        "cash lookup reuses selected accounts and fails without breaking insights",
        cash["state"] == "ok" and cash["balance_cents"] == 100000
        and cash["runway_days"] > 0
        and unavailable_cash["state"] == "error"
        and unavailable_cash["runway_days"] is None)
    failures += check("release version is v7", APP_VERSION == "7")

    if failures:
        print(f"FAIL: {failures} Financial Insights checks failed.")
        return 1
    print("PASS: Financial Insights checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
