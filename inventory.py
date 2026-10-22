"""Inventory acquisition tracking and FIFO cost-of-goods matching.

The half of COGS that was missing. `shopify_variant_costs` already knew
what a unit cost at a point in time; nothing knew that units had ever
been *acquired*, so `ledger_sync._sync_shopify_cogs` computed a correct
COGS figure and then refused to post it — crediting Inventory with
nothing ever debiting it is what made Inventory drift negative before.

This module supplies the debit side and answers one question precisely:
*given a sale of N units of a variant on date D, what did those specific
units cost?*

Three rules, all of them refusals rather than estimates:

1. Cost comes from acquisitions dated on or before the sale. A later
   restock at a different price never reprices an earlier sale.
2. If tracked acquisitions do not cover the full sale quantity, the whole
   line is refused. Costing 8 of 10 units would post a number that looks
   like COGS and is not.
3. Money is integer cents end to end, Decimal only at the boundary.

FIFO is the costing method. It is the standard choice for physical goods
and it is the one that follows from rule 1: the oldest unpaid-for layer
is the one being sold.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

import db
import ledger
from insight_metrics import (InsightMetric, make_insight_metric,
                             rolling_periods, value_delta)

_CENT = Decimal("0.01")

ACQUISITION = "acquisition"
SALE = "sale"
ADJUSTMENT = "adjustment"


class InventoryError(ValueError):
    """An inventory event is invalid and must not be recorded."""


def _to_cents(value, field_name):
    """Decimal currency to exact integer cents, matching ledger.py."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise InventoryError(f"{field_name} must be a valid amount.")
    if not amount.is_finite() or amount < 0:
        raise InventoryError(f"{field_name} must be non-negative.")
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded != amount:
        raise InventoryError(f"{field_name} cannot have more than two decimals.")
    return int(rounded * 100)


def cents_to_amount(cents):
    return Decimal(int(cents)) / 100


def _clean_date(value, field_name):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise InventoryError(f"{field_name} must be an ISO date (YYYY-MM-DD).")


# ---------------------------------------------------------------- writes


def _post_acquisition_row(conn, event, funding_account_code,
                          source_reference, created_by, existing_id=None):
    """Atomically post Dr Inventory / Cr funding and the FIFO layer."""
    inventory_account = conn.execute(
        "SELECT id FROM accounts WHERE code = '1200'").fetchone()
    funding_account = conn.execute(
        "SELECT id, type FROM accounts WHERE code = ?",
        (str(funding_account_code).strip(),)).fetchone()
    if inventory_account is None:
        raise InventoryError("Inventory account 1200 is missing.")
    if funding_account is None:
        raise InventoryError("Choose an existing bank or payable funding account.")
    if funding_account["type"] not in {"asset", "liability"}:
        raise InventoryError("Funding account must be a bank asset or payable.")
    if str(funding_account_code).strip() == "1200":
        raise InventoryError("Inventory cannot fund its own acquisition.")

    amount = cents_to_amount(
        int(event["quantity"]) * int(event["unit_cost_cents"]))
    prepared = ledger._validate_journal_entry(
        event["effective_date"],
        f"Inventory acquisition: {event['variant_id']}",
        [{"account_id": inventory_account["id"], "debit_amount": amount},
         {"account_id": funding_account["id"], "credit_amount": amount}],
        created_by, source_reference)
    existing = conn.execute(
        "SELECT 1 FROM ledger_source_imports WHERE source_reference = ?",
        (source_reference,)).fetchone()
    if existing:
        return False
    ledger_entry = ledger._save_journal_entry(conn, *prepared)
    conn.execute(
        "INSERT INTO ledger_source_imports "
        "(source_reference, journal_entry_id, imported_at) VALUES (?, ?, ?)",
        (source_reference, ledger_entry["id"],
         datetime.now(timezone.utc).isoformat()),
    )
    if existing_id is not None:
        conn.execute(
            "UPDATE inventory_quantity_events SET accounting_status = ?, "
            "funding_account_id = ?, journal_source_reference = ?, "
            "resolution_note = NULL, resolved_at = NULL WHERE id = ?",
            ("ledger_posted", funding_account["id"], source_reference,
             existing_id),
        )
    else:
        columns = {
            **event,
            "accounting_status": "ledger_posted",
            "funding_account_id": funding_account["id"],
            "journal_source_reference": source_reference,
            "resolution_note": None,
            "resolved_at": None,
        }
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO inventory_quantity_events ({names}) "
            f"VALUES ({placeholders})",
            [columns[name] for name in columns],
        )
    return True


def record_acquisition(variant_id, quantity, unit_cost, effective_date,
                       source="manual", source_reference=None, note=None,
                       funding_account_code=None):
    """Record units bought in, at the cost they were bought at.

    This is what creates a FIFO layer, and therefore what makes COGS
    postable for later sales of this variant.
    """
    variant_id = str(variant_id or "").strip()
    if not variant_id:
        raise InventoryError("variant_id is required.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise InventoryError("quantity must be a whole number.")
    if quantity <= 0:
        raise InventoryError("An acquisition needs a positive quantity.")
    if not funding_account_code:
        raise InventoryError("A funding account is required for an acquisition.")
    unit_cost_cents = _to_cents(unit_cost, "unit cost")
    if unit_cost_cents <= 0:
        raise InventoryError(
            "A Ledger-backed acquisition needs a unit cost greater than zero."
        )
    event = {
        "variant_id": variant_id,
        "event_type": ACQUISITION,
        "quantity": quantity,
        "unit_cost_cents": unit_cost_cents,
        "effective_date": _clean_date(effective_date, "effective date"),
        "source": source,
        "source_reference": source_reference,
        "accounting_status": "ledger_posted",
        "note": (note or "").strip() or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    source_reference = source_reference or f"inventory:acquisition:{uuid4()}"
    event["source_reference"] = source_reference
    db.init_db()
    with db.get_connection() as conn:
        return _post_acquisition_row(
            conn, event, funding_account_code, source_reference,
            "inventory-acquisition")


def reconcile_acquisition(event_id, funding_account_code):
    """Post one legacy quantity-only acquisition through the Review queue."""
    db.init_db()
    with db.get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM inventory_quantity_events WHERE id = ?",
            (event_id,)).fetchone()
        if event is None or event["event_type"] != ACQUISITION:
            raise InventoryError("That acquisition no longer exists.")
        if event["accounting_status"] != "legacy_unposted":
            raise InventoryError("That acquisition has already been resolved.")
        source_reference = f"inventory:reconcile:{event_id}"
        payload = {key: event[key] for key in (
            "variant_id", "event_type", "quantity", "unit_cost_cents",
            "effective_date", "source", "source_reference", "note",
            "created_at")}
        return _post_acquisition_row(
            conn, payload, funding_account_code, source_reference,
            "inventory-reconciliation", existing_id=event_id)


def acknowledge_unresolvable(event_id, note):
    """Close a legacy acquisition without making up a funding account."""
    note = (note or "").strip()
    if not note:
        raise InventoryError("Explain why this acquisition cannot be resolved.")
    db.init_db()
    with db.get_connection() as conn:
        cursor = conn.execute(
            "UPDATE inventory_quantity_events SET accounting_status = ?, "
            "resolution_note = ?, resolved_at = ? "
            "WHERE id = ? AND event_type = 'acquisition' "
            "AND accounting_status = 'legacy_unposted'",
            ("acknowledged_unresolvable", note,
             datetime.now(timezone.utc).isoformat(), event_id),
        )
        if cursor.rowcount == 0:
            raise InventoryError("That acquisition has already been resolved.")
        return True


def record_sale(variant_id, quantity, effective_date, source_reference,
                source="shopify_sync", note=None):
    """Record units going out, so stock on hand stays true.

    Carries a source reference and is written with INSERT OR IGNORE, so a
    re-run of the same sync cannot consume the same stock twice.
    """
    quantity = int(quantity)
    if quantity <= 0:
        raise InventoryError("A sale needs a positive quantity to remove.")
    return db.record_inventory_event({
        "variant_id": str(variant_id).strip(),
        "event_type": SALE,
        "quantity": -quantity,
        "unit_cost_cents": None,
        "effective_date": _clean_date(effective_date, "effective date"),
        "source": source,
        "source_reference": source_reference,
        "accounting_status": "ledger_posted",
        "note": (note or "").strip() or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def record_adjustment(variant_id, quantity, effective_date, note=None):
    """A correction: shrinkage, breakage, a miscount put right.

    Adjustments carry no cost. A negative adjustment consumes FIFO layers
    like a sale; a positive one without a cost cannot create a layer, so
    it is recorded for stock-on-hand only and deliberately does not make
    COGS postable — use an acquisition for that.
    """
    quantity = int(quantity)
    if quantity == 0:
        raise InventoryError("An adjustment of zero changes nothing.")
    return db.record_inventory_event({
        "variant_id": str(variant_id).strip(),
        "event_type": ADJUSTMENT,
        "quantity": quantity,
        "unit_cost_cents": None,
        "effective_date": _clean_date(effective_date, "effective date"),
        "source": "manual",
        "source_reference": None,
        "accounting_status": "ledger_posted",
        "note": (note or "").strip() or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------- FIFO


def _remaining_layers(variant_id, as_of, exclude_source_reference=None):
    """Unconsumed acquisition layers as of a date, oldest first.

    Walks acquisitions in order and draws down whatever has already left
    (sales and negative adjustments) against the oldest layers first.

    exclude_source_reference drops one event from the outflow — the sale
    currently being costed. Without it, re-running a sync would see its
    own prior sale event, believe the stock was gone, and start refusing
    entries it had already posted.
    """
    events = db.list_inventory_events(variant_id, up_to=as_of)
    layers = []
    outflow = 0
    for event in events:
        if (exclude_source_reference is not None
                and event["source_reference"] == exclude_source_reference):
            continue
        quantity = int(event["quantity"])
        if (event["event_type"] == ACQUISITION
                and event.get("accounting_status", "ledger_posted")
                == "ledger_posted"):
            layers.append([quantity, int(event["unit_cost_cents"])])
        elif quantity < 0:
            outflow += -quantity
        else:
            # A positive adjustment has no cost, so it cannot form a
            # layer. It offsets outflow instead, which keeps stock on
            # hand right without inventing a cost basis.
            outflow -= quantity

    for layer in layers:
        if outflow <= 0:
            break
        take = min(layer[0], outflow)
        layer[0] -= take
        outflow -= take
    return [(qty, cost) for qty, cost in layers if qty > 0]


def cost_of_sale(variant_id, quantity, as_of, exclude_source_reference=None):
    """FIFO cost of selling `quantity` units on `as_of`.

    Returns (total_cents, None) when tracked acquisitions cover the sale,
    or (None, reason) when they do not. Never returns a partial figure:
    a COGS number covering some of the units is not COGS.
    """
    variant_id = str(variant_id or "").strip()
    if not variant_id:
        return None, "no variant id"
    quantity = int(quantity)
    if quantity <= 0:
        return None, "sale quantity is not positive"

    layers = _remaining_layers(variant_id, as_of, exclude_source_reference)
    available = sum(qty for qty, _ in layers)
    if available < quantity:
        return None, (f"tracked acquisitions cover {available} of {quantity} "
                      f"units on or before {as_of}")

    total, outstanding = 0, quantity
    for layer_qty, unit_cost in layers:
        if outstanding <= 0:
            break
        take = min(layer_qty, outstanding)
        total += take * unit_cost
        outstanding -= take
    return total, None


# --------------------------------------------------------------- reports


def inventory_position(as_of=None):
    """Stock on hand and its cost basis, per variant.

    Same shape as ledger.trial_balance() and profit_and_loss(): a plain
    structure the caller renders, with money as Decimal.
    """
    as_of = _clean_date(as_of, "as of") if as_of else date.today().isoformat()
    titles = db.shopify_variant_titles()
    rows = []
    total_value = 0
    for variant_id in db.list_inventory_variants():
        layers = _remaining_layers(variant_id, as_of)
        on_hand = sum(qty for qty, _ in layers)
        value_cents = sum(qty * cost for qty, cost in layers)
        latest = db.get_shopify_variant_cost_at(variant_id, as_of + "T23:59:59")
        latest_cents = (latest or {}).get("unit_cost_cents")
        total_value += value_cents
        rows.append({
            "variant_id": variant_id,
            "title": titles.get(variant_id) or variant_id,
            "on_hand": on_hand,
            "value": cents_to_amount(value_cents),
            "average_cost": (cents_to_amount(round(value_cents / on_hand))
                             if on_hand else None),
            "latest_known_cost": (cents_to_amount(latest_cents)
                                  if latest_cents is not None else None),
            "layers": [{"quantity": qty, "unit_cost": cents_to_amount(cost)}
                       for qty, cost in layers],
        })
    rows.sort(key=lambda row: (-row["on_hand"], row["title"].lower()))
    return {
        "as_of": as_of,
        "items": rows,
        "total_value": cents_to_amount(total_value),
        "tracked_variants": len(rows),
    }


def cost_history(variant_id):
    """Every recorded cost for one variant, newest first."""
    return db.list_variant_cost_history(str(variant_id).strip())


def get_insight_metrics() -> list[InsightMetric]:
    """Inventory value at exact FIFO cost from locally recorded layers."""
    db.init_db()
    if not db.list_inventory_variants():
        return []

    points = []
    for start, end in rolling_periods():
        position = inventory_position(end.isoformat())
        value_cents = int(position["total_value"] * 100)
        points.append({
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": value_cents,
        })
    value, delta = value_delta(points)

    # Inventory is keyed to Shopify variants. Use Shopify as the source
    # system when it is connected, and its generic admin otherwise.
    import shopify_client
    store = shopify_client.get_store()
    deep_link = (f"https://{store}/admin/products" if store
                 else "https://admin.shopify.com/")
    return [make_insight_metric(
        "inventory_value",
        "Inventory at cost",
        value,
        delta,
        "inventory",
        deep_link,
        "currency_cents",
        points,
    )]
