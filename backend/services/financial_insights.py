"""Deterministic business insights from the native double-entry Ledger."""

import calendar
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import db
from backend.services import tax_financials


PAYROLL_CATEGORIES = {"wages", "employee_benefits"}


def _month_windows(as_of, months):
    windows = []
    year, month = as_of.year, as_of.month
    for offset in reversed(range(months)):
        target_month = month - offset
        target_year = year
        while target_month <= 0:
            target_month += 12
            target_year -= 1
        last_day = calendar.monthrange(target_year, target_month)[1]
        end_day = min(last_day, as_of.day) if offset == 0 else last_day
        start = date(target_year, target_month, 1)
        end = date(target_year, target_month, end_day)
        windows.append((start.strftime("%b"), start.isoformat(), end.isoformat()))
    return windows


def _amount_due_cents(value):
    try:
        cents = int((Decimal(str(value).split()[0]) * 100).quantize(
            Decimal("1")))
        return max(0, cents)
    except (InvalidOperation, IndexError, ValueError):
        return 0


def _expense_total(statement):
    return (statement["cost_of_goods_sold"]["total_cents"]
            + statement["operating_expenses"]["total_cents"])


def empty_snapshot(as_of=None, months=6, window_days=90):
    """A calm renderer fallback with the same shape as a real snapshot."""
    as_of = as_of or date.today()
    window_start = as_of - timedelta(days=window_days - 1)
    return {
        "as_of": as_of.isoformat(),
        "window": {
            "start_date": window_start.isoformat(),
            "end_date": as_of.isoformat(),
            "days": window_days,
        },
        "source_entry_count": 0,
        "daily_burn_cents": 0,
        "monthly_burn_cents": 0,
        "monthly_revenue_cents": 0,
        "monthly_expenses_cents": 0,
        "payroll_cents": None,
        "payroll_ratio_pct": None,
        "ltv_cents": None,
        "cac_cents": None,
        "new_customer_count": 0,
        "overdue_receivables_cents": 0,
        "tracked_open_invoice_count": 0,
        "dso_days": None,
        "trends": [
            {
                "label": label, "start_date": start, "end_date": end,
                "net_revenue_cents": 0, "expenses_cents": 0,
                "gross_margin_pct": 0, "net_margin_pct": 0,
            }
            for label, start, end in _month_windows(as_of, months)
        ],
        "basis": tax_financials.BASIS,
        "category_metrics_complete": True,
    }


def financial_snapshot(as_of=None, months=6, window_days=90):
    """Return financial KPIs without inferring data Klearing does not hold."""
    as_of = as_of or date.today()
    window_start = as_of - timedelta(days=window_days - 1)
    statement = tax_financials.income_statement(
        window_start.isoformat(), as_of.isoformat())
    net_revenue = statement["income"]["net_revenue_cents"]
    expenses = _expense_total(statement)
    daily_burn = round(expenses / window_days)

    category_rows = (statement["cost_of_goods_sold"]["categories"]
                     + statement["operating_expenses"]["categories"])
    category_totals = {}
    for row in category_rows:
        if row["category"] is not None:
            category_totals[row["category"]] = (
                category_totals.get(row["category"], 0)
                + row["amount_cents"]
            )
    category_metrics_complete = statement["is_tax_complete"]
    payroll = (sum(category_totals.get(category, 0)
                   for category in PAYROLL_CATEGORIES)
               if category_metrics_complete else None)
    advertising = (category_totals.get("advertising", 0)
                   if category_metrics_complete else None)

    trends = []
    for label, start, end in _month_windows(as_of, months):
        monthly = tax_financials.income_statement(start, end)
        trends.append({
            "label": label,
            "start_date": start,
            "end_date": end,
            "net_revenue_cents": monthly["income"]["net_revenue_cents"],
            "expenses_cents": _expense_total(monthly),
            "gross_margin_pct": monthly["gross_margin"] * 100,
            "net_margin_pct": monthly["net_margin"] * 100,
        })

    customers = db.list_shopify_customers()
    paying_customers = [row for row in customers
                        if Decimal(str(row.get("total_spent") or 0)) > 0]
    ltv_cents = None
    if paying_customers:
        ltv_cents = round(
            sum(Decimal(str(row["total_spent"]))
                for row in paying_customers)
            / len(paying_customers) * 100)
    new_customers = sum(
        1 for row in customers
        if row.get("created_at")
        and window_start.isoformat() <= row["created_at"][:10] <= as_of.isoformat()
    )
    cac_cents = (round(advertising / new_customers)
                 if advertising is not None and new_customers else None)

    latest_invoices = {}
    for row in db.list_invoice_reminders():
        invoice_id = row.get("wave_invoice_id")
        if not invoice_id:
            continue
        current = latest_invoices.get(invoice_id)
        if (current is None
                or int(row.get("id") or 0) > int(current.get("id") or 0)):
            latest_invoices[invoice_id] = row
    open_invoices = [row for row in latest_invoices.values()
                     if row.get("status") != "paid"]
    overdue_receivables = sum(_amount_due_cents(row.get("amount_due"))
                              for row in open_invoices)
    dso_days = None
    if open_invoices and net_revenue > 0:
        dso_days = overdue_receivables / net_revenue * window_days

    return {
        "as_of": as_of.isoformat(),
        "window": {
            "start_date": window_start.isoformat(),
            "end_date": as_of.isoformat(),
            "days": window_days,
        },
        "source_entry_count": statement["source_entry_count"],
        "daily_burn_cents": daily_burn,
        "monthly_burn_cents": daily_burn * 30,
        "monthly_revenue_cents": round(net_revenue / window_days * 30),
        "monthly_expenses_cents": round(expenses / window_days * 30),
        "payroll_cents": payroll,
        "payroll_ratio_pct": (payroll / net_revenue * 100
                              if payroll is not None and net_revenue > 0
                              else None),
        "ltv_cents": ltv_cents,
        "cac_cents": cac_cents,
        "new_customer_count": new_customers,
        "overdue_receivables_cents": overdue_receivables,
        "tracked_open_invoice_count": len(open_invoices),
        "dso_days": dso_days,
        "trends": trends,
        "basis": tax_financials.BASIS,
        "category_metrics_complete": category_metrics_complete,
    }


def apply_scenario(snapshot, revenue_change_pct=0, expense_change_pct=0,
                   one_time_cost_cents=0, cash_balance_cents=None):
    """Apply a reversible one-month scenario to the observed baseline."""
    revenue = max(0, round(snapshot["monthly_revenue_cents"]
                           * (1 + revenue_change_pct / 100)))
    recurring_expenses = max(0, round(snapshot["monthly_expenses_cents"]
                                      * (1 + expense_change_pct / 100)))
    one_time_cost_cents = max(0, int(one_time_cost_cents))
    total_expenses = recurring_expenses + one_time_cost_cents
    net_income = revenue - total_expenses
    margin_pct = net_income / revenue * 100 if revenue else None
    runway_days = None
    if cash_balance_cents is not None and recurring_expenses > 0:
        remaining_cash = max(0, cash_balance_cents - one_time_cost_cents)
        runway_days = remaining_cash / (recurring_expenses / 30)
    return {
        "revenue_cents": revenue,
        "recurring_expenses_cents": recurring_expenses,
        "one_time_cost_cents": one_time_cost_cents,
        "total_expenses_cents": total_expenses,
        "net_income_cents": net_income,
        "margin_pct": margin_pct,
        "runway_days": runway_days,
    }
