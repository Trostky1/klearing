"""Demo mode: a fake business to click through, kept apart from real data.

"Take a look" on the first Setup screen starts a demo. It points Klearing at
its own folder (``<data dir>/demo``), fills it with a made-up business, and
shows a banner until the visitor exits. Real data is never read or written
while a demo is open, and no credentials are stored.

Every date in the demo is relative to today, so the demo never goes stale.
Each visit starts a fresh demo.
"""

import gc
import shutil
import time
from datetime import date, timedelta

import paths

DEMO_FOLDER = "demo"
DEMO_BUSINESS = "Harbor & Pine Studio"


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


def seed():
    """Fill the open (demo) data folder with a small, believable business."""
    # Imported here: app imports this module, so a top-level import would loop.
    import app
    import business_registry
    import db
    import invoice_ledger
    import ledger

    db.init_db()
    business_registry.upsert_business(DEMO_BUSINESS, make_active=True)
    db.set_setting("business_name", DEMO_BUSINESS)
    db.set_setting("setup_complete", "1")

    # One bank account, mapped to the Checking ledger account.
    db.upsert_plaid_account({
        "account_id": "demo-checking", "name": "Operating Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "4242", "synced_at": date.today().isoformat() + "T00:00:00+00:00",
    })
    db.set_bank_ledger_account(
        "plaid", "demo-checking", ledger.account_by_code("1010")["id"])

    # --- Contacts and pipeline stages
    people = {}
    for name, stage in [("Acme Corp", "key_account"), ("Bolt Supply", "customer"),
                        ("Cedar Studio", "customer"), ("Delta Works", "lead"),
                        ("Echo Labs", "lead"), ("Fern & Co", "dormant"),
                        ("Gable Goods", None)]:
        slug = name.split()[0].lower()
        contact_id = db.create_contact(name, email=f"billing@{slug}.example")
        if stage:
            db.set_contact_stage(contact_id, stage)
        people[slug] = contact_id

    # --- Invoices in every state
    def invoice(who, issued_days_ago, due_in, lines, send=True):
        invoice_id = db.create_invoice(
            people[who], _days_ago(issued_days_ago), _in_days(due_in),
            terms="Net 30")
        db.replace_invoice_lines(invoice_id, lines)
        if send:
            invoice_ledger.send_invoice(invoice_id)
        return invoice_id

    def pay(invoice_id, cents, days_ago):
        payment_id = db.record_invoice_payment(
            invoice_id, _days_ago(days_ago), cents)
        invoice_ledger.post_payment(invoice_id, payment_id)

    paid = invoice("acme", 75, -45, [_line("Brand refresh, phase 1", 1, 420000)])
    pay(paid, 420000, 40)
    invoice("acme", 40, -10, [_line("Website design", 2, 46800)])       # overdue
    invoice("bolt", 6, 24, [_line("Monthly retainer", 1, 150000)])       # sent
    partial = invoice("cedar", 20, 10, [_line("Packaging design", 1, 240000)])
    pay(partial, 100000, 5)                                              # partly paid
    invoice("delta", 1, 30, [_line("Logo options", 3, 15000)], send=False)  # draft

    template = db.create_recurring_template(
        people["bolt"], "monthly", _in_days(18), name="Monthly retainer")
    db.replace_recurring_template_lines(
        template, [_line("Monthly retainer", 1, 150000)])

    # --- Money that already went through the books (approved, then posted)
    def approved(vendor, amount, direction, code, days_ago, key):
        item_id = db.insert_pending_item(
            source="plaid", vendor=vendor, amount=amount,
            date=_days_ago(days_ago), suggested_category=None,
            direction=direction, external_transaction_id=f"demo-{key}",
            account_id="demo-checking")
        item = db.get_item(item_id)
        account, name = app.resolve_submitted_category(code)
        app._post_one_approved(item, name, account, "", None)

    approved("Client deposit, Gable Goods", 2800.00, "income", "4100", 52, "in1")
    approved("Client deposit, Bolt Supply", 1500.00, "income", "4100", 24, "in2")
    approved("Client deposit, Cedar Studio", 1750.00, "income", "4100", 9, "in3")
    approved("Studio rent", 1200.00, "expense", "6070", 55, "rent1")
    approved("Studio rent", 1200.00, "expense", "6070", 25, "rent2")
    approved("Adobe", 54.99, "expense", "6030", 50, "adobe1")
    approved("Adobe", 54.99, "expense", "6030", 20, "adobe2")
    approved("Adobe", 129.99, "expense", "6030", 3, "adobe3")     # a spend jump
    approved("Figma", 45.00, "expense", "6030", 28, "figma1")
    approved("Google Ads", 310.00, "expense", "6010", 33, "ads1")
    approved("Google Ads", 420.00, "expense", "6010", 4, "ads2")
    approved("Office Depot", 86.40, "expense", "6040", 14, "office1")

    # --- Still waiting for review
    waiting = [
        ("Corner Cafe", 18.40, "expense", 2), ("Acme Corp payment", 936.00, "income", 1),
        ("Canva", 14.99, "expense", 3), ("Owner transfer", 500.00, "transfer", 4),
        ("Staples", 62.15, "expense", 5), ("Uber", 23.60, "expense", 6),
    ]
    for index, (vendor, amount, direction, days_ago) in enumerate(waiting):
        db.insert_pending_item(
            source="plaid", vendor=vendor, amount=amount,
            date=_days_ago(days_ago), suggested_category="Uncategorized Expense"
            if direction == "expense" else None,
            direction=direction, external_transaction_id=f"demo-wait-{index}",
            account_id="demo-checking")
