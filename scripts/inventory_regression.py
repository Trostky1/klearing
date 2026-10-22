"""Offline regression coverage for inventory FIFO costing and the
historical Shopify analytics import.

Two things are being guarded here, and they are guarded for different
reasons:

1. FIFO costing decides real money. The tests below pin the behaviours
   that were chosen deliberately — cost as of the sale date, never
   today's; refuse rather than part-cost; refuse rather than guess.
2. The analytics import must never reach the ledger. That is asserted
   negatively, by counting journal_entries before and after, because it
   is the constraint most likely to be broken by a well-meaning later
   change.

Isolated temp data dir, no network, no credentials.
"""

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DATA = tempfile.TemporaryDirectory(prefix="klearing-inventory-",
                                    ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db  # noqa: E402
import inventory  # noqa: E402
import ledger  # noqa: E402
import shopify_analytics  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def journal_entry_count():
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]


def reset_variant(variant_id):
    with db.get_connection() as conn:
        conn.execute("DELETE FROM inventory_quantity_events WHERE variant_id = ?",
                     (variant_id,))


# ------------------------------------------------------------ A: FIFO


def check_fifo_costing():
    reset_variant("v1")
    # Two layers at different prices: 10 @ $4.00 then 10 @ $6.00.
    inventory.record_acquisition("v1", 10, "4.00", "2026-01-01", funding_account_code="1010")
    inventory.record_acquisition("v1", 10, "6.00", "2026-02-01", funding_account_code="1010")

    total, reason = inventory.cost_of_sale("v1", 5, "2026-03-01")
    check("FIFO takes the oldest layer first",
          total == 2000 and reason is None, f"got {total}, {reason}")

    total, _ = inventory.cost_of_sale("v1", 15, "2026-03-01")
    check("FIFO spans layers when one is not enough",
          total == 10 * 400 + 5 * 600, f"got {total}")

    total, _ = inventory.cost_of_sale("v1", 20, "2026-03-01")
    check("FIFO consumes every layer exactly",
          total == 10 * 400 + 10 * 600, f"got {total}")

    # Constraint 1: never apply a later cost to an earlier sale.
    total, _ = inventory.cost_of_sale("v1", 5, "2026-01-15")
    check("a sale before the second restock cannot use its price",
          total == 5 * 400, f"got {total}")

    total, reason = inventory.cost_of_sale("v1", 15, "2026-01-15")
    check("a sale cannot draw on stock acquired after it",
          total is None and "cover 10 of 15" in (reason or ""),
          f"got {total}, {reason}")


def check_refusals():
    reset_variant("v2")
    total, reason = inventory.cost_of_sale("v2", 1, "2026-03-01")
    check("untracked variant refuses rather than costing zero",
          total is None and reason is not None, f"got {total}, {reason}")

    inventory.record_acquisition("v2", 8, "5.00", "2026-01-01", funding_account_code="1010")
    # Constraint 2: partial coverage is a refusal, not a part-cost.
    total, reason = inventory.cost_of_sale("v2", 10, "2026-03-01")
    check("partial coverage refuses the whole line",
          total is None and "cover 8 of 10" in (reason or ""),
          f"got {total}, {reason}")

    total, _ = inventory.cost_of_sale("v2", 8, "2026-03-01")
    check("exact coverage is accepted", total == 4000, f"got {total}")


def check_money_discipline():
    reset_variant("v3")
    for bad in ("1.005", "0.001", "12.345"):
        try:
            inventory.record_acquisition("v3", 1, bad, "2026-01-01", funding_account_code="1010")
            check(f"rejects sub-cent cost {bad}", False, "accepted it")
        except inventory.InventoryError:
            check(f"rejects sub-cent cost {bad}", True)
    try:
        inventory.record_acquisition("v3", 1, "-5.00", "2026-01-01", funding_account_code="1010")
        check("rejects negative cost", False, "accepted it")
    except inventory.InventoryError:
        check("rejects negative cost", True)
    try:
        inventory.record_acquisition("v3", 1, "5.00", "01/01/2026", funding_account_code="1010")
        check("rejects non-ISO date", False, "accepted it")
    except inventory.InventoryError:
        check("rejects non-ISO date", True)

    inventory.record_acquisition("v3", 3, "19.99", "2026-01-01", funding_account_code="1010")
    total, _ = inventory.cost_of_sale("v3", 3, "2026-02-01")
    check("cents are exact, no float drift", total == 5997, f"got {total}")
    check("Decimal at the boundary",
          inventory.cents_to_amount(5997) == Decimal("59.97"))


def check_idempotency():
    reset_variant("v4")
    inventory.record_acquisition("v4", 10, "2.00", "2026-01-01", funding_account_code="1010")
    first = inventory.record_sale("v4", 4, "2026-02-01", "shopify:order:X:cogs")
    second = inventory.record_sale("v4", 4, "2026-02-01", "shopify:order:X:cogs")
    check("a sale event with the same source reference is written once",
          first is True and second is False, f"{first}, {second}")

    events = db.list_inventory_events("v4")
    check("only one sale event exists",
          sum(1 for e in events if e["event_type"] == "sale") == 1)

    # Re-costing the same sale must not see its own outflow, or a re-run
    # would refuse entries it had already posted.
    total, reason = inventory.cost_of_sale(
        "v4", 4, "2026-02-01", exclude_source_reference="shopify:order:X:cogs")
    check("re-costing a recorded sale excludes its own outflow",
          total == 800 and reason is None, f"got {total}, {reason}")

    # A different sale does see it.
    total, _ = inventory.cost_of_sale("v4", 6, "2026-02-02")
    check("a later sale sees stock already consumed", total == 1200, f"got {total}")
    total, reason = inventory.cost_of_sale("v4", 7, "2026-02-02")
    check("and refuses once the remainder runs out",
          total is None and "cover 6 of 7" in (reason or ""), f"{total}, {reason}")


def check_position_report():
    reset_variant("v5")
    inventory.record_acquisition("v5", 10, "3.00", "2026-01-01", funding_account_code="1010")
    inventory.record_sale("v5", 4, "2026-02-01", "shopify:order:Y:cogs")
    inventory.record_adjustment("v5", -1, "2026-02-05", note="breakage")

    position = inventory.inventory_position("2026-03-01")
    item = next(i for i in position["items"] if i["variant_id"] == "v5")
    check("stock on hand nets sales and adjustments",
          item["on_hand"] == 5, f'got {item["on_hand"]}')
    check("value follows remaining layers",
          item["value"] == Decimal("15.00"), f'got {item["value"]}')
    check("report shape matches the ledger reports",
          {"as_of", "items", "total_value", "tracked_variants"}
          <= set(position))


def check_accounting_bridge():
    reset_variant("v6")
    inserted = db.record_inventory_event({
        "variant_id": "v6", "event_type": "acquisition", "quantity": 4,
        "unit_cost_cents": 750, "effective_date": "2025-01-01",
        "source": "manual", "source_reference": "legacy:v6",
        "note": "old quantity-only row", "created_at": "2026-01-01T00:00:00",
        "accounting_status": "legacy_unposted",
    })
    total, reason = inventory.cost_of_sale("v6", 1, "2025-02-01")
    check("legacy quantity-only rows cannot cover COGS before reconciliation",
          inserted and total is None and "cover 0 of 1" in (reason or ""))
    queued = {row["id"] for row in db.list_inventory_reconciliation_queue()}
    event_id = next(iter(queued))
    inventory.reconcile_acquisition(event_id, "1010")
    total, reason = inventory.cost_of_sale("v6", 1, "2025-02-01")
    check("reconciliation posts the FIFO layer and enables dated COGS",
          total == 750 and reason is None)
    check("reconciled acquisition is Dr Inventory / Cr funding account",
          db.source_import_account_codes(f"inventory:reconcile:{event_id}")
          == {"1200", "1010"})

    reset_variant("v7")
    db.record_inventory_event({
        "variant_id": "v7", "event_type": "acquisition", "quantity": 2,
        "unit_cost_cents": 500, "effective_date": "2025-01-01",
        "source": "manual", "source_reference": "legacy:v7",
        "note": None, "created_at": "2026-01-01T00:00:00",
        "accounting_status": "legacy_unposted",
    })
    event_id = next(row["id"] for row in db.list_inventory_reconciliation_queue()
                    if row["variant_id"] == "v7")
    inventory.acknowledge_unresolvable(event_id, "Funding source cannot be established.")
    total, reason = inventory.cost_of_sale("v7", 1, "2025-02-01")
    check("unresolvable acknowledgement closes the queue without inventing COGS",
          total is None and "cover 0 of 1" in (reason or "")
          and not any(row["id"] == event_id
                      for row in db.list_inventory_reconciliation_queue()))


# ------------------------------------------------- B: analytics import


CSV_TOTAL_SALES = (
    "Day,Total sales\n"
    "2026-01-01,1234.56\n"
    "2026-01-02,0.00\n"
    "2026-01-03,987.65\n"
)

CSV_BY_PRODUCT = (
    "Product title,Product ID,Net sales\n"
    "Neckband,111,4500.00\n"
    "Pods,222,1200.50\n"
)

CSV_MALFORMED = (
    "Day,Total sales\n"
    "2026-02-01,100.00\n"
    "not-a-date,50.00\n"
    "2026-02-03,abc\n"
    "2026-02-04,25.00\n"
)


def check_analytics_import():
    before = journal_entry_count()

    result = shopify_analytics.import_csv(CSV_TOTAL_SALES, "total_sales", "day")
    check("imports well-formed rows", result["imported"] == 3,
          f'got {result["imported"]}')
    check("no errors on a clean file", not result["errors"])

    rows = db.list_shopify_analytics(metric_type="total_sales", period="day")
    check("money stored as integer cents",
          any(r["value"] == 123456 for r in rows))
    check("a zero-value period is kept, not dropped",
          any(r["value"] == 0 for r in rows))

    # Idempotency: same file again must not double the history.
    shopify_analytics.import_csv(CSV_TOTAL_SALES, "total_sales", "day")
    rows_again = db.list_shopify_analytics(metric_type="total_sales", period="day")
    check("re-importing the same export does not duplicate",
          len(rows_again) == len(rows), f"{len(rows)} -> {len(rows_again)}")

    result = shopify_analytics.import_csv(CSV_BY_PRODUCT, "sales_by_product",
                                          "month", period_start="2026-01-01")
    check("product breakdown imports", result["imported"] == 2)
    product_rows = db.list_shopify_analytics(metric_type="sales_by_product")
    check("product reference is retained",
          any(r["product_ref"] == "111" for r in product_rows))

    # Row-level errors: bad rows reported, good rows still land.
    result = shopify_analytics.import_csv(CSV_MALFORMED, "total_sales", "day")
    check("malformed rows are reported per row", len(result["errors"]) == 2,
          f'got {result["errors"]}')
    check("good rows in a partly-bad file still import",
          result["imported"] == 2, f'got {result["imported"]}')
    check("errors name the row number",
          all("row" in e.lower() for e in result["errors"]),
          str(result["errors"]))

    result = shopify_analytics.import_csv("Day,Total sales\n", "total_sales", "day")
    check("an empty file reports rather than silently succeeding",
          result["imported"] == 0)

    try:
        shopify_analytics.import_csv(CSV_TOTAL_SALES, "not_a_metric", "day")
        check("unknown metric type is rejected", False, "accepted it")
    except ValueError:
        check("unknown metric type is rejected", True)

    # THE constraint: none of this reached the ledger.
    after = journal_entry_count()
    check("analytics import wrote ZERO journal entries",
          after == before, f"journal_entries {before} -> {after}")
    with db.get_connection() as conn:
        imports = conn.execute(
            "SELECT COUNT(*) FROM ledger_source_imports "
            "WHERE source_reference LIKE '%analytics%'").fetchone()[0]
    check("analytics import created no ledger source imports", imports == 0)


def main():
    db.init_db()
    print("A — FIFO costing")
    check_fifo_costing()
    print("\nA — refusals rather than guesses")
    check_refusals()
    print("\nA — money discipline")
    check_money_discipline()
    print("\nA — idempotency")
    check_idempotency()
    print("\nA — position report")
    check_position_report()
    print("\nA — Ledger-backed acquisition reconciliation")
    check_accounting_bridge()
    print("\nB — analytics import (and the ledger wall)")
    check_analytics_import()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("PASS: all inventory and analytics regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
