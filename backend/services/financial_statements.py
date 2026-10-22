"""Legacy Wave Tax-queue statements retained only as a regression oracle.

All production financial and tax output uses ``tax_financials`` and the
native double-entry Ledger. This module exists solely to reproduce the retired
Wave-backed calculation for frozen before/after fixtures.
"""

from collections import defaultdict
from datetime import date

import db


REVENUE_CATEGORIES = {"gross_receipts"}
RETURNS_CATEGORIES = {"returns_and_allowances"}
COGS_CATEGORIES = {"supplies", "contract_labor"}
EXCLUDED_CATEGORIES = {"owner_draw", "transfer"}


def _approved_rows(start_date=None, end_date=None):
    query = (
        "SELECT amount_cents, COALESCE(NULLIF(user_override_category, ''), "
        "ai_category, 'uncategorized') AS category "
        "FROM kl_transactions WHERE user_approved = 1"
    )
    args = []
    if start_date:
        query += " AND date >= ?"
        args.append(start_date)
    if end_date:
        query += " AND date <= ?"
        args.append(end_date)
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def _expense_rows(rows, categories=None):
    totals = defaultdict(int)
    for row in rows:
        if row["amount_cents"] >= 0:
            continue
        category = row["category"]
        if categories is None or category in categories:
            totals[category] += abs(int(row["amount_cents"]))
    return [{"category": category, "amount_cents": amount}
            for category, amount in sorted(totals.items(),
                                           key=lambda item: (-item[1], item[0]))]


def _income_statement(rows):
    gross_receipts = sum(max(0, int(row["amount_cents"])) for row in rows
                         if row["category"] in REVENUE_CATEGORIES)
    returns = sum(abs(int(row["amount_cents"])) for row in rows
                  if row["category"] in RETURNS_CATEGORIES)
    cogs_categories = _expense_rows(rows, COGS_CATEGORIES)
    cogs = sum(row["amount_cents"] for row in cogs_categories)
    operating_categories = _expense_rows(rows)
    operating_categories = [row for row in operating_categories
                            if row["category"] not in COGS_CATEGORIES
                            and row["category"] not in RETURNS_CATEGORIES
                            and row["category"] not in EXCLUDED_CATEGORIES]
    operating_expenses = sum(row["amount_cents"] for row in operating_categories)
    net_revenue = gross_receipts - returns
    gross_profit = net_revenue - cogs
    net_income = gross_profit - operating_expenses
    return {
        "income": {
            "gross_receipts_cents": gross_receipts,
            "returns_and_allowances_cents": returns,
            "net_revenue_cents": net_revenue,
        },
        "cost_of_goods_sold": {
            "categories": cogs_categories,
            "total_cents": cogs,
        },
        "gross_profit_cents": gross_profit,
        "gross_margin": gross_profit / net_revenue if net_revenue else 0,
        "operating_expenses": {
            "categories": operating_categories,
            "total_cents": operating_expenses,
        },
        "net_income_cents": net_income,
        "net_margin": net_income / net_revenue if net_revenue else 0,
        "approved_transaction_count": len(rows),
    }


def income_statement(start_date, end_date):
    """Return a cash-basis income statement for the requested inclusive period."""
    rows = _approved_rows(start_date, end_date)
    return {"period": {"start_date": start_date, "end_date": end_date},
            "basis": "approved_tax_transactions", **_income_statement(rows)}


def balance_sheet(as_of):
    """Return an honest partial position; the ledger cannot make a full balance sheet."""
    rows = _approved_rows(end_date=as_of)
    cash_in = sum(max(0, int(row["amount_cents"])) for row in rows)
    cash_out = sum(abs(min(0, int(row["amount_cents"]))) for row in rows)
    owner_draws = sum(abs(int(row["amount_cents"])) for row in rows
                      if row["category"] == "owner_draw" and row["amount_cents"] < 0)
    cumulative_income = _income_statement(rows)["net_income_cents"]
    return {
        "as_of": as_of,
        "basis": "approved_tax_transactions",
        "is_complete": False,
        "note": (
            "This is a cash-basis position from approved ledger movement, not a "
            "complete balance sheet. Opening balances, asset detail, and liabilities "
            "are not tracked in the Tax ledger yet."
        ),
        "cash_position": {
            "cash_in_cents": cash_in,
            "cash_out_cents": cash_out,
            "net_cash_movement_cents": cash_in - cash_out,
            "opening_cash_cents": None,
            "closing_cash_cents": None,
        },
        "equity_estimate": {
            "cumulative_net_income_cents": cumulative_income,
            "owner_draws_cents": owner_draws,
            "retained_earnings_after_draws_cents": cumulative_income - owner_draws,
        },
        "missing_components": [
            "opening cash balance", "asset account balances", "liabilities",
            "equity contributions",
        ],
        "approved_transaction_count": len(rows),
    }


def current_month_bounds():
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()
