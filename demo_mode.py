"""Demo mode: a fake business to click through, kept apart from real data.

"Take a look" on the first Setup screen starts a demo. It points Klearing at
its own folder (``<data dir>/demo``), fills it with a made-up business, and
shows a banner until the visitor exits. Real data is never read or written
while a demo is open, and no credentials are stored.

Every date in the demo is relative to today, so the demo never goes stale.
Each visit starts a fresh demo.
"""

import gc
import json
import shutil
import time
from datetime import date, timedelta

import paths

DEMO_FOLDER = "demo"
DEMO_BUSINESS = "Summit Edge Ski & Snowboard"


def demo_dir():
    return paths.base_data_dir() / DEMO_FOLDER


def is_active():
    return paths.is_demo()


def activate():
    """Open a fresh demo business. Safe to call again: it starts over."""
    folder = demo_dir()
    # Only ever remove the folder named "demo" directly inside the data dir.
    if (folder.name == DEMO_FOLDER and folder.parent == paths.base_data_dir()
            and folder.exists()):
        _remove(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths.set_data_dir_override(folder)
    try:
        seed()
    except Exception:
        paths.set_data_dir_override(None)
        raise


def _remove(folder):
    """Delete the demo folder. SQLite connections are closed by garbage
    collection, and on Windows an open file cannot be deleted, so collect
    first and retry briefly."""
    for _ in range(20):
        gc.collect()
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("Could not clear the previous demo. Close it and try again.")


def deactivate():
    paths.set_data_dir_override(None)


# ------------------------------------------------------------------- seeding


def _days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


def _in_days(days):
    return (date.today() + timedelta(days=days)).isoformat()


def _line(description, quantity, price_cents):
    return {"description": description, "quantity_milli": int(quantity * 1000),
            "unit_price_cents": price_cents, "tax_rate_bp": 0}


PRODUCTS = [
    # (product id, title, type, vendor, retail, cost, [(variant id, label, sku)])
    ("7710001", "Atomic Maverick 95 Skis", "Skis", "Atomic", 649, 380,
     [("4410001", "170 cm", "SKI-ATM-170"), ("4410002", "177 cm", "SKI-ATM-177"),
      ("4410003", "184 cm", "SKI-ATM-184")]),
    ("7710002", "Burton Custom Snowboard", "Snowboards", "Burton", 599, 340,
     [("4410011", "154 cm", "SNB-BUR-154"), ("4410012", "158 cm", "SNB-BUR-158"),
      ("4410013", "162 cm", "SNB-BUR-162")]),
    ("7710003", "Salomon STH2 Bindings", "Bindings", "Salomon", 349, 190,
     [("4410021", "Small", "BND-SAL-S"), ("4410022", "Medium", "BND-SAL-M"),
      ("4410023", "Large", "BND-SAL-L")]),
    ("7710004", "Smith I/O Goggles", "Goggles", "Smith", 229, 120,
     [("4410031", "One size", "GOG-SMI-OS")]),
    ("7710005", "Giro Ratio Helmet", "Helmets", "Giro", 179, 95,
     [("4410041", "Small", "HLM-GIR-S"), ("4410042", "Medium", "HLM-GIR-M"),
      ("4410043", "Large", "HLM-GIR-L")]),
    ("7710006", "Black Diamond Traverse Poles", "Poles", "Black Diamond", 79, 38,
     [("4410051", "One size", "POL-BD-OS")]),
    ("7710007", "Smartwool Ski Socks", "Apparel", "Smartwool", 26, 11,
     [("4410061", "Medium", "SOC-SMW-M"), ("4410062", "Large", "SOC-SMW-L")]),
    ("7710008", "Dakine Boot Bag", "Accessories", "Dakine", 59, 26,
     [("4410071", "One size", "BAG-DAK-OS")]),
]

SHOP_CUSTOMERS = [
    ("9910001", "Maya", "Lindqvist"), ("9910002", "Jordan", "Reyes"),
    ("9910003", "Priya", "Nair"), ("9910004", "Cole", "Whitaker"),
    ("9910005", "Elena", "Brandt"), ("9910006", "Sam", "Okafor"),
    ("9910007", "Hannah", "Kowalski"), ("9910008", "Tyler", "Nguyen"),
]


def _variants():
    for pid, title, kind, vendor, price, cost, variants in PRODUCTS:
        for vid, label, sku in variants:
            yield {"pid": pid, "title": title, "vendor": vendor, "price": price,
                   "cost": cost, "vid": vid, "label": label, "sku": sku}


def seed():
    """Fill the open (demo) data folder with a ski and snowboard shop."""
    # Imported here: app imports this module, so a top-level import would loop.
    import random

    import app
    import banks
    import business_registry
    import db
    import inventory
    import invoice_ledger
    import ledger
    import ledger_sync

    rng = random.Random(2026)
    db.init_db()
    business_registry.upsert_business(DEMO_BUSINESS, make_active=True)
    db.set_setting("business_name", DEMO_BUSINESS)
    db.set_setting("setup_complete", "1")

    # --- Bank account, mapped to Checking, and the alert settings
    db.upsert_plaid_account({
        "account_id": "demo-checking", "name": "Operating Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "4242", "synced_at": date.today().isoformat() + "T00:00:00+00:00",
    })
    db.set_bank_ledger_account(
        "plaid", "demo-checking", ledger.account_by_code("1010")["id"])
    db.set_setting("cash_accounts", '["demo-checking"]')
    db.set_setting(banks.ACTIVE_BANK_KEY, "plaid")
    db.set_setting("alert_balance_threshold", "5000")

    # --- Chart of accounts: the shop's own revenue lines, and tax mappings
    rentals = ledger.create_account("Equipment Rentals", "revenue", "4100", "gross_receipts")
    lessons = ledger.create_account("Lessons and Clinics", "revenue", "4100", "gross_receipts")
    repairs = ledger.create_account("Tuning and Repair", "revenue", "4100", "gross_receipts")
    for code, category in (("6030", "other_expense"), ("6070", "rent_other"),
                           ("6020", "commissions_and_fees"), ("6060", "wages"),
                           ("6000", "other_expense")):
        ledger.set_account_tax_category(
            ledger.account_by_code(code)["id"], category)

    # --- Products, customers, and stock in
    for pid, title, kind, vendor, price, cost, variants in PRODUCTS:
        db.upsert_shopify_product({
            "product_id": pid, "title": title, "product_type": kind,
            "vendor": vendor, "status": "active",
            "created_at": _days_ago(200) + "T12:00:00+00:00",
            "variants_json": json.dumps([
                {"id": vid, "title": label, "sku": sku, "price": f"{price:.2f}",
                 "product_id": pid} for vid, label, sku in variants]),
        })

    def approved(vendor, amount, direction, code, days_ago, key):
        item_id = db.insert_pending_item(
            source="plaid", vendor=vendor, amount=amount,
            date=_days_ago(days_ago), suggested_category=None,
            direction=direction, external_transaction_id=f"demo-{key}",
            account_id="demo-checking")
        item = db.get_item(item_id)
        account, name = app.resolve_submitted_category(code)
        app._post_one_approved(item, name, account, "", None)

    # The owners put money in before the shop bought stock.
    approved("Owner investment", 55000.00, "income", "3000", 130, "owner1")

    left = {}
    for v in _variants():
        qty = {"Skis": 9, "Snowboards": 9}.get(
            next(p[2] for p in PRODUCTS if p[0] == v["pid"]), 12)
        left[v["vid"]] = qty
        inventory.record_acquisition(
            v["vid"], qty, v["cost"], _days_ago(115), source="manual",
            source_reference=f"demo-acq-{v['vid']}", note=v["title"],
            funding_account_code="1010")
    # A restock that has arrived but is not matched to a payment yet.
    db.record_inventory_event({
        "variant_id": "4410042", "event_type": "acquisition", "quantity": 6,
        "unit_cost_cents": 9500, "effective_date": _days_ago(6),
        "source": "manual", "source_reference": "demo-acq-restock",
        "accounting_status": "legacy_unposted",
        "note": "Giro Ratio Helmet restock",
        "created_at": date.today().isoformat() + "T00:00:00+00:00"})

    # --- Shopify: customers, orders, and payouts
    order_rows = []
    variants = list(_variants())
    spent = {c[0]: [0, 0] for c in SHOP_CUSTOMERS}
    for n in range(44):
        days_ago = 86 - int(n * 1.9)
        cid, first, last = rng.choice(SHOP_CUSTOMERS)
        lines, subtotal = [], 0.0
        for _ in range(rng.choice([1, 1, 2, 2, 3])):
            v = rng.choice(variants)
            qty = rng.choice([1, 1, 1, 2]) if v["price"] < 100 else 1
            if left[v["vid"]] < qty + 1:
                continue
            left[v["vid"]] -= qty
            subtotal += v["price"] * qty
            lines.append({
                "id": 5500000 + n * 10 + len(lines), "product_id": v["pid"],
                "variant_id": v["vid"], "title": v["title"],
                "variant_title": v["label"], "name": f"{v['title']} - {v['label']}",
                "sku": v["sku"], "quantity": qty, "price": f"{v['price']:.2f}",
                "vendor": v["vendor"]})
        if not lines:
            continue
        discount = round(subtotal * 0.10, 2) if rng.random() < 0.2 else 0.0
        tax = round((subtotal - discount) * 0.0775, 2)
        total = round(subtotal - discount + tax, 2)
        spent[cid][0] += 1
        spent[cid][1] += total
        order_rows.append((days_ago, total))
        db.upsert_shopify_order({
            "order_id": str(6600000 + n),
            "created_at": _days_ago(days_ago) + "T15:30:00+00:00",
            "total_price": total, "subtotal_price": subtotal,
            "total_discounts": discount, "total_tax": tax,
            "financial_status": "paid", "fulfillment_status": "fulfilled",
            "customer_id": cid, "customer_email": f"{first.lower()}.{last.lower()}@example.com",
            "customer_name": f"{first} {last}", "source": "online_store",
            "refund_total": 0.0, "line_items_json": json.dumps(lines)})
    for cid, first, last in SHOP_CUSTOMERS:
        orders, total = spent[cid]
        db.upsert_shopify_customer({
            "customer_id": cid, "email": f"{first.lower()}.{last.lower()}@example.com",
            "first_name": first, "last_name": last, "orders_count": orders,
            "total_spent": round(total, 2), "created_at": _days_ago(150) + "T12:00:00+00:00",
            "tags": "", "accepts_marketing": 1})
        contact_id = db.create_contact(
            f"{first} {last}", email=f"{first.lower()}.{last.lower()}@example.com")
        db.update_contact(contact_id, shopify_customer_id=cid)
    week = 0
    while True:
        end = 80 - week * 7
        if end < 4:
            break
        gross = sum(t for d, t in order_rows if end <= d < end + 7)
        if gross:
            fee = round(gross * 0.029 + 0.30 * 3, 2)
            db.upsert_shopify_payout({
                "payout_id": f"88000{week}",
                "amount_cents": round((gross - fee) * 100),
                "date": _days_ago(end - 2), "status": "paid", "currency": "USD",
                "fee_amount_cents": round(fee * 100),
                "gross_amount_cents": round(gross * 100),
                "summary_json": "{}", "synced_at": _days_ago(end - 2) + "T00:00:00+00:00"})
            # The deposit that arrived at the bank, so the ledger can match it.
            db.upsert_bank_transaction({
                "source": "plaid", "transaction_id": f"demo-bank-payout-{week}",
                "account_id": "demo-checking", "date": _days_ago(end - 2),
                "authorized_date": _days_ago(end - 2),
                "amount_cents": round((gross - fee) * 100),
                "merchant_name": f"Shopify payout 88000{week}",
                "name": f"SHOPIFY PAYOUT 88000{week}", "pending": 0,
                "category_primary": "TRANSFER_IN", "category_detailed": "",
                "removed": 0, "synced_at": _days_ago(end - 2) + "T00:00:00+00:00"})
        week += 1
    try:
        ledger_sync.sync_ledger_from_sources()
    except Exception:
        pass   # the rest of the demo does not depend on it

    # --- Business customers, pipeline stages, and invoices
    people = {}
    for name, stage, email in [
            ("Alpine Ridge Resort", "key_account", "ap@alpineridge.example"),
            ("Northface Ski Club", "customer", "treasurer@northfaceclub.example"),
            ("Powder House Rentals", "customer", "billing@powderhouse.example"),
            ("Eldora Mountain School", "customer", "office@eldoraschool.example"),
            ("Boulder High Ski Team", "lead", "coach@boulderhigh.example"),
            ("Summit County Search and Rescue", "dormant", "gear@summitsar.example"),
            ("Frisco Bike and Board", "lead", "owner@friscobb.example")]:
        contact_id = db.create_contact(name, email=email)
        db.set_contact_stage(contact_id, stage)
        people[name] = contact_id

    def invoice(who, issued_days_ago, due_in, lines, send=True):
        invoice_id = db.create_invoice(
            people[who], _days_ago(issued_days_ago), _in_days(due_in), terms="Net 30")
        db.replace_invoice_lines(invoice_id, lines)
        if send:
            invoice_ledger.send_invoice(invoice_id)
        return invoice_id

    def pay(invoice_id, cents, days_ago):
        payment_id = db.record_invoice_payment(invoice_id, _days_ago(days_ago), cents)
        invoice_ledger.post_payment(invoice_id, payment_id)

    paid = invoice("Powder House Rentals", 70, -40,
                   [_line("Atomic Maverick 95 skis, rental fleet", 6, 52000),
                    _line("Salomon STH2 bindings", 6, 28000)])
    pay(paid, 480000, 38)
    overdue = invoice("Alpine Ridge Resort", 55, -25,
                      [_line("Rental ski packages, season fleet", 12, 64000)])
    invoice("Northface Ski Club", 8, 22,
            [_line("Team ski packages (skis, bindings, poles)", 18, 41000)])
    partial = invoice("Eldora Mountain School", 20, 10,
                      [_line("Youth snowboard packages", 10, 54000)])
    pay(partial, 200000, 5)
    invoice("Boulder High Ski Team", 1, 30,
            [_line("Race ski order, 6 pairs", 6, 58000)], send=False)

    template = db.create_recurring_template(
        people["Alpine Ridge Resort"], "monthly", _in_days(14),
        name="Rental fleet service plan")
    db.replace_recurring_template_lines(
        template, [_line("Monthly tuning and repair, rental fleet", 1, 45000)])
    queued = db.create_recurring_template(
        people["Powder House Rentals"], "monthly", _in_days(26),
        name="Monthly boot fitting clinic")
    db.replace_recurring_template_lines(
        queued, [_line("Boot fitting clinic", 1, 30000)])
    db.create_recurring_instance(queued, _days_ago(2))

    # --- Money that already went through the books
    for months in range(4):
        d = 95 - months * 30
        approved("Mountain View Properties", 3200.00, "expense", "6070", d, f"rent{months}")
        approved("Shopify", 79.00, "expense", "6030", d + 2, f"shop{months}")
        approved("Klaviyo", 45.00, "expense", "6030", d + 4, f"klav{months}")
        approved("Hartford Insurance", 410.00, "expense", "6000", d + 1, f"ins{months}")
    for months, (google, meta) in enumerate([(600, 300), (720, 320), (850, 310), (910, 520)]):
        d = 90 - months * 30
        approved("Google Ads", float(google), "expense", "6010", d, f"google{months}")
        approved("Meta Ads", float(meta), "expense", "6010", d + 3, f"meta{months}")
    for months, bill in enumerate([4800, 4800, 5200]):
        approved("Gusto seasonal staff payroll", float(bill), "expense", "6060",
                 70 - months * 30, f"pay{months}")
    for months, bill in enumerate([260, 310, 290]):
        approved("Xcel Energy", float(bill), "expense", "6070", 60 - months * 30, f"xcel{months}")
    for index, (vendor, amount) in enumerate([
            ("Uline packaging supplies", 214.60), ("Freight in, Atomic pallet", 380.00),
            ("Uline packaging supplies", 198.35)]):
        approved(vendor, amount, "expense", "5000", 58 - index * 21, f"cogs{index}")
    for index, d in enumerate([75, 50, 31, 17, 9]):
        approved("UPS Shipping", 140.00 + index * 22, "expense", "6040", d, f"ups{index}")
    for months in range(4):
        d = 85 - months * 28
        approved("Ski school lessons", 1800.00 + months * 350, "income",
                 lessons["code"], d, f"lesson{months}")
        approved("Equipment rentals, walk-in", 2400.00 + months * 600, "income",
                 rentals["code"], d + 5, f"rent_in{months}")
        approved("Tuning and repair counter", 640.00 + months * 120, "income",
                 repairs["code"], d + 9, f"tune{months}")

    # --- Waiting in Review
    waiting = [
        ("Atomic Skis USA", 2410.00, "expense", 2), ("Trailhead Coffee", 22.50, "expense", 1),
        ("Staples", 112.15, "expense", 3), ("UPS Store", 48.90, "expense", 3),
        ("Transfer to savings", 1000.00, "transfer", 4),
        ("Eldora Mountain School deposit", 2000.00, "income", 5),
        ("Whole Foods", 64.20, "expense", 6), ("Smith Optics", 1380.00, "expense", 7),
    ]
    for index, (vendor, amount, direction, d) in enumerate(waiting):
        db.insert_pending_item(
            source="plaid", vendor=vendor, amount=amount, date=_days_ago(d),
            suggested_category="Uncategorized Expense" if direction == "expense" else None,
            direction=direction, external_transaction_id=f"demo-wait-{index}",
            account_id="demo-checking")
    db.record_payment_match(
        overdue, "bank", "plaid:transaction:demo-alpine", 768000, _days_ago(1),
        detail="Deposit from Alpine Ridge Resort")
    db.record_payment_match(
        partial, "bank", "plaid:transaction:demo-eldora", 340000, _days_ago(3),
        detail="Deposit from Eldora Mountain School")

    # --- Follow-ups and reminders
    for index, (who, email, roster, goal, d) in enumerate([
            ("Dana Whitfield, Summit High Ski Team", "dana@summithigh.example", "22",
             "Race-ready alpine skis for the season", 2),
            ("Marcus Hale, Vail Valley Snowboard Club", "marcus@vvsc.example", "14",
             "Team boards, bindings, and helmets", 4),
            ("Ines Duarte, Telluride Adaptive Sports", "ines@telluride.example", "9",
             "Adaptive sit-ski packages", 6)]):
        db.insert_quote_followup(
            f"demo-profile-{index}", who, email, "Team equipment quote", roster, goal,
            _days_ago(d) + "T16:00:00+00:00",
            "Your team equipment quote from Summit Edge",
            f"Hi {who.split(',')[0].split()[0]},\n\nThanks for reaching out. Here is a draft quote "
            f"for {goal.lower()}. Tell me what to adjust and I will send it over.\n\nSummit Edge",
            None)
    db.insert_invoice_reminder(
        "demo-inv-2", "INV-0002", "Alpine Ridge Resort", "ap@alpineridge.example",
        "7680.00 USD", _days_ago(25), "Friendly reminder: invoice INV-0002",
        "Hi, this is a quick reminder that invoice INV-0002 for $7,680.00 was due "
        "25 days ago. Let me know if anything is holding it up.\n\nSummit Edge")

    # --- Website traffic: stored daily snapshots, as the daily check writes them
    events = ["view_item", "add_to_cart", "purchase"]
    db.set_setting("ga4_conversion_events", json.dumps(events))
    db.set_setting("ga4_property_id", "demo")
    for back in range(11, -1, -1):
        grow = 1 + (11 - back) * 0.02
        week_sessions = int(2100 * grow)
        snapshot = {
            "generated_at": _days_ago(back) + "T08:00:00+00:00",
            "conversion_events": events, "primary_conversion_event": "purchase",
            "windows": {
                "7": {
                    "traffic": {
                        "current": {"users": int(week_sessions * 0.72),
                                    "sessions": week_sessions,
                                    "page_views": int(week_sessions * 3.1)},
                        "prior": {"users": int(week_sessions * 0.7),
                                  "sessions": int(week_sessions * 0.96),
                                  "page_views": int(week_sessions * 2.9)}},
                    "events": {
                        "current": {"view_item": int(week_sessions * 1.4),
                                    "add_to_cart": int(week_sessions * 0.22),
                                    "purchase": int(week_sessions * 0.031)},
                        "prior": {"view_item": int(week_sessions * 1.3),
                                  "add_to_cart": int(week_sessions * 0.2),
                                  "purchase": int(week_sessions * 0.029)}},
                    "top_pages": [{"path": "/collections/skis", "views": 2300},
                                  {"path": "/collections/snowboards", "views": 1900}]},
                "30": {
                    "traffic": {
                        "current": {"users": 6100, "sessions": 8800, "page_views": 27400},
                        "prior": {"users": 5600, "sessions": 8100, "page_views": 25000}},
                    "events": {
                        "current": {"view_item": 12400, "add_to_cart": 1950, "purchase": 273},
                        "prior": {"view_item": 11300, "add_to_cart": 1790, "purchase": 241}},
                    "top_pages": [
                        {"path": "/collections/skis", "views": 9400},
                        {"path": "/collections/snowboards", "views": 7600},
                        {"path": "/products/atomic-maverick-95-skis", "views": 4100},
                        {"path": "/products/burton-custom-snowboard", "views": 3700},
                        {"path": "/collections/helmets-goggles", "views": 2900},
                        {"path": "/pages/boot-fitting", "views": 1600}],
                    "by_source": [
                        {"channel": "Organic Search", "sessions": 3300, "conversions": 118},
                        {"channel": "Paid Search", "sessions": 2100, "conversions": 71},
                        {"channel": "Email", "sessions": 1200, "conversions": 52},
                        {"channel": "Paid Social", "sessions": 1400, "conversions": 24},
                        {"channel": "Direct", "sessions": 800, "conversions": 8}]}}}
        db.save_ga4_snapshot(_days_ago(back), json.dumps(snapshot))

    # --- Data health: the bank synced this morning
    prefix = app._health_prefix("plaid", "demo-checking")
    db.set_setting(prefix + "last_sync_date", date.today().isoformat())
    db.set_setting(prefix + "last_transaction_date", _days_ago(1))

    # --- Sales tax nexus
    for state, revenue, threshold in (("Colorado", "184200.00", "100000.00"),
                                      ("Utah", "41200.00", "100000.00"),
                                      ("Vermont", "18900.00", "100000.00")):
        db.add_nexus_state(state, revenue, threshold)
