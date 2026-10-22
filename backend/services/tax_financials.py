"""Tax-facing financial reports from Klearing's native double-entry Ledger.

This adapter is the only production bridge between Ledger reports and Tax
outputs.  It converts exact Decimal dollar amounts to integer cents, preserves
every nonzero revenue and expense account, and reports unresolved Schedule C
metadata instead of omitting or guessing it.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

import db
import ledger
import tax_categories


BASIS = "native_double_entry_ledger"


def _cents(value):
    """Convert an exact Ledger dollar amount to integer cents without rounding."""
    scaled = Decimal(value) * 100
    integral = scaled.to_integral_exact()
    if scaled != integral:
        raise ValueError("Ledger report contained a sub-cent amount")
    return int(integral)


def _entry_count(start_date=None, end_date=None):
    predicates = []
    values = []
    if start_date:
        predicates.append("date >= ?")
        values.append(start_date)
    if end_date:
        predicates.append("date <= ?")
        values.append(end_date)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM journal_entries" + where, values
        ).fetchone()
    return int(row["count"] or 0)


def _account_view(account):
    return {
        "account_id": account["id"],
        "code": account["code"],
        "name": account["name"],
        "type": account["type"],
        "tax_category": account["tax_category"],
        "debit_cents": _cents(account["debit_total"]),
        "credit_cents": _cents(account["credit_total"]),
        "amount_cents": _cents(account["amount"]),
    }


def _unresolved_accounts(report):
    unresolved = []
    accounts = report["revenue"] + report["cogs"] + report["operating_expenses"]
    for account in accounts:
        debit_cents = _cents(account["debit_total"])
        credit_cents = _cents(account["credit_total"])
        if account["tax_category"] is None and (debit_cents or credit_cents):
            unresolved.append(_account_view(account))
    return sorted(unresolved, key=lambda row: (row["code"], row["account_id"]))


def _group_expenses(accounts):
    grouped = defaultdict(int)
    for account in accounts:
        amount_cents = _cents(account["amount"])
        if amount_cents == 0:
            continue
        category = account["tax_category"]
        key = category or f"unresolved:{account['id']}"
        grouped[key] += amount_cents

    rows = []
    for key, amount_cents in grouped.items():
        if key.startswith("unresolved:"):
            account_id = int(key.split(":", 1)[1])
            account = next(row for row in accounts if row["id"] == account_id)
            rows.append({
                "category": None,
                "label": f"Needs categorization — {account['code']} {account['name']}",
                "amount_cents": amount_cents,
                "account_ids": [account_id],
            })
        else:
            account_ids = [
                row["id"] for row in accounts
                if row["tax_category"] == key and _cents(row["amount"]) != 0
            ]
            rows.append({
                "category": key,
                "label": tax_categories.CATEGORY_LABELS[key],
                "amount_cents": amount_cents,
                "account_ids": account_ids,
            })
    return sorted(rows, key=lambda row: (-row["amount_cents"], row["label"]))


def income_statement(start_date, end_date):
    """Return a complete native-Ledger P&L plus explicit tax-map readiness."""
    report = ledger.profit_and_loss(start_date, end_date)
    unresolved = _unresolved_accounts(report)
    gross_receipts = sum(
        _cents(row["amount"]) for row in report["revenue"]
        if row["tax_category"] == "gross_receipts"
    )
    # Contra-revenue has a debit-normal (negative revenue) balance. Preserve
    # reversals by negating the signed balance rather than applying abs().
    returns = -sum(
        _cents(row["amount"]) for row in report["revenue"]
        if row["tax_category"] == "returns_and_allowances"
    )
    net_revenue = _cents(report["revenue_total"])
    gross_profit = _cents(report["gross_profit"])
    operating_total = _cents(report["operating_expenses_total"])
    net_income = _cents(report["net_income"])
    return {
        "period": {"start_date": report["start_date"], "end_date": report["end_date"]},
        "basis": BASIS,
        "source_entry_count": _entry_count(report["start_date"], report["end_date"]),
        "is_tax_complete": not unresolved,
        "unresolved_account_count": len(unresolved),
        "unresolved_accounts": unresolved,
        "income": {
            "gross_receipts_cents": gross_receipts,
            "returns_and_allowances_cents": returns,
            "unresolved_revenue_cents": net_revenue - gross_receipts + returns,
            "net_revenue_cents": net_revenue,
        },
        "revenue_accounts": [_account_view(row) for row in report["revenue"]],
        "cost_of_goods_sold": {
            "categories": _group_expenses(report["cogs"]),
            "accounts": [_account_view(row) for row in report["cogs"]],
            "total_cents": _cents(report["cogs_total"]),
        },
        "gross_profit_cents": gross_profit,
        "gross_margin": gross_profit / net_revenue if net_revenue else 0,
        "operating_expenses": {
            "categories": _group_expenses(report["operating_expenses"]),
            "accounts": [_account_view(row) for row in report["operating_expenses"]],
            "total_cents": operating_total,
        },
        "net_income_cents": net_income,
        "net_margin": net_income / net_revenue if net_revenue else 0,
    }


def _balance_accounts(accounts):
    return [
        {
            "account_id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "amount_cents": _cents(row["amount"]),
        }
        for row in accounts
    ]


def balance_sheet(as_of):
    """Return the native Ledger balance sheet without invented cash figures."""
    report = ledger.balance_sheet(as_of)
    return {
        "as_of": report["as_of_date"],
        "basis": BASIS,
        "source_entry_count": _entry_count(end_date=report["as_of_date"]),
        "is_complete": True,
        "is_balanced": report["is_balanced"],
        "note": (
            "Balances come from the native double-entry Ledger through this date. "
            "Current earnings are shown separately because closing entries are not yet posted."
        ),
        "assets": _balance_accounts(report["assets"]),
        "assets_total_cents": _cents(report["assets_total"]),
        "liabilities": _balance_accounts(report["liabilities"]),
        "liabilities_total_cents": _cents(report["liabilities_total"]),
        "equity_accounts": _balance_accounts(report["equity_accounts"]),
        "equity_accounts_total_cents": _cents(report["equity_accounts_total"]),
        "current_earnings_cents": _cents(report["current_earnings"]),
        "equity_total_cents": _cents(report["equity_total"]),
        "difference_cents": _cents(report["difference"]),
    }


def current_month_bounds():
    today = date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def categorization_required_detail(statement, output_label="projection",
                                   consequence="No estimate is shown."):
    count = statement["unresolved_account_count"]
    noun = "account" if count == 1 else "accounts"
    verb = "needs" if count == 1 else "need"
    return {
        "code": "tax_categorization_required",
        "message": (
            f"{count} Ledger {noun} {verb} categorization before this "
            f"{output_label} can be completed. {consequence}"
        ),
        "unresolved_account_count": count,
        "unresolved_accounts": statement["unresolved_accounts"],
        "action_url": "/ledger?tab=accounts",
    }
