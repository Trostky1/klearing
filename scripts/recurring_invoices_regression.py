"""Offline regression coverage for recurring invoices.

Four things are guarded, each for its own reason:

1. **A recurring invoice is indistinguishable from a manual one to the
   ledger.** Asserted by creating both with identical data and comparing
   their journal lines directly, not by inspecting the recurring path in
   isolation. A parallel posting path is the failure this prevents, and
   it would look completely fine from inside the recurring code.

2. **Catch-up never drops a cycle and never double-generates one.** The
   app being closed across four due-dates must queue four instances, and
   running the check again -- launch and the daily job can fire seconds
   apart -- must queue none.

3. **Monthly clamping preserves the anchor.** A template on the 31st
   lands on the 28th in February and returns to the 31st in March. A
   naive clamp walks the schedule backwards permanently after one short
   month, and nothing downstream would notice.

4. **Nothing sends itself.** Generating queues; only approve() posts.
   Asserted by counting journal_entries across a generation run.

Isolated temp data dir, no network, no credentials.

Run it:

    venv/Scripts/python scripts/recurring_invoices_regression.py   # Windows
    venv/bin/python scripts/recurring_invoices_regression.py       # macOS/Linux

Exits 0 if every check passes, 1 if any fails (prints which).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DATA = tempfile.TemporaryDirectory(prefix="klearing-recurring-",
                                    ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db  # noqa: E402
import invoice_ledger  # noqa: E402
import ledger  # noqa: E402
import recurring_invoices as ri  # noqa: E402
from migrations.run_migrations import run_migrations  # noqa: E402

_failures = []

LINES = [
    {"description": "Retainer", "quantity_milli": 1000,
     "unit_price_cents": 50000, "tax_rate_bp": 875},
    {"description": "Labour", "quantity_milli": 2500,
     "unit_price_cents": 10000, "tax_rate_bp": 0},
]


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


def posted_lines(invoice_id):
    """The chart codes and amounts one invoice's issue entry posted."""
    with db.get_connection() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT a.code, jl.debit_amount, jl.credit_amount "
            "FROM journal_entries je "
            "JOIN journal_lines jl ON jl.journal_entry_id = je.id "
            "JOIN accounts a ON a.id = jl.account_id "
            "WHERE je.source_reference = ? ORDER BY a.code",
            (f"invoice:{invoice_id}:issue",)).fetchall()]


def template(contact_id, recurrence, first_due, **kwargs):
    lines = kwargs.pop("lines", LINES)
    template_id = db.create_recurring_template(
        contact_id, recurrence, first_due, **kwargs)
    if lines:
        db.replace_recurring_template_lines(template_id, lines)
    return template_id


def main():
    db.init_db()
    run_migrations(report=False)
    contact = db.create_contact("Acme Corp", email="ap@acme.com")

    print("\nMonthly clamping preserves the anchor")
    walk, cursor = [], "2026-01-31"
    for _ in range(5):
        cursor = ri.next_due_after(cursor, "monthly", anchor_day=31).isoformat()
        walk.append(cursor)
    check("a 31st template clamps to February's last day",
          walk[0] == "2026-02-28", walk[0])
    check("and returns to the 31st in March rather than staying clamped",
          walk[1] == "2026-03-31", walk[1])
    check("30-day months clamp without dragging the anchor down",
          walk[2:4] == ["2026-04-30", "2026-05-31"], str(walk[2:4]))
    check("February 29 is used in a leap year",
          ri.next_due_after("2028-01-31", "monthly",
                            anchor_day=31).isoformat() == "2028-02-29")
    check("the year boundary rolls over",
          ri.next_due_after("2026-12-31", "monthly",
                            anchor_day=31).isoformat() == "2027-01-31")
    check("weekly advances seven days",
          ri.next_due_after("2026-03-01", "weekly").isoformat() == "2026-03-08")
    check("custom advances by its interval",
          ri.next_due_after("2026-03-01", "custom",
                            interval_days=10).isoformat() == "2026-03-11")

    refused = []
    for bad in ({"recurrence": "custom", "interval_days": 0},
                {"recurrence": "custom", "interval_days": None},
                {"recurrence": "quarterly"}):
        try:
            ri.next_due_after("2026-03-01", bad.pop("recurrence"), **bad)
        except ri.RecurringInvoiceError:
            refused.append(True)
    check("a zero, missing, or unknown interval is refused, never defaulted",
          len(refused) == 3, f"{len(refused)} of 3 refused")

    print("\nCatch-up queues every missed cycle, exactly once")
    weekly = template(contact, "weekly", "2026-01-01", name="Weekly retainer")
    before = journal_entry_count()
    first = ri.run_due_checks(today="2026-01-29")
    check("four weeks closed queues all five due cycles",
          first["queued"] == 5, f"queued {first['queued']}")
    check("the template's next date moves past today",
          db.get_recurring_template(weekly)["next_due_date"] == "2026-02-05",
          db.get_recurring_template(weekly)["next_due_date"])

    second = ri.run_due_checks(today="2026-01-29")
    check("running the check again queues nothing new",
          second["queued"] == 0, f"queued {second['queued']}")
    check("still exactly five instances",
          len(db.list_recurring_instances("pending_review")) == 5,
          str(len(db.list_recurring_instances("pending_review"))))

    # The real race: launch and the daily job both reaching the insert
    # before either advanced next_due_date. The UNIQUE constraint, not
    # the date bookkeeping, is what has to hold here.
    duplicate = db.create_recurring_instance(weekly, "2026-01-08")
    check("a second insert for the same cycle is refused by the constraint",
          duplicate is None, str(duplicate))

    print("\nGenerating queues, it does not post")
    check("no journal entry was created by generation",
          journal_entry_count() == before,
          f"{before} before, {journal_entry_count()} after")
    check("every queued instance is pending, none sent",
          all(row["status"] == "pending_review"
              for row in db.list_recurring_instances()),
          str({row["status"] for row in db.list_recurring_instances()}))

    print("\nAn unbillable template is skipped and logged, never guessed at")
    empty = template(contact, "monthly", "2026-01-15", name="Empty",
                     anchor_day=15, lines=[])
    result = ri.run_due_checks(today="2026-01-20")
    check("a template with no lines produces a recorded skip",
          result["skipped"] == 1, f"skipped {result['skipped']}")
    skipped = [row for row in db.list_recurring_instances("skipped")]
    check("the skip carries a reason a person can act on",
          skipped and "no line items" in skipped[0]["skip_reason"],
          str(skipped))
    check("its schedule still advances so it does not re-report forever",
          db.get_recurring_template(empty)["next_due_date"] > "2026-01-20",
          db.get_recurring_template(empty)["next_due_date"])

    zero = template(contact, "weekly", "2026-01-10", name="Zero value",
                    lines=[{"description": "Free", "quantity_milli": 1000,
                            "unit_price_cents": 0, "tax_rate_bp": 0}])
    ri.run_due_checks(today="2026-01-12")
    zero_rows = [row for row in db.list_recurring_instances("skipped")
                 if row["template_id"] == zero]
    check("a template whose lines total zero is skipped, not billed for zero",
          zero_rows and "total zero" in zero_rows[0]["skip_reason"],
          str(zero_rows))

    print("\nApproving posts exactly what a manual invoice posts")
    manual = db.create_invoice(contact, "2026-06-01", "2026-07-01",
                               terms="Net 30")
    db.replace_invoice_lines(manual, LINES)
    manual_result = invoice_ledger.send_invoice(manual, sent_at="2026-06-01")

    monthly = template(contact, "monthly", "2026-06-01", name="Retainer",
                       anchor_day=1, terms="Net 30", due_in_days=30)
    ri.run_due_checks(today="2026-06-01")
    instance = [row for row in db.list_recurring_instances("pending_review")
                if row["template_id"] == monthly][0]
    recurring_result = ri.approve(instance["id"])

    check("the journal lines are identical, code for code and cent for cent",
          posted_lines(manual) == posted_lines(recurring_result["invoice_id"]),
          f"{posted_lines(manual)} vs "
          f"{posted_lines(recurring_result['invoice_id'])}")
    check("the totals are identical",
          manual_result["totals"] == recurring_result["totals"],
          str(recurring_result["totals"]))
    check("the accounting basis is stamped the same",
          manual_result["basis"] == recurring_result["basis"],
          recurring_result["basis"])
    check("the recurring invoice gets a real allocated number",
          bool(re.match(r"^INV-\d+$", recurring_result["number"])),
          recurring_result["number"])
    check("tax posted as a liability, not revenue",
          any(code == "2200" and credit > 0
              for code, _, credit in posted_lines(recurring_result["invoice_id"])),
          str(posted_lines(recurring_result["invoice_id"])))

    approved = db.get_recurring_instance(instance["id"])
    check("the instance records which invoice it became",
          approved["status"] == "approved_sent"
          and approved["invoice_id"] == recurring_result["invoice_id"],
          str(dict(approved)))

    print("\nDecisions are final per cycle, but never break the schedule")
    reapproved = False
    try:
        ri.approve(instance["id"])
    except ri.RecurringInvoiceError:
        reapproved = True
    check("an already-approved cycle cannot be approved twice", reapproved)

    pending = db.list_recurring_instances("pending_review")[0]
    template_before = db.get_recurring_template(
        pending["template_id"])["next_due_date"]
    ri.skip(pending["id"])
    check("skipping marks the cycle skipped",
          db.get_recurring_instance(pending["id"])["status"] == "skipped")
    check("skipping does not disturb the template's future schedule",
          db.get_recurring_template(
              pending["template_id"])["next_due_date"] == template_before,
          template_before)
    reskipped = False
    try:
        ri.skip(pending["id"])
    except ri.RecurringInvoiceError:
        reskipped = True
    check("an already-decided cycle cannot be skipped again", reskipped)

    print("\nPausing stops generation without discarding the template")
    paused = template(contact, "weekly", "2026-01-01", name="Paused")
    db.update_recurring_template(paused, active=0)
    queued_before = len(db.list_recurring_instances())
    ri.run_due_checks(today="2026-02-01")
    check("a paused template queues nothing",
          len(db.list_recurring_instances()) == queued_before,
          f"{queued_before} -> {len(db.list_recurring_instances())}")
    check("and still exists to be resumed",
          db.get_recurring_template(paused) is not None)

    print("\nThe ledger still ties out")
    # trial_balance() returns one row per account; the ledger ties when
    # every posted debit is matched by a credit somewhere.
    trial = ledger.trial_balance()
    debits = sum(row["debit_total"] for row in trial)
    credits = sum(row["credit_total"] for row in trial)
    check("debits equal credits after a full generate-approve cycle",
          debits == credits, f"{debits} vs {credits}")
    check("the cycle actually posted something to tie",
          debits > 0, str(debits))

    print("\nThe app binds to localhost only")
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    check("main.py contains no 0.0.0.0 binding",
          "0.0.0.0" not in source)
    check("the one backend runner uses the shared loopback binding",
          'LOCAL_BIND_HOST = "127.0.0.1"' in source
          and source.count("host=LOCAL_BIND_HOST") == 1
          and "def _run_backend(" in source
          and "return _run_backend()" in source,
          f'{source.count("host=LOCAL_BIND_HOST")} shared bindings found')

    print(f"\n{len(_failures)} failed")
    if _failures:
        print("FAIL: " + "; ".join(_failures))
        return 1
    print("PASS: all recurring invoice regression checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
