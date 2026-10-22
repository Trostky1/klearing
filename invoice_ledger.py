"""Invoice arithmetic and the journal entries each lifecycle event posts.

This module is the single place that knows how an invoice becomes accounting.
db.py stores rows; this decides what those rows are worth and what they post.

Three rules govern everything here, and each exists because getting it wrong
misstates someone's income:

1. **Sales tax is a liability, never revenue.** Money collected on behalf of
   a state is not yours. It credits 2200 Sales Tax Payable.
2. **Overdue posts nothing.** "Overdue" is a reporting attribute of an open
   receivable, computed from a due date at read time. It is not a
   transaction, and any system that journals it is inventing activity.
3. **Fees are recorded gross.** A $1,000 payment that nets $971 posts $1,000
   against AR, $971 to cash and $29 to fee expense. Netting understates both
   revenue and expenses and will not tie to a 1099-K.

Corrections never edit a posted entry -- the ledger has no update path by
design. A void or reversal posts a new, opposite entry under a suffixed
source reference, the same pattern ledger_sync uses for reclassification.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import db
import ledger
import ledger_sync

CREATED_BY = "invoicing"

# Where the money lands. Sourced from ledger_sync so there is one definition
# of each account code in the codebase, and so a missing account is caught by
# the same required-set guard that protects source sync.
CHECKING = ledger_sync.CHECKING
ACCOUNTS_RECEIVABLE = ledger_sync.ACCOUNTS_RECEIVABLE
SALES_REVENUE = ledger_sync.SALES_REVENUE
PAYMENT_PROCESSING_FEES = ledger_sync.PAYMENT_PROCESSING_FEES
SALES_TAX_PAYABLE = ledger_sync.SALES_TAX_PAYABLE
CUSTOMER_DEPOSITS = ledger_sync.CUSTOMER_DEPOSITS
ALLOWANCE_DOUBTFUL = ledger_sync.ALLOWANCE_DOUBTFUL
SALES_RETURNS = ledger_sync.SALES_RETURNS
BAD_DEBT_EXPENSE = ledger_sync.BAD_DEBT_EXPENSE

INVOICE_NUMBER_PREFIX_KEY = "invoice_number_prefix"
DEFAULT_INVOICE_PREFIX = "INV-"

WRITE_OFF_ALLOWANCE = "allowance"
WRITE_OFF_DIRECT = "direct"


class InvoiceError(RuntimeError):
    """An invoice cannot be posted in its current state."""


# ------------------------------------------------------------------ amounts


def _cents(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def line_totals(line):
    """One line's net and tax, both in whole cents.

    Rounds per line and then sums, rather than summing exact fractions and
    rounding once. Per-line rounding is what appears on the printed invoice,
    so it has to be what the ledger posts -- otherwise the PDF and the books
    disagree by a penny and nobody can tell which is right.
    """
    quantity_milli = int(line.get("quantity_milli") or 0)
    unit_price_cents = int(line.get("unit_price_cents") or 0)
    tax_rate_bp = int(line.get("tax_rate_bp") or 0)
    net = _cents(Decimal(quantity_milli) * Decimal(unit_price_cents) / 1000)
    tax = _cents(Decimal(net) * Decimal(tax_rate_bp) / 10000)
    return net, tax


def invoice_totals(lines):
    """{net, tax, gross} in cents for a set of lines."""
    net = tax = 0
    for line in lines:
        line_net, line_tax = line_totals(line)
        net += line_net
        tax += line_tax
    return {"net": net, "tax": tax, "gross": net + tax}


def amount_paid_cents(invoice_id):
    return sum(int(payment["amount_cents"])
               for payment in db.list_invoice_payments(invoice_id))


def credited_cents(invoice_id):
    """Credit notes only. Cash refunds return money that was already banked
    and do not reduce what the customer still owes on this invoice."""
    return sum(int(note["amount_cents"])
               for note in db.list_invoice_credit_notes(invoice_id)
               if not note["is_refund"])


def applied_deposit_cents(invoice_id):
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total "
            "FROM customer_deposits WHERE applied_invoice_id = ?",
            (invoice_id,)).fetchone()
    return int(row["total"])


def balance_cents(invoice_id, lines=None):
    """What the customer still owes: gross, less payments, credit notes and
    any deposit applied to this invoice."""
    lines = db.list_invoice_lines(invoice_id) if lines is None else lines
    gross = invoice_totals(lines)["gross"]
    return (gross - amount_paid_cents(invoice_id) - credited_cents(invoice_id)
            - applied_deposit_cents(invoice_id))


# ------------------------------------------------------------------- status


def status(invoice, balance=None, today=None):
    """The invoice's state, derived rather than stored.

    Order matters: a voided invoice is void even if a payment landed on it,
    and a written-off invoice is not merely "overdue forever".
    """
    if invoice.get("voided_at"):
        return "void"
    if invoice.get("written_off_at"):
        return "written_off"
    if not invoice.get("sent_at"):
        return "draft"
    if balance is None:
        balance = balance_cents(invoice["id"])
    if balance <= 0:
        return "paid"
    today = today or date.today().isoformat()
    if invoice["due_date"] < today:
        return "overdue"
    if balance < invoice_totals(db.list_invoice_lines(invoice["id"]))["gross"]:
        return "partial"
    return "sent"


STATUS_LABELS = {
    "draft": "Draft", "sent": "Sent", "partial": "Partly paid",
    "paid": "Paid", "overdue": "Overdue", "void": "Void",
    "written_off": "Written off",
}


def days_overdue(invoice, today=None):
    today = date.fromisoformat(today or date.today().isoformat())
    try:
        due = date.fromisoformat(invoice["due_date"])
    except (TypeError, ValueError):
        return 0
    return max(0, (today - due).days)


AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def aging_bucket(invoice, today=None):
    """Standard five-bucket AR aging, by due date."""
    overdue = days_overdue(invoice, today)
    if overdue <= 0:
        return "current"
    if overdue <= 30:
        return "1-30"
    if overdue <= 60:
        return "31-60"
    if overdue <= 90:
        return "61-90"
    return "90+"


# ------------------------------------------------------------------ posting


def _accounts():
    return {account["code"]: account["id"] for account in ledger.list_accounts()}


def _post(date_value, memo, entries, source_reference):
    """Post a balanced entry, dropping zero-amount legs.

    Zero legs are dropped rather than rejected because callers legitimately
    produce them: a tax-free invoice has no 2200 leg, a fee-free payment no
    6100 leg. Building the leg and discarding it here keeps every caller's
    shape uniform.
    """
    lines = [line for line in entries if int(line["amount"]) != 0]
    if not lines:
        return None
    accounts = _accounts()
    journal_lines = []
    for line in lines:
        amount = Decimal(int(line["amount"])) / 100
        side = "debit_amount" if line["side"] == "debit" else "credit_amount"
        journal_lines.append({"account_id": accounts[line["code"]],
                              side: amount})
    return ledger.create_sourced_journal_entry(
        date_value, memo, journal_lines, source_reference=source_reference,
        created_by=CREATED_BY)


def invoice_prefix():
    return (db.get_setting(INVOICE_NUMBER_PREFIX_KEY)
            or DEFAULT_INVOICE_PREFIX)


def send_invoice(invoice_id, sent_at=None, basis=None):
    """Mark an invoice sent, allocate its number, and post it.

    Under accrual this posts Dr AR / Cr Revenue (+ Cr Sales Tax Payable).
    Under cash basis it posts **nothing** -- revenue is not earned until the
    money arrives -- but the invoice still becomes a real, numbered, sent
    document whose balance drives AR aging as a report.

    The basis in force is stamped on the invoice so that changing the
    setting later cannot retroactively reinterpret what was posted.
    """
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise InvoiceError("That invoice no longer exists.")
    if invoice["sent_at"]:
        raise InvoiceError("That invoice has already been sent.")
    if invoice["voided_at"]:
        raise InvoiceError("A voided invoice cannot be sent.")
    lines = db.list_invoice_lines(invoice_id)
    totals = invoice_totals(lines)
    if totals["gross"] <= 0:
        raise InvoiceError(
            "An invoice needs at least one line with an amount before it can "
            "be sent.")

    basis = basis or ledger.accounting_basis()
    number = db.allocate_invoice_number(invoice_id, invoice_prefix())
    sent_at = sent_at or invoice["issue_date"]
    db.update_invoice(invoice_id, sent_at=sent_at, posted_basis=basis)

    entry = None
    if basis == "accrual":
        entry = _post(
            invoice["issue_date"],
            f"Invoice {number}",
            [{"code": ACCOUNTS_RECEIVABLE, "side": "debit",
              "amount": totals["gross"]},
             {"code": SALES_REVENUE, "side": "credit", "amount": totals["net"]},
             {"code": SALES_TAX_PAYABLE, "side": "credit",
              "amount": totals["tax"]}],
            f"invoice:{invoice_id}:issue")
    return {"number": number, "totals": totals, "basis": basis,
            "entry": entry}


def post_payment(invoice_id, payment_id):
    """Post one recorded payment.

    Accrual clears the receivable; cash basis recognizes the revenue now,
    apportioning tax by the invoice's own net/tax split so a partial payment
    does not over- or under-state the tax liability.
    """
    invoice = db.get_invoice(invoice_id)
    payment = next((p for p in db.list_invoice_payments(invoice_id)
                    if p["id"] == payment_id), None)
    if not invoice or not payment:
        raise InvoiceError("That payment no longer exists.")

    gross = int(payment["amount_cents"])
    fee = int(payment["fee_cents"])
    cash = gross - fee
    reference = f"invoice:{invoice_id}:payment:{payment_id}"
    basis = invoice["posted_basis"] or ledger.accounting_basis()
    number = invoice["invoice_number"] or f"draft {invoice_id}"

    if basis == "accrual":
        return _post(
            payment["paid_on"], f"Payment on invoice {number}",
            [{"code": CHECKING, "side": "debit", "amount": cash},
             {"code": PAYMENT_PROCESSING_FEES, "side": "debit", "amount": fee},
             {"code": ACCOUNTS_RECEIVABLE, "side": "credit", "amount": gross}],
            reference)

    totals = invoice_totals(db.list_invoice_lines(invoice_id))
    tax_share = 0
    if totals["gross"]:
        tax_share = _cents(Decimal(gross) * Decimal(totals["tax"])
                           / Decimal(totals["gross"]))
    return _post(
        payment["paid_on"], f"Payment on invoice {number}",
        [{"code": CHECKING, "side": "debit", "amount": cash},
         {"code": PAYMENT_PROCESSING_FEES, "side": "debit", "amount": fee},
         {"code": SALES_REVENUE, "side": "credit", "amount": gross - tax_share},
         {"code": SALES_TAX_PAYABLE, "side": "credit", "amount": tax_share}],
        reference)


def post_credit_note(invoice_id, credit_note_id):
    """A credit note reduces the receivable; a refund returns banked cash.

    Both debit contra-revenue rather than Revenue directly, so gross sales
    stay intact for reporting and the reduction is visible as its own line.

    Under cash basis an unpaid credit note posts nothing: no revenue was ever
    recognized, so there is nothing to reverse.
    """
    invoice = db.get_invoice(invoice_id)
    note = next((n for n in db.list_invoice_credit_notes(invoice_id)
                 if n["id"] == credit_note_id), None)
    if not invoice or not note:
        raise InvoiceError("That credit note no longer exists.")

    amount = int(note["amount_cents"])
    tax = int(note["tax_cents"])
    net = amount - tax
    is_refund = bool(note["is_refund"])
    basis = invoice["posted_basis"] or ledger.accounting_basis()
    number = invoice["invoice_number"] or f"draft {invoice_id}"
    kind = "Refund" if is_refund else "Credit note"
    reference = f"invoice:{invoice_id}:credit:{credit_note_id}"

    if basis == "cash" and not is_refund:
        return None

    credit_code = CHECKING if is_refund else ACCOUNTS_RECEIVABLE
    return _post(
        note["issued_on"], f"{kind} on invoice {number}",
        [{"code": SALES_RETURNS, "side": "debit", "amount": net},
         {"code": SALES_TAX_PAYABLE, "side": "debit", "amount": tax},
         {"code": credit_code, "side": "credit", "amount": amount}],
        reference)


def write_off(invoice_id, method=WRITE_OFF_ALLOWANCE, written_off_at=None):
    """Write an uncollectible receivable off.

    Allowance method: the expense was already recognized when the allowance
    was estimated, so the write-off itself has no P&L impact -- it clears the
    allowance against AR. Direct method books the expense now.

    Under cash basis this posts nothing: revenue was never recognized, so
    there is no receivable to remove and nothing to deduct. The invoice is
    still marked written off so it leaves the AR aging report.
    """
    if method not in (WRITE_OFF_ALLOWANCE, WRITE_OFF_DIRECT):
        raise InvoiceError("Unknown write-off method.")
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise InvoiceError("That invoice no longer exists.")
    if invoice["written_off_at"]:
        raise InvoiceError("That invoice is already written off.")

    outstanding = balance_cents(invoice_id)
    if outstanding <= 0:
        raise InvoiceError("There is nothing outstanding to write off.")

    written_off_at = written_off_at or date.today().isoformat()
    db.update_invoice(invoice_id, written_off_at=written_off_at,
                      write_off_method=method)

    basis = invoice["posted_basis"] or ledger.accounting_basis()
    if basis == "cash":
        return None

    debit_code = (ALLOWANCE_DOUBTFUL if method == WRITE_OFF_ALLOWANCE
                  else BAD_DEBT_EXPENSE)
    return _post(
        written_off_at,
        f"Write off invoice {invoice['invoice_number']}",
        [{"code": debit_code, "side": "debit", "amount": outstanding},
         {"code": ACCOUNTS_RECEIVABLE, "side": "credit",
          "amount": outstanding}],
        f"invoice:{invoice_id}:writeoff")


def estimate_allowance(amount_cents, as_of=None):
    """Periodic bad-debt estimate under the allowance method.

    Separate from write_off() because it is a judgement about the portfolio,
    not about one invoice -- typically sized from the AR aging report.
    """
    as_of = as_of or date.today().isoformat()
    return _post(
        as_of, "Bad debt allowance estimate",
        [{"code": BAD_DEBT_EXPENSE, "side": "debit", "amount": amount_cents},
         {"code": ALLOWANCE_DOUBTFUL, "side": "credit",
          "amount": amount_cents}],
        f"invoice:allowance:{as_of}")


def post_deposit_received(deposit_id):
    """Money taken before there is an invoice. A liability, not revenue --
    on either basis, because it has not been earned yet."""
    deposit = db.get_customer_deposit(deposit_id)
    if not deposit:
        raise InvoiceError("That deposit no longer exists.")
    return _post(
        deposit["received_on"], "Customer deposit received",
        [{"code": CHECKING, "side": "debit",
          "amount": deposit["amount_cents"]},
         {"code": CUSTOMER_DEPOSITS, "side": "credit",
          "amount": deposit["amount_cents"]}],
        f"deposit:{deposit_id}:receive")


def post_deposit_applied(deposit_id):
    """Move an earned deposit off the liability and against the receivable.

    Under cash basis the money was already recognized as revenue when it
    arrived, so there is no receivable to clear and this posts nothing.
    """
    deposit = db.get_customer_deposit(deposit_id)
    if not deposit or not deposit["applied_invoice_id"]:
        raise InvoiceError("That deposit is not applied to an invoice.")
    invoice = db.get_invoice(deposit["applied_invoice_id"])
    basis = (invoice or {}).get("posted_basis") or ledger.accounting_basis()
    if basis == "cash":
        return None
    return _post(
        deposit["applied_on"], "Customer deposit applied",
        [{"code": CUSTOMER_DEPOSITS, "side": "debit",
          "amount": deposit["amount_cents"]},
         {"code": ACCOUNTS_RECEIVABLE, "side": "credit",
          "amount": deposit["amount_cents"]}],
        f"deposit:{deposit_id}:apply")


def void_invoice(invoice_id, voided_at=None):
    """Void a sent invoice by reversing its issue entry.

    The original entry is never edited -- the ledger has no update path, and
    an audit trail that can be rewritten is not an audit trail. This posts
    the mirror image under a :reversal reference.
    """
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise InvoiceError("That invoice no longer exists.")
    if invoice["voided_at"]:
        raise InvoiceError("That invoice is already void.")
    if amount_paid_cents(invoice_id):
        raise InvoiceError(
            "This invoice has payments against it. Issue a refund or credit "
            "note instead of voiding it.")

    voided_at = voided_at or date.today().isoformat()
    was_sent = bool(invoice["sent_at"])
    db.update_invoice(invoice_id, voided_at=voided_at)
    if not was_sent or (invoice["posted_basis"] or "accrual") == "cash":
        return None

    totals = invoice_totals(db.list_invoice_lines(invoice_id))
    return _post(
        voided_at, f"Void invoice {invoice['invoice_number']}",
        [{"code": SALES_REVENUE, "side": "debit", "amount": totals["net"]},
         {"code": SALES_TAX_PAYABLE, "side": "debit", "amount": totals["tax"]},
         {"code": ACCOUNTS_RECEIVABLE, "side": "credit",
          "amount": totals["gross"]}],
        f"invoice:{invoice_id}:issue:reversal")
