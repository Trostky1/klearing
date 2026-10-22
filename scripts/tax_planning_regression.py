"""Offline coverage for Tax Planning's local-only SKU and formula paths."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-planning-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ledger  # noqa: E402
from backend.routers import tax  # noqa: E402
from backend.services import sku_service  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def legacy_transaction(row_id, category, amount, approved=1):
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_transactions (id, date, amount_cents, ai_category, "
            "user_approved) VALUES (?, ?, ?, ?, ?)",
            (row_id, date.today().isoformat(), amount, category, approved),
        )


def expense_entry(account, cash, amount, memo):
    ledger.create_journal_entry(
        date.today().isoformat(), memo, [
            {"account_id": account["id"], "debit_amount": amount},
            {"account_id": cash["id"], "credit_amount": amount},
        ], created_by="regression",
    )


def run():
    failures = 0
    db.init_db()
    failures += check("line cents preserves 0.29 x 3 exactly",
                      sku_service._line_cents("0.29", 3) == 87)
    failures += check("line cents preserves 0.1 x 3 exactly",
                      sku_service._line_cents("0.1", 3) == 30)
    failures += check("line cents preserves 0.15 x 7 exactly",
                      sku_service._line_cents("0.15", 7) == 105)
    by_code = {row["code"]: row for row in ledger.list_accounts()}
    ledger.set_account_tax_category(by_code["5000"]["id"], "supplies")
    ledger.set_account_tax_category(by_code["6070"]["id"], "rent_other")
    today = f"{date.today().isoformat()} 12:00:00"
    items = ('[{"product_id": 1, "variant_id": 2, "title": "Widget", '
             '"variant_title": "Blue", "quantity": 3, "price": "10.00", '
             '"total_discount": "2.00"}, {"product_id": 1, "variant_id": 3, '
             '"title": "Widget", "variant_title": "Default Title", "quantity": 1, '
             '"price": "8.00", "total_discount": "0.00"}]')
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO shopify_orders (order_id, created_at, total_price_cents, "
            "subtotal_price_cents, total_discounts_cents, total_tax_cents, "
            "refund_total_cents, "
            "financial_status, line_items_json) "
            "VALUES (?, ?, 3800, 3800, 200, 0, 0, ?, ?)",
            ("order-1", today, "paid", items),
        )
    metrics = sku_service.parse_sku_metrics(30)
    failures += check("SKU service has no Shopify API client dependency",
                      "shopify_client" not in sku_service.__dict__)
    failures += check("SKU metrics aggregate local line items",
                      metrics["blended"] == {
                          "total_units": 4,
                          "total_net_revenue_cents": 3600,
                          "blended_avg_sp_cents": 900,
                      } and metrics["per_sku"][0]["title"] == "Widget - Blue")

    precision_items = ('[{"product_id": 4, "variant_id": 5, '
                       '"title": "Precision Widget", "quantity": 3, '
                       '"price": "0.29", "total_discount": "0.00"}]')
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO shopify_orders (order_id, created_at, total_price_cents, "
            "subtotal_price_cents, total_discounts_cents, total_tax_cents, "
            "refund_total_cents, financial_status, line_items_json) "
            "VALUES (?, ?, 87, 87, 0, 0, 0, ?, ?)",
            ("precision-order", today, "paid", precision_items),
        )
    precision_metrics = sku_service.parse_sku_metrics(30)
    precision_sku = next(
        row for row in precision_metrics["per_sku"]
        if row["title"] == "Precision Widget"
    )
    failures += check(
        "Tax Planning includes exact revenue for precision-prone line items",
        precision_sku["gross_revenue_cents"] == 87
        and precision_sku["net_revenue_cents"] == 87
        and precision_metrics["blended"] == {
            "total_units": 7,
            "total_net_revenue_cents": 3687,
            "blended_avg_sp_cents": 526,
        },
    )

    legacy_transaction("supplies-1", "supplies", -2400)
    legacy_transaction("rent-1", "rent_other", -3000)
    expense_entry(by_code["5000"], by_code["1010"], "24.00", "Supplies")
    expense_entry(by_code["6070"], by_code["1010"], "30.00", "Rent")
    costs = sku_service.ledger_costs(30)
    with db.get_connection() as conn:
        legacy_variable = conn.execute(
            "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM kl_transactions "
            "WHERE user_approved = 1 AND amount_cents < 0 "
            "AND ai_category IN ('supplies', 'contract_labor')"
        ).fetchone()[0]
        legacy_fixed = conn.execute(
            "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM kl_transactions "
            "WHERE user_approved = 1 AND amount_cents < 0 "
            "AND ai_category = 'rent_other'"
        ).fetchone()[0]
    failures += check(
        "equivalent overlapping Wave and Ledger planning costs match exactly",
        costs["variable_cost_cents"] == legacy_variable == 2400
        and costs["fixed_cost_cents"] == legacy_fixed == 3000,
    )
    config = sku_service.get_units_inputs()
    config["source"] = "shopify"
    shopify_inputs = sku_service.resolve_sp_q_vc(
        config, metrics, costs["variable_cost_cents"])
    failures += check("Shopify source uses blended sales and Ledger variable costs",
                      shopify_inputs == {"sp_cents": 900, "q": 4,
                                         "vc_per_unit_cents": 600,
                                         "source_used": "shopify"})
    manual = {"source": "manual", "manual_sp_cents": 1500,
              "manual_q": 10, "manual_vc_cents": 400}
    failures += check("manual source uses entered unit inputs",
                      sku_service.resolve_sp_q_vc(manual, metrics) == {
                          "sp_cents": 1500, "q": 10,
                          "vc_per_unit_cents": 400, "source_used": "manual"})
    removed_auto = sku_service.resolve_sp_q_vc(
        {"source": "auto"}, metrics, costs["variable_cost_cents"])
    failures += check("removed Auto source cannot revive a legacy calculation",
                      removed_auto["source_used"] == "shopify")

    updated = tax.update_planning_config({"source": "manual", "manual_sp_cents": 1500,
                                           "manual_q": 10, "manual_vc_cents": 400,
                                           "target_income_cents": 5000,
                                           "date_range_days": 30})
    plan = tax.planning()
    failures += check("planning config persists requested values",
                      updated["source"] == "manual" and updated["manual_q"] == 10)
    failures += check("planning formula returns correct break-even outputs",
                      plan["inputs"] == {"sp_cents": 1500, "q": 10,
                                         "vc_per_unit_cents": 400, "source_used": "manual",
                                         "fc_cents": 3000, "target_income_cents": 5000}
                      and plan["cost_basis"]["source"] == "native_double_entry_ledger"
                      and plan["cost_basis"]["period"]["days"] == 30
                      and plan["outputs"]["contribution_margin_cents"] == 1100
                      and plan["outputs"]["breakeven_q"] == 3
                      and plan["outputs"]["target_q"] == 8
                      and plan["outputs"]["current_profit_cents"] == 8000
                      and plan["outputs"]["units_to_target"] == 0
                      and plan["outputs"]["min_sp_for_target_cents"] == 1200)
    try:
        tax.update_planning_config({"source": "auto"})
    except Exception as exc:
        auto_error = getattr(exc, "status_code", None) == 400
    else:
        auto_error = False
    failures += check("Planning API rejects the retired Auto source", auto_error)
    expense_entry(
        by_code["6060"], by_code["1010"], "9.00", "Unmapped contractor",
    )
    try:
        tax.planning()
    except Exception as exc:
        blocked = (
            getattr(exc, "status_code", None) == 409
            and isinstance(getattr(exc, "detail", None), dict)
            and exc.detail.get("code") == "tax_categorization_required"
            and "No planning amount is shown." in exc.detail.get("message", "")
        )
    else:
        blocked = False
    failures += check(
        "Planning refuses to show money when a used Ledger account is unresolved",
        blocked,
    )
    if failures:
        print(f"FAIL: {failures} Tax Planning checks failed.")
        return 1
    print("PASS: Tax Planning checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
