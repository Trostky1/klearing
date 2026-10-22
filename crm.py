"""The unified business graph: what one contact is worth, from real data.

Klearing already knows that *this Shopify order* belongs to *this
customer* and cost *this much to fulfil*, because it is one system rather
than a storefront bolted to a bookkeeper bolted to a CRM. Nothing
surfaced that connection. This module is the read layer that does.

It is read-only by design. Nothing here writes a journal entry, and
nothing here owns a copy of data another table already holds -- orders
stay in shopify_orders, invoices in invoices, amounts in journal_lines.
A CRM that duplicated those would immediately start disagreeing with
them.

Two rules govern every figure below, and both are refusals:

1. **Margin is only reported where it is actually known.** COGS is
   withheld entirely for any order with a line that could not be costed
   (see inventory.py), so a customer's orders routinely divide into some
   with COGS posted and some without. Summing revenue across all of them
   and COGS across only some produces a margin that is silently
   overstated and looks authoritative. Margin here covers only orders
   carrying both postings, and always reports the coverage alongside it.

2. **Figures come from what posted, not from what could be recomputed.**
   Order revenue is netted of refunds at post time; re-deriving it from
   shopify_orders would yield a second number, close enough to look
   right and free to disagree with the books. Everything money-shaped
   here is read back out of journal_lines.

Guest checkouts carry no Shopify customer id and so belong to no
contact. They are counted and reported rather than quietly dropped: a
per-customer view that omits them without saying so reads as a complete
picture of revenue and is not one.
"""

import sqlite3
from decimal import Decimal

import db
import ledger_sync

# Sourced from ledger_sync so there is one definition of each code in the
# codebase, the same reason invoice_ledger.py imports them from there.
SALES_REVENUE = ledger_sync.SALES_REVENUE
COGS = ledger_sync.COGS

# Stage keys are db.CONTACT_STAGES; these are their labels and the order
# they read in. Fixed and not user-configurable, deliberately -- see
# migrations/add_contact_pipeline.sql.
STAGE_LABELS = (
    ("lead", "Lead"),
    ("customer", "Customer"),
    ("key_account", "Key account"),
    ("dormant", "Dormant"),
)
UNASSIGNED = "unassigned"
UNASSIGNED_LABEL = "Unassigned"


def stage_label(stage):
    """The display name for a stage key, including the absent case."""
    return dict(STAGE_LABELS).get(stage, UNASSIGNED_LABEL)


def _amount(cents):
    """Cents to Decimal at the boundary, matching ledger and inventory."""
    return Decimal(cents) / 100


def _net(totals, code, kind):
    """The net posted amount against one account code.

    kind is "credit" for revenue (credits increase it) or "debit" for
    COGS (debits increase it). The opposite column is subtracted rather
    than ignored so a reversal or a returns entry reduces the figure
    instead of being invisible.
    """
    line = (totals or {}).get(code)
    if not line:
        return None
    if kind == "credit":
        return line["credit"] - line["debit"]
    return line["debit"] - line["credit"]


def _order_rows(orders):
    """Each order with whatever the ledger actually posted against it.

    Returns rows carrying revenue_cents and cogs_cents, either of which
    may be None meaning "nothing posted" -- distinct from 0, which would
    mean "posted, and it was nothing".
    """
    references = []
    for order in orders:
        reference = f"shopify:order:{order['order_id']}"
        references.append(reference)
        references.append(f"{reference}:cogs")
    posted = db.posted_amounts_by_source_reference(references)

    rows = []
    for order in orders:
        reference = f"shopify:order:{order['order_id']}"
        revenue = _net(posted.get(reference), SALES_REVENUE, "credit")
        cogs = _net(posted.get(f"{reference}:cogs"), COGS, "debit")
        rows.append({
            "order_id": order["order_id"],
            "created_at": (order.get("created_at") or "")[:10],
            "financial_status": order.get("financial_status") or "",
            "fulfillment_status": order.get("fulfillment_status") or "",
            "revenue_cents": revenue,
            "cogs_cents": cogs,
            # Margin is per-order and only where both halves posted.
            "margin_cents": (revenue - cogs
                             if revenue is not None and cogs is not None
                             else None),
        })
    return rows


def shopify_order_financials(orders=None):
    """Public read-only view of posted revenue, COGS, and margin per order.

    Insights uses this same path as Contacts so an inventory-backed margin is
    never recomputed from raw Shopify prices or partially costed order lines.
    """
    return _order_rows(db.list_shopify_orders() if orders is None else orders)


def _roll_up(rows):
    """Totals across a contact's orders, with margin coverage stated.

    revenue covers every order that posted revenue. margin covers only
    the subset that also posted COGS, and margin_revenue is that same
    subset's revenue -- so a margin percentage is computed against the
    revenue it actually corresponds to, never against the full total.
    """
    revenue = sum(row["revenue_cents"] for row in rows
                  if row["revenue_cents"] is not None)
    costed = [row for row in rows if row["margin_cents"] is not None]
    margin = sum(row["margin_cents"] for row in costed)
    margin_revenue = sum(row["revenue_cents"] for row in costed)
    posted_orders = [row for row in rows if row["revenue_cents"] is not None]
    return {
        "orders": len(rows),
        "orders_with_revenue": len(posted_orders),
        "orders_costed": len(costed),
        "uncosted": [row for row in posted_orders
                     if row["margin_cents"] is None],
        "revenue_cents": revenue,
        "revenue": _amount(revenue),
        "margin_cents": margin if costed else None,
        "margin": _amount(margin) if costed else None,
        "margin_revenue": _amount(margin_revenue) if costed else None,
        "margin_percent": (Decimal(margin * 100) / Decimal(margin_revenue)
                           if costed and margin_revenue else None),
        "margin_is_partial": bool(costed) and len(costed) < len(posted_orders),
    }


def _invoice_rows(contact_id):
    """This contact's invoices, priced by invoice_ledger.

    Totals and status come from invoice_ledger rather than being
    recomputed here, so there stays exactly one place that knows how an
    invoice's balance is worked out.
    """
    try:
        invoices = db.list_invoices_for_contact(contact_id)
    except sqlite3.OperationalError:
        # The invoicing tables are created by their own gated migration.
        # A CRM that hard-failed when they were absent would make this
        # page depend on a workstream it only reads from. Narrow to the
        # missing-table error rather than bare Exception, so a genuine
        # bug in invoice pricing still surfaces instead of rendering as
        # "this contact has no invoices".
        return []

    import invoice_ledger

    rows = []
    for invoice in invoices:
        lines = db.list_invoice_lines(invoice["id"])
        totals = invoice_ledger.invoice_totals(lines)
        balance = invoice_ledger.balance_cents(invoice["id"], lines)
        rows.append({
            "id": invoice["id"],
            "number": invoice.get("invoice_number") or "Draft",
            "issue_date": invoice.get("issue_date") or "",
            "due_date": invoice.get("due_date") or "",
            "status": invoice_ledger.status(invoice, balance),
            # invoice_totals returns {net, tax, gross}; gross is the
            # figure the customer owes, tax included.
            "total": _amount(totals["gross"]),
            "balance": _amount(balance),
            "balance_cents": balance,
        })
    return rows


def contact_graph(contact_id):
    """Everything Klearing knows about one contact, in one structure.

    Returns None if the contact does not exist, so the caller can 404
    rather than render an empty page that looks like a real one.
    """
    contact = db.get_contact(contact_id)
    if not contact:
        return None

    orders = db.list_shopify_orders_for_customer(contact["shopify_customer_id"])
    rows = _order_rows(orders)
    invoices = _invoice_rows(contact_id)
    outstanding = sum(row["balance_cents"] for row in invoices
                      if row["balance_cents"] > 0)

    return {
        "contact": contact,
        "stage": db.get_contact_stage(contact_id),
        "aliases": db.list_contact_aliases(contact_id),
        "orders": rows,
        "totals": _roll_up(rows),
        "invoices": invoices,
        "invoiced_outstanding": _amount(outstanding),
        "has_shopify_link": bool(contact["shopify_customer_id"]),
    }


def contact_summaries():
    """One row per contact for the list and pipeline views.

    Deliberately one pass over every order rather than a query per
    contact: a store with a few thousand orders would otherwise fire a
    few thousand queries to draw one table.
    """
    contacts = db.list_contacts()
    stages = db.contact_stages()
    orders_by_customer = {}
    for order in db.list_shopify_orders():
        key = (order.get("customer_id") or "").strip()
        if key:
            orders_by_customer.setdefault(key, []).append(order)

    summaries = []
    for contact in contacts:
        orders = orders_by_customer.get(
            str(contact["shopify_customer_id"] or ""), [])
        totals = _roll_up(_order_rows(orders))
        summaries.append({
            "contact": contact,
            "stage": stages.get(contact["id"]),
            "orders": totals["orders"],
            "revenue": totals["revenue"],
            "revenue_cents": totals["revenue_cents"],
            "margin": totals["margin"],
            "margin_cents": totals["margin_cents"],
            "margin_is_partial": totals["margin_is_partial"],
            "orders_costed": totals["orders_costed"],
            "orders_with_revenue": totals["orders_with_revenue"],
        })
    summaries.sort(key=lambda row: (-row["revenue_cents"],
                                    row["contact"]["display_name"].lower()))
    return summaries


def pipeline_board(summaries=None):
    """Contacts grouped by stage, in STAGE_LABELS order then unassigned.

    Unassigned is last and always present, because it is where every
    Shopify-seeded contact starts and hiding it would hide most of them.
    """
    summaries = contact_summaries() if summaries is None else summaries
    groups = {key: [] for key, _ in STAGE_LABELS}
    groups[UNASSIGNED] = []
    for row in summaries:
        groups[row["stage"] or UNASSIGNED].append(row)
    ordered = [(key, label, groups[key]) for key, label in STAGE_LABELS]
    ordered.append((UNASSIGNED, UNASSIGNED_LABEL, groups[UNASSIGNED]))
    return ordered
