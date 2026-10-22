"""Local-only SKU and native-Ledger inputs for Tax Planning."""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import db
from backend.services import tax_financials


VARIABLE_COST_CATEGORIES = ("supplies", "contract_labor")
FIXED_COST_CATEGORIES = (
    "rent_other", "rent_vehicles_machinery", "utilities", "insurance",
    "wages", "employee_benefits", "interest_mortgage", "interest_other",
)
_CENT = Decimal("0.01")


def _integer(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _line_cents(value, quantity=1):
    try:
        amount = Decimal(str(value or 0))
        rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
        if not amount.is_finite() or rounded != amount:
            return 0
        return int(rounded * 100) * int(quantity)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return 0


def date_range_bounds(date_range_days=30):
    """Return an exact inclusive planning period ending today."""
    days = max(1, _integer(date_range_days) or 30)
    end = date.today()
    start = end - timedelta(days=days - 1)
    return days, start.isoformat(), end.isoformat()


def parse_sku_metrics(date_range_days=30):
    """Aggregate the locally synced Shopify line items without API access."""
    days, start, end = date_range_bounds(date_range_days)
    day_after_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    with db.get_connection() as conn:
        orders = conn.execute(
            "SELECT line_items_json FROM shopify_orders WHERE created_at >= ? "
            "AND created_at < ? "
            "AND financial_status IN ('paid', 'partially_refunded')",
            (start, day_after_end),
        ).fetchall()
    if not orders:
        return {
            "per_sku": [],
            "blended": {"total_units": 0, "total_net_revenue_cents": 0,
                        "blended_avg_sp_cents": 0},
            "date_range_days": days,
            "source": "no_data",
        }

    grouped = {}
    for order in orders:
        try:
            line_items = json.loads(order["line_items_json"])
        except (TypeError, ValueError):
            line_items = []
        for item in line_items if isinstance(line_items, list) else []:
            product_id = str(item.get("product_id") or "")
            variant_id = str(item.get("variant_id") or "")
            sku_key = f"{product_id}:{variant_id}"
            title = str(item.get("title") or "Untitled product")
            variant_title = item.get("variant_title")
            if variant_title and variant_title != "Default Title":
                title = f"{title} - {variant_title}"
            entry = grouped.setdefault(sku_key, {
                "sku_key": sku_key, "title": title, "units_sold": 0,
                "gross_revenue_cents": 0, "discounts_cents": 0,
            })
            quantity = max(0, _integer(item.get("quantity")))
            entry["units_sold"] += quantity
            entry["gross_revenue_cents"] += _line_cents(item.get("price"), quantity)
            entry["discounts_cents"] += _line_cents(item.get("total_discount"))

    per_sku = []
    for entry in grouped.values():
        net = entry["gross_revenue_cents"] - entry["discounts_cents"]
        units = entry["units_sold"]
        per_sku.append({**entry, "net_revenue_cents": net,
                        "avg_sp_cents": net // units if units else 0})
    per_sku.sort(key=lambda row: (-row["net_revenue_cents"], row["title"]))
    total_units = sum(row["units_sold"] for row in per_sku)
    total_net = sum(row["net_revenue_cents"] for row in per_sku)
    return {
        "per_sku": per_sku,
        "blended": {
            "total_units": total_units,
            "total_net_revenue_cents": total_net,
            "blended_avg_sp_cents": total_net // total_units if total_units else 0,
        },
        "date_range_days": days,
        "source": "shopify",
    }


def get_units_inputs():
    """Return the single Planning configuration row, creating defaults once."""
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO kl_units_config (id, source) "
            "VALUES (1, 'shopify')"
        )
        row = conn.execute("SELECT * FROM kl_units_config WHERE id = 1").fetchone()
    return dict(row)


def ledger_costs(date_range_days=30):
    """Return exact period-matched planning costs from categorized Ledger data.

    The statement is returned with the totals so the router can fail closed
    when any used revenue or expense account remains unresolved.
    """
    days, start, end = date_range_bounds(date_range_days)
    statement = tax_financials.income_statement(start, end)
    categories = (
        statement["cost_of_goods_sold"]["categories"]
        + statement["operating_expenses"]["categories"]
    )

    def total(selected):
        return sum(
            row["amount_cents"] for row in categories
            if row["category"] in selected
        )

    return {
        "period": {"days": days, "start_date": start, "end_date": end},
        "variable_cost_cents": total(VARIABLE_COST_CATEGORIES),
        "fixed_cost_cents": total(FIXED_COST_CATEGORIES),
        "statement": statement,
    }


def resolve_sp_q_vc(config, sku_metrics, variable_cost_cents=0):
    """Resolve selling price, units, and variable cost for the selected source."""
    source = config.get("source") if config.get("source") in {
        "shopify", "manual"} else "shopify"
    if source == "manual":
        return {"sp_cents": _integer(config.get("manual_sp_cents")),
                "q": _integer(config.get("manual_q")),
                "vc_per_unit_cents": _integer(config.get("manual_vc_cents")),
                "source_used": "manual"}
    blended = sku_metrics["blended"]
    units = _integer(blended.get("total_units"))
    variable_cents = _integer(variable_cost_cents)
    return {"sp_cents": _integer(blended.get("blended_avg_sp_cents")),
            "q": units,
            "vc_per_unit_cents": variable_cents // units if units else 0,
            "source_used": "shopify"}
