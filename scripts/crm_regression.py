"""Offline regression coverage for the CRM contact graph and pipeline.

Three things are guarded here, for three different reasons:

1. **Margin honesty.** COGS is withheld for any order with a line that
   could not be costed, so a customer's orders routinely split into some
   with COGS posted and some without. The tests pin that margin covers
   only the costed subset, that a margin percentage is computed against
   that subset's own revenue rather than the full total, and that a
   contact with nothing costed reports no margin rather than zero.

2. **The CRM posts nothing.** Asserted negatively, by counting
   journal_entries around every CRM write, because a read layer that
   quietly started posting would leave every report still balancing —
   just with invented entries in it. Nothing else would notice.

3. **Stage is a judgment, stored separately.** An absent row means "no
   judgment yet", never a defaulted stage, and an unknown stage is
   refused rather than written.

Isolated temp data dir, no network, no credentials.

Run it:

    venv/Scripts/python scripts/crm_regression.py   # Windows
    venv/bin/python scripts/crm_regression.py       # macOS/Linux

Exits 0 if every check passes, 1 if any fails (prints which).
"""

import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DATA = tempfile.TemporaryDirectory(prefix="klearing-crm-",
                                    ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db  # noqa: E402
import crm  # noqa: E402
import inventory  # noqa: E402
import invoice_ledger  # noqa: E402
import ledger_sync  # noqa: E402
from migrations.run_migrations import run_migrations  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def journal_entry_count():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM journal_entries").fetchone()[0]


def customer(customer_id, first, last, email):
    db.upsert_shopify_customer({
        "customer_id": customer_id, "email": email, "first_name": first,
        "last_name": last, "orders_count": 0, "total_spent": 0.0,
        "created_at": "2026-01-01", "tags": "", "accepts_marketing": 0})


def order(order_id, customer_id, total, variant, quantity,
          status="paid", refund=0.0):
    db.upsert_shopify_order({
        "order_id": order_id, "created_at": "2026-03-01T00:00:00",
        "total_price": total, "subtotal_price": total, "total_discounts": 0,
        "total_tax": 0, "financial_status": status,
        "fulfillment_status": "fulfilled", "customer_id": customer_id,
        "customer_email": "", "customer_name": "", "source": "web",
        "refund_total": refund,
        "line_items_json": json.dumps(
            [{"variant_id": variant, "quantity": quantity, "title": "Thing"}])})


def cost_snapshot(variant, cents):
    db.record_shopify_variant_cost({
        "variant_id": variant, "captured_at": "2026-01-01T00:00:00",
        "inventory_item_id": f"i{variant}", "unit_cost_cents": cents,
        "cost_status": "available", "detail": ""})


def main():
    db.init_db()
    run_migrations(report=False)

    # ------------------------------------------------------------ fixture
    # Ada: one fully costed order and one whose variant was never
    # acquired, so COGS is withheld for it. Bo: one order, nothing
    # acquired, so no margin at all. Plus a guest checkout owned by nobody.
    customer("C1", "Ada", "Byron", "ada@example.com")
    customer("C2", "Bo", "Diddley", "bo@example.com")
    order("1001", "C1", 200.0, "v1", 2)
    order("1002", "C1", 100.0, "v2", 1)
    order("1003", "", 50.0, "v1", 1)
    order("1004", "C2", 80.0, "v2", 1)
    # A voided order: posts no revenue at all, which is distinct from
    # posting zero. The graph must show it without inventing a figure.
    order("1005", "C1", 60.0, "v1", 1, status="voided")
    for variant, cents in (("v1", 1000), ("v2", 2000)):
        cost_snapshot(variant, cents)
    inventory.record_acquisition("v1", 10, "10.00", "2026-01-01",
                                 source="manual", funding_account_code="1010")
    ledger_sync.sync_ledger_from_sources()

    print("\nContact seeding")
    seeded = db.backfill_contacts_from_shopify()
    check("Shopify customers seed contacts", seeded["created"] == 2,
          f"created {seeded['created']}")
    again = db.backfill_contacts_from_shopify()
    check("re-running the seed creates nothing new", again["created"] == 0,
          f"created {again['created']} on the second run")
    check("contact count stays at 2", len(db.list_contacts()) == 2,
          f"{len(db.list_contacts())} contacts")

    ada = db.get_contact_by_shopify_id("C1")
    bo = db.get_contact_by_shopify_id("C2")

    # A hand-made contact sharing an email is adopted, not duplicated.
    manual = db.create_contact("Ada B", email="ada2@example.com")
    db.update_contact(manual, email="ada2@example.com")
    customer("C3", "Ada", "B", "ada2@example.com")
    adopted = db.backfill_contacts_from_shopify()
    check("an unlinked contact matching on email is adopted, not duplicated",
          adopted["linked"] == 1 and adopted["created"] == 0,
          f"linked {adopted['linked']}, created {adopted['created']}")

    print("\nMargin is only reported where it is known")
    graph = crm.contact_graph(ada["id"])
    totals = graph["totals"]
    check("revenue counts every order that posted",
          totals["revenue"] == Decimal("300"), str(totals["revenue"]))
    check("margin covers only the costed order",
          totals["margin"] == Decimal("180"), str(totals["margin"]))
    check("coverage is stated, not implied",
          totals["orders_costed"] == 1 and totals["orders_with_revenue"] == 2,
          f"{totals['orders_costed']} of {totals['orders_with_revenue']}")
    check("partial margin is flagged as partial", totals["margin_is_partial"])
    check("margin percent uses the costed subset's own revenue",
          totals["margin_revenue"] == Decimal("200")
          and totals["margin_percent"] == Decimal("90"),
          f"{totals['margin_percent']}% on {totals['margin_revenue']}")

    uncosted = {row["order_id"] for row in totals["uncosted"]}
    check("the uncosted order is named so it can be fixed",
          uncosted == {"1002"}, str(uncosted))

    by_id = {row["order_id"]: row for row in graph["orders"]}
    check("an uncosted order has no margin rather than a zero one",
          by_id["1002"]["margin_cents"] is None,
          str(by_id["1002"]["margin_cents"]))
    check("a voided order posts no revenue, distinct from posting zero",
          by_id["1005"]["revenue_cents"] is None,
          str(by_id["1005"]["revenue_cents"]))

    bo_graph = crm.contact_graph(bo["id"])
    check("a contact with nothing costed reports no margin, not zero",
          bo_graph["totals"]["margin"] is None,
          str(bo_graph["totals"]["margin"]))
    check("that contact still reports its real revenue",
          bo_graph["totals"]["revenue"] == Decimal("80"),
          str(bo_graph["totals"]["revenue"]))

    print("\nGuest checkouts are disclosed, not absorbed")
    check("orders with no customer id are counted",
          db.unattributed_shopify_order_count() == 1,
          str(db.unattributed_shopify_order_count()))
    attributed = sum(row["revenue"] for row in crm.contact_summaries())
    check("no contact silently claims the guest order's revenue",
          attributed == Decimal("380"), str(attributed))

    print("\nFigures come from what posted, not what could be recomputed")
    # A refund is netted at post time. Reading the ledger back must show
    # the netted figure, not shopify_orders.total_price.
    customer("C4", "Cy", "Twombly", "cy@example.com")
    order("1006", "C4", 100.0, "v1", 1, refund=40.0)
    ledger_sync.sync_ledger_from_sources()
    db.backfill_contacts_from_shopify()
    cy = db.get_contact_by_shopify_id("C4")
    cy_totals = crm.contact_graph(cy["id"])["totals"]
    check("revenue is net of refunds, as posted",
          cy_totals["revenue"] == Decimal("60"), str(cy_totals["revenue"]))

    print("\nStage is a judgment, stored separately")
    check("a contact starts with no stage",
          db.get_contact_stage(ada["id"]) is None)
    check("unassigned contacts appear in the board's unassigned group",
          any(key == crm.UNASSIGNED and any(
              row["contact"]["id"] == ada["id"] for row in rows)
              for key, _, rows in crm.pipeline_board()))
    db.set_contact_stage(ada["id"], "key_account")
    check("a stage can be assigned",
          db.get_contact_stage(ada["id"]) == "key_account")
    db.set_contact_stage(ada["id"], "dormant")
    check("re-assigning moves rather than duplicates",
          db.get_contact_stage(ada["id"]) == "dormant")
    rejected = False
    try:
        db.set_contact_stage(ada["id"], "prospect")
    except ValueError:
        rejected = True
    check("an unknown stage is refused", rejected)
    db.clear_contact_stage(ada["id"])
    check("clearing returns the contact to no judgment",
          db.get_contact_stage(ada["id"]) is None)
    check("stages are a fixed set of four", len(crm.STAGE_LABELS) == 4,
          f"{len(crm.STAGE_LABELS)} stages")
    check("every stage label maps to a db stage key",
          {key for key, _ in crm.STAGE_LABELS} == set(db.CONTACT_STAGES))

    print("\nA contact's invoices are priced by invoice_ledger")
    # The original suite only ever asserted the *absent* case ("degrades
    # to empty"), so the populated path -- the one a real user with a
    # real invoice hits -- was never executed, and a wrong totals key in
    # it made /contacts/<id> a 500. Build a real invoice and read it back.
    billed = db.create_contact("Billed Co", email="ap@billed.example")
    invoice_id = db.create_invoice(billed, "2026-06-01", "2026-07-01",
                                   terms="Net 30")
    db.replace_invoice_lines(invoice_id, [
        {"description": "Consulting", "quantity_milli": 2000,
         "unit_price_cents": 25000, "tax_rate_bp": 1000}])
    invoice_ledger.send_invoice(invoice_id, sent_at="2026-06-01")

    billed_graph = crm.contact_graph(billed["id"] if isinstance(billed, dict)
                                     else billed)
    check("a contact with an invoice renders instead of raising",
          billed_graph is not None and len(billed_graph["invoices"]) == 1,
          str(billed_graph and billed_graph["invoices"]))
    invoice_row = billed_graph["invoices"][0]
    # 2 x $250.00 = $500.00 net, +10% tax = $550.00 gross.
    check("the invoice total is gross, tax included",
          invoice_row["total"] == Decimal("550"), str(invoice_row["total"]))
    check("the outstanding balance matches the unpaid gross",
          billed_graph["invoiced_outstanding"] == Decimal("550"),
          str(billed_graph["invoiced_outstanding"]))
    check("the invoice carries its allocated number, not 'Draft'",
          invoice_row["number"].startswith("INV-"), invoice_row["number"])

    print("\nThe CRM posts nothing")
    before = journal_entry_count()
    db.set_contact_stage(bo["id"], "customer")
    db.update_contact(bo["id"], notes="Met at a trade show.")
    new_contact = db.create_contact("Zed Zed", email="zed@example.com")
    db.add_contact_alias(new_contact, "Zed Zed", "manual")
    crm.contact_graph(ada["id"])
    crm.contact_summaries()
    crm.pipeline_board()
    after = journal_entry_count()
    check("no CRM write or read creates a journal entry", before == after,
          f"{before} entries before, {after} after")

    print("\nThe graph reads, it does not duplicate")
    check("a missing contact returns None rather than an empty page",
          crm.contact_graph(999999) is None)
    check("aliases are listed for display",
          any(row["source"] == "shopify" for row in graph["aliases"]),
          str(graph["aliases"]))
    check("invoices degrade to empty rather than failing when absent",
          isinstance(graph["invoices"], list))

    print(f"\n{len(_failures)} failed")
    if _failures:
        print("FAIL: " + "; ".join(_failures))
        return 1
    print("PASS: all CRM regression checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
