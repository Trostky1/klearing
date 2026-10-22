"""Manual Phase 1 ledger sync and financial-statement verification report.

This is intentionally a read/debug script. The only operation that changes
ledger state is the requested ``sync_ledger_from_sources`` call itself.
"""

import sys
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import inventory  # noqa: E402
import ledger  # noqa: E402
import ledger_sync  # noqa: E402


def money(value):
    """Render a Decimal amount as dollars, including a useful negative sign."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}"


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def label_reason(reason):
    return reason.replace("_", " ")


def bank_skip_reasons():
    reasons = Counter()
    for transaction in db.list_bank_transactions_for_ledger():
        amount_cents = int(transaction["amount_cents"])
        if amount_cents == 0:
            reasons["zero amount"] += 1
        elif amount_cents > 0 and ledger_sync._is_shopify_payout(transaction):
            reasons["Shopify payout handled with payout matching"] += 1
    return reasons


def shopify_order_skip_reasons():
    reasons = Counter()
    for order in db.list_shopify_orders():
        status = (order.get("financial_status") or "").strip().lower()
        amount = ledger_sync._shopify_net_amount(order)
        if status in ledger_sync.SKIPPED_SHOPIFY_STATUSES:
            reasons[f"status: {status}"] += 1
        elif amount <= 0:
            reasons["zero or negative net order"] += 1
    return reasons


def persisted_skips():
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT source, reason, COUNT(*) AS count "
            "FROM ledger_source_import_skips "
            "GROUP BY source, reason ORDER BY source, reason"
        ).fetchall()]


def print_sync_summary(summary):
    section("SYNC SUMMARY")
    print(f"New journal entries created: {summary['created']}")
    print()
    print("Bank transactions and transfers:")
    print(f"  Created: {summary['plaid']['created']}")
    print(f"  Already imported: {summary['plaid']['already_imported']}")
    print(f"  Skipped: {summary['plaid']['skipped']}")
    for reason, count in bank_skip_reasons().items():
        print(f"    - {reason}: {count}")

    print("Shopify orders:")
    print(f"  Created: {summary['shopify']['created']}")
    print(f"  Already imported: {summary['shopify']['already_imported']}")
    print(f"  Skipped: {summary['shopify']['skipped']}")
    for reason, count in shopify_order_skip_reasons().items():
        print(f"    - {reason}: {count}")

    print("Shopify payouts:")
    print(f"  Created: {summary['shopify_payouts']['created']}")
    print(f"  Already imported: {summary['shopify_payouts']['already_imported']}")
    print(f"  Unmatched/skipped: {summary['shopify_payouts']['unmatched']}")
    for item in summary["shopify_payouts"]["unmatched_payouts"]:
        print(f"    - {item['payout_id']}: {label_reason(item['reason'])}")

    print("Shopify COGS:")
    print(f"  Created: {summary['shopify_cogs']['created']}")
    print(f"  Already imported: {summary['shopify_cogs']['already_imported']}")
    print(f"  Skipped: {summary['shopify_cogs']['skipped']}")
    cogs_reasons = Counter(item["reason"]
                           for item in summary["shopify_cogs"]["skipped_orders"])
    for reason, count in cogs_reasons.items():
        print(f"    - {label_reason(reason)}: {count}")

    # This includes durable details for skips recorded by the sync service,
    # which is useful when the reason needs more context than a count.
    details = persisted_skips()
    if details:
        print("Persisted skip counts:")
        for row in details:
            print(f"  {row['source']}: {label_reason(row['reason'])} - {row['count']}")


def print_trial_balance():
    section("TRIAL BALANCE")
    print(f"{'Code':<8} {'Account':<32} {'Type':<10} {'Debit':>15} {'Credit':>15} {'Balance':>15}")
    print("-" * 99)
    rows = ledger.trial_balance()
    for row in rows:
        print(f"{row['code']:<8} {row['name'][:32]:<32} {row['type']:<10} "
              f"{money(row['debit_total']):>15} {money(row['credit_total']):>15} "
              f"{money(row['normal_balance']):>15}")
    debit = sum((row["debit_total"] for row in rows), Decimal("0"))
    credit = sum((row["credit_total"] for row in rows), Decimal("0"))
    print("-" * 99)
    print(f"{'TOTAL':<52} {money(debit):>15} {money(credit):>15}")


def print_pnl(report):
    section(f"PROFIT & LOSS ({report['start_date']} through {report['end_date']})")
    for title, key, total_key in (
        ("Revenue", "revenue", "revenue_total"),
        ("Cost of Goods Sold", "cogs", "cogs_total"),
        ("Operating Expenses", "operating_expenses", "operating_expenses_total"),
    ):
        print(title)
        for row in report[key]:
            print(f"  {row['code']} - {row['name']}: {money(row['amount'])}")
        print(f"  {title} total: {money(report[total_key])}\n")
    print(f"Gross profit: {money(report['gross_profit'])}")
    print(f"Net income:   {money(report['net_income'])}")


def print_balance_sheet(report):
    section(f"BALANCE SHEET (as of {report['as_of_date']})")
    for title, key, total_key in (
        ("Assets", "assets", "assets_total"),
        ("Liabilities", "liabilities", "liabilities_total"),
        ("Equity accounts", "equity_accounts", "equity_accounts_total"),
    ):
        print(title)
        for row in report[key]:
            print(f"  {row['code']} - {row['name']}: {money(row['amount'])}")
        print(f"  {title} total: {money(report[total_key])}\n")
    print(f"Current earnings: {money(report['current_earnings'])}")
    print(f"Total equity:    {money(report['equity_total'])}")
    print(f"Assets:          {money(report['assets_total'])}")
    print(f"Liabilities + Equity: {money(report['liabilities_total'] + report['equity_total'])}")
    print(f"Difference:      {money(report['difference'])}")
    result = "PASS" if report["is_balanced"] else "FAIL"
    print(f"\nAssets = Liabilities + Equity: {result}")


def print_inventory_and_cogs(summary):
    """COGS coverage, the duplicate-post guard, and stock on hand.

    The duplicate check is the one that matters most: a second COGS entry
    for the same order would double the expense and understate inventory,
    and nothing in the balance sheet would look wrong because both sides
    would still foot.
    """
    section("INVENTORY AND COST OF GOODS")
    print(f"COGS entries posted this run: {summary['shopify_cogs']['created']}")
    print(f"Already posted (skipped):     {summary['shopify_cogs']['already_imported']}")
    print(f"Held back:                    {summary['shopify_cogs']['skipped']}")

    reasons = Counter(item["reason"]
                      for item in summary["shopify_cogs"]["skipped_orders"])
    for reason, count in sorted(reasons.items()):
        print(f"    - {label_reason(reason)}: {count}")

    with db.get_connection() as conn:
        posted = conn.execute(
            "SELECT COUNT(*) FROM ledger_source_imports "
            "WHERE source_reference LIKE 'shopify:order:%:cogs'").fetchone()[0]
        duplicates = conn.execute(
            "SELECT source_reference, COUNT(*) AS n FROM ledger_source_imports "
            "WHERE source_reference LIKE 'shopify:order:%:cogs' "
            "GROUP BY source_reference HAVING n > 1").fetchall()
    print()
    print(f"Total orders with a COGS entry: {posted}")
    result = "PASS" if not duplicates else "FAIL"
    print(f"No order has more than one COGS entry: {result}")
    for row in duplicates:
        print(f"    DUPLICATE: {row['source_reference']} x{row['n']}")

    position = inventory.inventory_position()
    print()
    print(f"{'Item':<38} {'On hand':>9} {'Avg cost':>12} {'Value':>14}")
    print("-" * 76)
    for item in position["items"]:
        avg = money(item["average_cost"]) if item["average_cost"] is not None else "-"
        print(f"{item['title'][:38]:<38} {item['on_hand']:>9} "
              f"{avg:>12} {money(item['value']):>14}")
    print("-" * 76)
    print(f"{'TOTAL':<38} {'':>9} {'':>12} {money(position['total_value']):>14}")


def print_analytics_wall():
    """Confirm the historical import stayed out of the ledger.

    An explicit negative check. This is the constraint most worth
    guarding against regression, because nothing else would notice: the
    analytics table could quietly start posting and every report would
    still balance, just with invented entries in it.
    """
    section("SHOPIFY HISTORICAL IMPORT (NON-LEDGER)")
    rows = db.shopify_analytics_summary()
    if not rows:
        print("No historical analytics imported.")
    for row in rows:
        print(f"  {row['metric_type']:<18} {row['period']:<6} "
              f"{row['rows']:>5} rows  {row['first_period']} .. {row['last_period']}")

    with db.get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM shopify_analytics_history").fetchone()[0]
        leaked = conn.execute(
            "SELECT COUNT(*) FROM ledger_source_imports "
            "WHERE source_reference LIKE '%analytics%'").fetchone()[0]
    print()
    print(f"Analytics rows stored:                {total}")
    print(f"Ledger imports referencing analytics: {leaked}")
    print(f"Analytics stayed out of the ledger:   {'PASS' if leaked == 0 else 'FAIL'}")


def main():
    summary = ledger_sync.sync_ledger_from_sources()
    today = date.today()
    start = today - timedelta(days=89)
    print_sync_summary(summary)
    print_trial_balance()
    print_pnl(ledger.profit_and_loss(start.isoformat(), today.isoformat()))
    balance_sheet = ledger.balance_sheet(today.isoformat())
    print_balance_sheet(balance_sheet)
    print_inventory_and_cogs(summary)
    print_analytics_wall()
    return 0 if balance_sheet["is_balanced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
