"""Overdue invoice reminders are drafted from Klearing's own invoices.

The daily job used to ask Wave over GraphQL for a list of OVERDUE
invoices. That meant chasing a customer depended on an external service
being reachable, and only worked for invoices raised in Wave rather than
here. Overdue is a derived state -- send date, due date, open balance --
so the local answer was always the authoritative one.

What is asserted is the selection: which invoices produce a reminder and
which do not. The drafting itself, the 7-day redraft window, and the
review-before-send rule are unchanged by the migration and covered where
they always were.

Nothing is emailed. Reminders land as drafts for review, which is the
same contract as before.

Offline: isolated temp data dir, no network, no credentials.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-invoice-reminders-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import invoice_ledger as il  # noqa: E402
import check_unpaid_invoices as cui  # noqa: E402

PAST = "2026-01-31"      # due date already gone
FUTURE = "2099-01-31"    # due date still ahead


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return 0 if condition else 1


def invoice_for(name, email, due, *, send=True, cents=50000):
    """One invoice for a fresh contact, sent unless told otherwise."""
    contact = db.create_contact(name, email=email)
    invoice_id = db.create_invoice(contact, "2026-01-01", due, terms="Net 30")
    db.replace_invoice_lines(invoice_id, [
        {"description": "Services", "quantity_milli": 1000,
         "unit_price_cents": cents, "tax_rate_bp": 0}])
    if send:
        il.send_invoice(invoice_id)
    return invoice_id


def drafted_ids():
    return {row["wave_invoice_id"] for row in db.list_invoice_reminders()}


def main():
    failures = 0
    db.init_db()

    overdue = invoice_for("Late Payer", "late@example.com", PAST)
    not_yet_due = invoice_for("Prompt Payer", "prompt@example.com", FUTURE)
    never_sent = invoice_for("Draft Only", "draft@example.com", PAST,
                             send=False)
    no_email = invoice_for("No Contact Details", "", PAST)

    paid = invoice_for("Settled", "settled@example.com", PAST)
    payment = db.record_invoice_payment(paid, "2026-02-01", 50000)
    il.post_payment(paid, payment)

    print("\nOnly genuinely overdue invoices are selected")

    selected = {inv["id"] for inv in cui.fetch_overdue_invoices()}
    failures += check("an overdue invoice is selected", overdue in selected)
    failures += check("one not yet due is not", not_yet_due not in selected)
    failures += check("an unsent draft is not", never_sent not in selected)
    failures += check("a paid one is not", paid not in selected)

    print("\nDrafting")

    summary = cui.run_invoice_check()
    reminders = drafted_ids()
    failures += check("the overdue invoice gets a reminder",
                      str(overdue) in {str(r) for r in reminders})
    failures += check("a contact with no email is skipped",
                      str(no_email) not in {str(r) for r in reminders})
    failures += check("the count reported matches what was drafted",
                      summary["drafted"] == len(reminders))
    failures += check("nothing was sent, only drafted",
                      all(row["status"] == "pending_review"
                          for row in db.list_invoice_reminders()))

    print("\nThe redraft window still holds")

    before = len(db.list_invoice_reminders())
    cui.run_invoice_check()
    failures += check("a second run does not duplicate a pending draft",
                      len(db.list_invoice_reminders()) == before)

    # Mark it handled but leave the clock inside the window.
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE invoice_reminders SET status = 'sent', last_sent_at = ? "
            "WHERE wave_invoice_id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
             str(overdue)))
    cui.run_invoice_check()
    failures += check("nor redraft before the 7 days are up",
                      len(db.list_invoice_reminders()) == before)

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE invoice_reminders SET last_sent_at = ? "
            "WHERE wave_invoice_id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
             str(overdue)))
    cui.run_invoice_check()
    failures += check("but does once they are",
                      len(db.list_invoice_reminders()) == before + 1)

    print("\nPaying an invoice resolves its reminders")

    settle = db.record_invoice_payment(overdue, "2026-02-01", 50000)
    il.post_payment(overdue, settle)
    result = cui.run_invoice_check()
    failures += check("it drops out of the overdue set",
                      overdue not in {inv["id"]
                                      for inv in cui.fetch_overdue_invoices()})
    failures += check("and its reminder rows are marked paid",
                      result["marked_paid"] >= 1
                      and all(row["status"] == "paid"
                              for row in db.list_invoice_reminders()
                              if str(row["wave_invoice_id"]) == str(overdue)))

    print(f"\n{failures} failed")
    if failures:
        return 1
    print("PASS: invoice reminders come from Klearing's own invoices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
