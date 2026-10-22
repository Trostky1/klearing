"""Deterministic cross-source metrics built from Klearing's shared registry."""

from datetime import date
from decimal import Decimal

import crm
import db
import ledger_sync
from insight_metrics import (InsightMetric, make_insight_metric,
                             rolling_periods, value_delta)


def _metric(metrics, metric_id):
    return next((metric for metric in metrics if metric["id"] == metric_id), None)


def _shopify_link(path):
    import shopify_client
    store = shopify_client.get_store()
    return f"https://{store}{path}" if store else "https://admin.shopify.com/"


def _matched_payout_metric():
    """Shopify payouts whose exact settled Plaid deposit is identifiable."""
    transactions = db.list_bank_transactions_for_ledger()
    matched = []
    for payout in db.list_shopify_payouts():
        if (payout.get("status") or "").strip().lower() != "paid":
            continue
        transaction, _reason = ledger_sync.match_payout_to_bank(
            payout, transactions)
        if transaction is not None:
            matched.append(payout)

    points = []
    for start, end in rolling_periods():
        total = 0
        for payout in matched:
            try:
                payout_date = date.fromisoformat(payout["date"])
                amount = int(payout["amount_cents"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= payout_date <= end:
                total += amount
        points.append({
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": total,
        })
    value, delta = value_delta(points)
    return make_insight_metric(
        "shopify_payouts_matched",
        "Shopify payouts matched to bank",
        value,
        delta,
        "shopify",
        _shopify_link("/admin/settings/payments"),
        "currency_cents",
        points,
    )


def _inventory_margin_metric():
    """Gross margin only for periods where every posted order has COGS."""
    rows = crm.shopify_order_financials()
    windows = rolling_periods()
    points = []
    values_by_end = {}
    for start, end in windows:
        period_rows = []
        for row in rows:
            try:
                order_date = date.fromisoformat(row["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= order_date <= end and row["revenue_cents"] is not None:
                period_rows.append(row)

        # A partial margin is not a margin for this period. CRM can show a
        # covered subset with a disclosure; this headline metric must refuse.
        if (not period_rows
                or any(row["cogs_cents"] is None for row in period_rows)):
            continue
        revenue = sum(row["revenue_cents"] for row in period_rows)
        if revenue <= 0:
            continue
        margin = sum(row["margin_cents"] for row in period_rows)
        margin_pct = round(float(Decimal(margin * 100) / Decimal(revenue)), 2)
        point = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": margin_pct,
        }
        points.append(point)
        values_by_end[end.isoformat()] = margin_pct

    current_end = windows[-1][1].isoformat()
    prior_end = windows[-2][1].isoformat()
    value = values_by_end.get(current_end)
    prior = values_by_end.get(prior_end)
    delta = value - prior if value is not None and prior is not None else None
    return make_insight_metric(
        "inventory_gross_margin",
        "Inventory-backed gross margin",
        value,
        delta,
        "inventory",
        _shopify_link("/admin/orders"),
        "percent",
        points,
    )


def _expense_growth_metric(base_metrics):
    """Top Plaid expense growth minus Shopify revenue growth, in points."""
    revenue = _metric(base_metrics, "shopify_revenue")
    expense = _metric(base_metrics, "plaid_top_expense_category")
    generic_label = "Expense growth versus Shopify revenue"
    if not revenue or not expense or not expense["time_series"]:
        return make_insight_metric(
            "expense_growth_vs_revenue", generic_label,
            None, None, "plaid", "https://my.plaid.com/", "percent", [])

    revenue_by_period = {
        (point["period_start"], point["period_end"]): point["value"]
        for point in revenue["time_series"]
    }
    expense_points = expense["time_series"]
    points = []
    for index in range(1, len(expense_points)):
        previous_expense = expense_points[index - 1]
        current_expense = expense_points[index]
        previous_key = (previous_expense["period_start"],
                        previous_expense["period_end"])
        current_key = (current_expense["period_start"],
                       current_expense["period_end"])
        previous_revenue = revenue_by_period.get(previous_key)
        current_revenue = revenue_by_period.get(current_key)
        if (previous_expense["value"] <= 0 or previous_revenue is None
                or previous_revenue <= 0 or current_revenue is None):
            continue
        expense_growth = ((current_expense["value"]
                           - previous_expense["value"])
                          / previous_expense["value"] * 100)
        revenue_growth = ((current_revenue - previous_revenue)
                          / previous_revenue * 100)
        points.append({
            "period_start": current_expense["period_start"],
            "period_end": current_expense["period_end"],
            "value": round(expense_growth - revenue_growth, 2),
        })

    current_period_end = revenue["time_series"][-1]["period_end"]
    prior_period_end = revenue["time_series"][-2]["period_end"]
    values_by_end = {point["period_end"]: point["value"] for point in points}
    value = values_by_end.get(current_period_end)
    prior = values_by_end.get(prior_period_end)
    delta = value - prior if value is not None and prior is not None else None

    category = expense["label"].removeprefix("Top expense: ")
    if value is None:
        label = generic_label
    elif value > 0:
        label = f"{category} costs are climbing faster than Shopify revenue"
    elif value < 0:
        label = f"{category} costs are growing more slowly than Shopify revenue"
    else:
        label = f"{category} costs are moving with Shopify revenue"
    return make_insight_metric(
        "expense_growth_vs_revenue",
        label,
        value,
        delta,
        "plaid",
        "https://my.plaid.com/",
        "percent",
        points,
    )


def get_insight_metrics(base_metrics) -> list[InsightMetric]:
    """Return only pairings whose connected source metrics are present."""
    ids = {metric["id"] for metric in base_metrics}
    metrics = []
    if {"shopify_revenue", "plaid_top_expense_category"} <= ids:
        metrics.append(_matched_payout_metric())
        metrics.append(_expense_growth_metric(base_metrics))
    if {"shopify_revenue", "inventory_value"} <= ids:
        metrics.append(_inventory_margin_metric())
    return metrics
