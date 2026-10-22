"""
Unpaid invoice reminders for Klearing.

Runs as part of the daily check (called from check_bank.run_daily,
also runnable standalone). Read-only: it drafts payment reminders for
OVERDUE invoices, which wait for human review on the Invoices tab.
Nothing is ever emailed to a customer without a click.

Reads Klearing's own invoices. It used to query Wave over GraphQL for a
list of overdue invoices, which meant the reminder that chased a customer
depended on an external service being reachable, and on the invoice
having been raised there rather than here. Overdue is a derived state
(invoice_ledger.status), so the local answer is the authoritative one.

Rules:
- A reminder is drafted for an overdue invoice when it isn't tracked
  yet, or when its last reminder was sent (or skipped) 7+ days ago
  and it's still overdue.
- Any tracked invoice that is no longer overdue gets its rows flipped to
  status "paid".

"""

import sys
from datetime import datetime, timedelta, timezone

import db
import business_registry
import email_utils
import invoice_ledger

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REDRAFT_AFTER_DAYS = 7


def fetch_overdue_invoices():
    """Every overdue invoice, flattened to what a reminder needs.

    Overdue is derived from the send date, the due date, and the open
    balance (invoice_ledger.status), not stored -- so this asks the same
    question the Invoices screen does and cannot disagree with it.
    """
    overdue = []
    for invoice in db.list_invoices(include_voided=False):
        balance = invoice_ledger.balance_cents(invoice["id"])
        if invoice_ledger.status(invoice, balance) != "overdue":
            continue
        contact = db.get_contact(invoice["contact_id"]) or {}
        overdue.append({
            "id": invoice["id"],
            "number": invoice.get("invoice_number") or "",
            "due_date": invoice["due_date"],
            "amount_due": f"{balance / 100:.2f}",
            "currency": invoice.get("currency") or "USD",
            "customer_name": contact.get("display_name") or "",
            "customer_email": contact.get("email") or "",
        })
    return overdue


def draft_reminder(invoice, business_name=""):
    """A friendly payment reminder, signed for the business the invoice
    belongs to. No late fees, no threats."""
    number = invoice["number"]
    amount = invoice["amount_due"]
    currency = invoice["currency"]
    due_date = invoice["due_date"]
    customer_name = invoice["customer_name"] or "there"
    first_name = customer_name.split()[0] if customer_name.strip() else "there"

    subject = f"Payment reminder for Invoice #{number}"

    db.init_db()
    business = (business_name
                or (db.get_setting("business_name") or "").strip())
    signature = email_utils.sender_name()
    if business and business != signature:
        signature = f"{signature}\n{business}"

    body = (
        f"Hi {first_name},\n"
        f"\n"
        f"Just a friendly reminder that Invoice #{number} has an outstanding "
        f"balance of {amount} {currency}, which was due on {due_date}.\n"
        f"\n"
        f"If payment is already on its way, please disregard this note. "
        f"Otherwise, you can settle it whenever convenient, and let me know "
        f"if you have any questions or need a fresh copy of the invoice.\n"
        f"\n"
        f"Thanks so much,\n"
        f"{signature}"
    )
    return subject, body


def needs_new_draft(invoice_id, now):
    """
    True when this overdue invoice should get a (new) reminder draft:
    never tracked, or last handled (sent/skipped) 7+ days ago. A row
    still sitting in pending_review means one is already waiting.
    """
    latest = db.latest_invoice_reminder(invoice_id)
    if latest is None:
        return True
    if latest["status"] == "pending_review":
        return False  # already waiting for review
    if latest["status"] == "paid":
        # Was resolved before but is overdue again (e.g. payment
        # reversed) - treat like new.
        return True
    # sent or skipped: redraft once the 7-day clock expires
    if not latest["last_sent_at"]:
        return True
    try:
        last_handled = datetime.fromisoformat(latest["last_sent_at"])
    except ValueError:
        return True
    if last_handled.tzinfo is None:
        last_handled = last_handled.replace(tzinfo=timezone.utc)
    return now - last_handled >= timedelta(days=REDRAFT_AFTER_DAYS)


def run_invoice_check():
    """Draft reminders for overdue invoices. Returns a summary dict:
    drafted count, rows marked paid, and the current overdue total.

    No longer loops over configured businesses. That loop existed because
    Wave kept a separate invoice book per business; Klearing's invoices
    table is single-booked, so there is one set to check. Reminder rows
    are still stamped with the active business so the scoping already in
    mark_invoice_reminders_paid keeps working.
    """
    db.init_db()
    active = business_registry.active_business()
    business_id = active["id"] if active else ""
    business_name = active["name"] if active else ""

    now = datetime.now(timezone.utc)
    invoices = fetch_overdue_invoices()
    drafted = 0

    # Anything tracked that is no longer overdue has been resolved.
    marked_paid = db.mark_invoice_reminders_paid(
        [inv["id"] for inv in invoices], business_id=business_id)

    for invoice in invoices:
        if not invoice["customer_email"]:
            print(f'  - Invoice #{invoice["number"]}: customer has no email '
                  f"on file, skipping draft.")
            continue
        if not needs_new_draft(invoice["id"], now):
            continue
        subject, body = draft_reminder(invoice, business_name)
        db.insert_invoice_reminder(
            wave_invoice_id=invoice["id"],
            invoice_number=invoice["number"],
            customer_name=invoice["customer_name"],
            customer_email=invoice["customer_email"],
            amount_due=f'{invoice["amount_due"]} {invoice["currency"]}',
            due_date=invoice["due_date"],
            draft_subject=subject,
            draft_body=body,
            business_id=business_id,
        )
        drafted += 1
        print(f'  - Invoice #{invoice["number"]}: reminder drafted.')

    print(f"Invoice reminders: {len(invoices)} overdue invoice(s), "
          f"{drafted} reminder(s) drafted, {marked_paid} row(s) marked paid.")
    return {"overdue": len(invoices), "drafted": drafted,
            "marked_paid": marked_paid}


def main() -> int:
    try:
        run_invoice_check()
    except RuntimeError as exc:
        # No network call left to fail, so RequestException is gone from
        # here along with the Wave query that raised it.
        print(f"Invoice reminder check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
