"""Recurring invoices: when a cycle is due, and what approving one does.

Named to stay clear of `recurring.py`, which is an unrelated feature --
it detects recurring *expenses* from bank history for the Vendors tab
and never acts on what it finds.

The shape of this feature is set by one decision: **nothing sends
itself**. A due cycle becomes a queued instance in Review, and only a
person approving it creates a real invoice. A recurring invoice is still
money leaving the building, and being on a schedule does not earn it a
bypass around triage. That is why this module generates and queues, and
why the only thing that posts is `approve()`, which hands off to
`invoice_ledger.send_invoice` -- the same call a manual invoice makes,
so the ledger cannot tell the two apart.

Three rules govern the rest:

1. **Catch-up never drops a cycle.** If the app was closed across four
   weekly due-dates, four instances are queued, not one. Each represents
   billing that should have happened, and collapsing them would be the
   silent skip the design forbids.

2. **Monthly clamping preserves the anchor.** A template anchored on the
   31st lands on the 28th in February and back on the 31st in March. The
   anchor is stored separately from `next_due_date` precisely because
   clamping is lossy -- carrying February's clamped 28th forward would
   walk the schedule backwards permanently after one short month.

3. **An unbillable template is skipped and logged, never guessed at.** A
   template whose lines total zero produces a recorded skip with a
   reason, not a zero-value invoice.
"""

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import db

WEEKLY = "weekly"
MONTHLY = "monthly"
CUSTOM = "custom"

# A single catch-up run is bounded so that a template left paused-but-due
# for years cannot queue thousands of rows in one pass. The cap is per
# template per run: anything still outstanding is queued by the next run,
# so nothing is dropped -- it arrives across several runs instead of one.
MAX_CATCH_UP_PER_RUN = 60


class RecurringInvoiceError(ValueError):
    """A template cannot produce an invoice in its current state."""


def _today():
    return date.today()


def _parse(value):
    return date.fromisoformat(str(value)[:10])


def describe(template):
    """How a template's cadence reads in the UI."""
    recurrence = template["recurrence"]
    if recurrence == WEEKLY:
        return "Weekly"
    if recurrence == MONTHLY:
        anchor = template.get("anchor_day")
        return f"Monthly on the {_ordinal(anchor)}" if anchor else "Monthly"
    days = template.get("interval_days")
    return f"Every {days} days" if days else "Custom"


def _ordinal(day):
    if 11 <= int(day) % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(int(day) % 10, 'th') }"


def next_due_after(current, recurrence, interval_days=None, anchor_day=None):
    """The cycle following `current`.

    Monthly is the only interesting case. It advances by calendar month
    and then places the day at min(anchor, days in that month), so the
    anchor is what recurs rather than whatever the previous clamp
    happened to produce.
    """
    current = _parse(current)
    if recurrence == WEEKLY:
        return current + timedelta(days=7)
    if recurrence == CUSTOM:
        days = int(interval_days or 0)
        if days < 1:
            raise RecurringInvoiceError(
                "A custom recurrence needs an interval of at least one day.")
        return current + timedelta(days=days)
    if recurrence != MONTHLY:
        raise RecurringInvoiceError(f"Unknown recurrence: {recurrence!r}")

    anchor = int(anchor_day or current.day)
    year, month = current.year, current.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return date(year, month, min(anchor, monthrange(year, month)[1]))


def due_dates_through(template, today=None):
    """Every cycle from the template's next_due_date up to and including
    today, oldest first.

    This is what makes catch-up faithful: it returns the whole missed
    run, not just the most recent one.
    """
    today = today or _today()
    if isinstance(today, str):
        today = _parse(today)
    cursor = _parse(template["next_due_date"])
    dates = []
    while cursor <= today and len(dates) < MAX_CATCH_UP_PER_RUN:
        dates.append(cursor.isoformat())
        cursor = next_due_after(cursor, template["recurrence"],
                                template.get("interval_days"),
                                template.get("anchor_day"))
    return dates, cursor.isoformat()


def _template_is_billable(template_id):
    """Whether a template's lines could produce a real invoice.

    Returns an error string, or None when it is fine. Checked before
    queueing so an unbillable template is visible as a recorded skip
    rather than as an approval that fails later.
    """
    lines = db.list_recurring_template_lines(template_id)
    if not lines:
        return "The template has no line items."
    gross = sum(int(line["quantity_milli"]) * int(line["unit_price_cents"])
                for line in lines)
    if gross <= 0:
        return "The template's lines total zero."
    return None


def run_due_checks(today=None):
    """Queue every cycle that has come due, for every active template.

    Called on app launch (so a machine that was off catches up straight
    away) and from the existing daily 8am job. Both are safe to run in
    any order or twice: create_recurring_instance is INSERT OR IGNORE
    against a UNIQUE (template_id, due_date), so a cycle can only ever
    be queued once.

    Returns a summary rather than printing, so the caller decides where
    it is reported.
    """
    today = today or _today()
    summary = {"templates": 0, "queued": 0, "already_queued": 0,
               "skipped": 0, "skipped_templates": []}

    for template in db.list_recurring_templates(active_only=True):
        summary["templates"] += 1
        dates, following = due_dates_through(template, today)
        if not dates:
            continue

        problem = _template_is_billable(template["id"])
        if problem:
            # The schedule still advances. Leaving next_due_date behind
            # would re-report the same broken cycle every run, burying
            # the queue in duplicates of one fixable problem.
            db.update_recurring_template(template["id"],
                                         next_due_date=following)
            for due_date in dates:
                instance_id = db.create_recurring_instance(template["id"],
                                                           due_date)
                if instance_id is None:
                    summary["already_queued"] += 1
                    continue
                db.set_recurring_instance_status(
                    instance_id, "skipped", skip_reason=problem)
                summary["skipped"] += 1
            summary["skipped_templates"].append(
                {"template_id": template["id"],
                 "name": template["name"], "reason": problem})
            continue

        for due_date in dates:
            if db.create_recurring_instance(template["id"], due_date) is None:
                summary["already_queued"] += 1
            else:
                summary["queued"] += 1
        db.update_recurring_template(template["id"], next_due_date=following)

    return summary


def approve(instance_id, today=None):
    """Turn one queued cycle into a real, sent invoice.

    The whole point of this function is that it does nothing special.
    It creates an invoice and calls invoice_ledger.send_invoice, which
    is the identical path a manually authored invoice takes -- same
    number allocation, same basis stamping, same journal entries. There
    is deliberately no recurring-invoice posting path to diverge from
    the manual one.
    """
    import invoice_ledger

    instance = db.get_recurring_instance(instance_id)
    if instance is None:
        raise RecurringInvoiceError("That queued invoice no longer exists.")
    if instance["status"] != "pending_review":
        raise RecurringInvoiceError("That queued invoice has already been "
                                    "decided.")

    template = db.get_recurring_template(instance["template_id"])
    if template is None:
        raise RecurringInvoiceError("Its template no longer exists.")

    problem = _template_is_billable(template["id"])
    if problem:
        raise RecurringInvoiceError(problem)

    issue_date = instance["due_date"]
    due_date = (_parse(issue_date)
                + timedelta(days=int(template["due_in_days"] or 0))).isoformat()

    invoice_id = db.create_invoice(
        template["contact_id"], issue_date, due_date,
        terms=template["terms"], notes=template["notes"])
    db.replace_invoice_lines(
        invoice_id, db.list_recurring_template_lines(template["id"]))
    result = invoice_ledger.send_invoice(invoice_id, sent_at=issue_date)
    db.set_recurring_instance_status(instance_id, "approved_sent",
                                     invoice_id=invoice_id)
    return {"invoice_id": invoice_id, "number": result["number"],
            "totals": result["totals"], "basis": result["basis"]}


def skip(instance_id, reason="Skipped in Review."):
    """Decline one cycle without touching the template's schedule.

    Non-destructive in the sense that matters here: the template keeps
    running and next month arrives as normal. Skipping January does not
    cancel the subscription.
    """
    instance = db.get_recurring_instance(instance_id)
    if instance is None:
        raise RecurringInvoiceError("That queued invoice no longer exists.")
    if instance["status"] != "pending_review":
        raise RecurringInvoiceError("That queued invoice has already been "
                                    "decided.")
    db.set_recurring_instance_status(instance_id, "skipped",
                                     skip_reason=reason)


def preview_totals(template_id):
    """What a template would bill, priced by invoice_ledger.

    Uses the same arithmetic the real invoice will, so the number shown
    in Review is the number that posts rather than a second estimate of
    it.
    """
    import invoice_ledger
    return invoice_ledger.invoice_totals(
        db.list_recurring_template_lines(template_id))
