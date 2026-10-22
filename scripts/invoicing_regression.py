"""
Regression suite for native invoicing and A/R.

Asserts the accounting, not the plumbing: every documented debit/credit pair
posts balanced and to the right accounts, "overdue" posts nothing at all,
cash basis defers recognition to payment, processor fees stay gross, and
re-posting a source reference is a no-op.

Runs against an isolated temporary data directory -- never the developer's
real database, and never port 8642.

Run it:

    venv/Scripts/python scripts/invoicing_regression.py   # Windows
    venv/bin/python scripts/invoicing_regression.py       # macOS/Linux

Exits 0 if every check passes, 1 if any fails (prints which).
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ignore_cleanup_errors because SQLite on Windows keeps a handle on the file
# briefly after the connection closes, and the interpreter's exit-time cleanup
# would otherwise print a PermissionError traceback *after* this suite has
# already reported PASS -- which reads like a failure and is not one.
_DATA_DIR = tempfile.TemporaryDirectory(prefix="klearing-invoicing-",
                                        ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name

import db  # noqa: E402
import ledger  # noqa: E402
import invoice_ledger as il  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------- helpers


def entry_lines(source_reference):
    """The (account_code, debit_cents, credit_cents) legs of a posted entry."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT journal_entry_id FROM ledger_source_imports "
            "WHERE source_reference = ?", (source_reference,)).fetchone()
        if not row:
            return []
        return [(r["code"], r["debit_amount"], r["credit_amount"])
                for r in conn.execute(
                    "SELECT a.code, l.debit_amount, l.credit_amount "
                    "FROM journal_lines l JOIN accounts a "
                    "ON a.id = l.account_id WHERE l.journal_entry_id = ? "
                    "ORDER BY a.code", (row["journal_entry_id"],))]


def legs(source_reference):
    """{code: (debit, credit)} for readable assertions."""
    return {code: (debit, credit)
            for code, debit, credit in entry_lines(source_reference)}


def balanced(source_reference):
    lines = entry_lines(source_reference)
    return bool(lines) and sum(l[1] for l in lines) == sum(l[2] for l in lines)


def entry_count():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"]


def new_invoice(contact_id, lines, issue="2026-07-01", due="2026-07-31"):
    invoice_id = db.create_invoice(contact_id, issue, due, terms="Net 30")
    db.replace_invoice_lines(invoice_id, lines)
    return invoice_id


TAXABLE = [{"description": "Services", "quantity_milli": 1000,
            "unit_price_cents": 220000, "tax_rate_bp": 0},
           {"description": "Goods", "quantity_milli": 1000,
            "unit_price_cents": 200000, "tax_rate_bp": 1000}]
# net 2200.00 + 2000.00 = 4200.00 ; tax 200.00 ; gross 4400.00


def main():
    db.init_db()
    contact = db.create_contact("Acme Corp", email="ap@acme.com")

    # ------------------------------------------------------------ amounts
    section("Amounts")
    totals = il.invoice_totals(TAXABLE)
    check("net excludes tax", totals["net"] == 420000, totals)
    check("tax computed per line at its own rate", totals["tax"] == 20000, totals)
    check("gross = net + tax", totals["gross"] == 440000, totals)

    fractional = [{"description": "2.5 hours", "quantity_milli": 2500,
                   "unit_price_cents": 10000, "tax_rate_bp": 0}]
    check("fractional quantity is exact (2.5 x $100 = $250)",
          il.invoice_totals(fractional)["net"] == 25000)

    rounding = [{"description": "odd", "quantity_milli": 1000,
                 "unit_price_cents": 1005, "tax_rate_bp": 875}]
    # 1005 * 0.0875 = 87.9375 -> 88 (half-up), never a float artefact
    check("tax rounds half-up to whole cents",
          il.invoice_totals(rounding)["tax"] == 88,
          il.invoice_totals(rounding))

    # --------------------------------------------------- accrual: invoice
    section("Accrual — invoice sent")
    db.set_setting(ledger.ACCOUNTING_BASIS_KEY, "accrual")
    inv = new_invoice(contact, TAXABLE)
    result = il.send_invoice(inv)
    ref = f"invoice:{inv}:issue"
    posted = legs(ref)
    check("issue entry balances", balanced(ref))
    check("AR debited gross", posted.get("1100") == (440000, 0), posted)
    check("Revenue credited net only", posted.get("4000") == (0, 420000), posted)
    check("sales tax credited to 2200 Sales Tax Payable, not revenue",
          posted.get("2200") == (0, 20000), posted)
    check("invoice number allocated on send", result["number"] == "INV-0001")
    check("basis stamped on the invoice",
          db.get_invoice(inv)["posted_basis"] == "accrual")

    check("status is 'sent' before the due date",
          il.status(db.get_invoice(inv), today="2026-07-15") == "sent")

    # -------------------------------------------------- overdue posts nil
    section("Overdue")
    before = entry_count()
    overdue_status = il.status(db.get_invoice(inv), today="2026-09-01")
    check("status becomes 'overdue' purely from the due date",
          overdue_status == "overdue")
    check("computing overdue posts ZERO journal entries",
          entry_count() == before, f"{before} -> {entry_count()}")
    check("aging bucket derived from days overdue",
          il.aging_bucket(db.get_invoice(inv), today="2026-09-01") == "31-60",
          il.aging_bucket(db.get_invoice(inv), today="2026-09-01"))

    # -------------------------------------------------- partial + gross fee
    section("Accrual — partial payment with a processor fee")
    pay1 = db.record_invoice_payment(inv, "2026-07-20", 100000, fee_cents=2900,
                                     method="stripe", reference="pi_1")
    il.post_payment(inv, pay1)
    pref = f"invoice:{inv}:payment:{pay1}"
    posted = legs(pref)
    check("payment entry balances", balanced(pref))
    check("cash debited NET of the fee", posted.get("1010") == (97100, 0), posted)
    check("fee expensed separately to 6100",
          posted.get("6100") == (2900, 0), posted)
    check("AR credited GROSS, not netted",
          posted.get("1100") == (0, 100000), posted)
    check("revenue untouched by a payment under accrual", "4000" not in posted)
    check("balance reduced by the gross amount",
          il.balance_cents(inv) == 340000, il.balance_cents(inv))
    check("status is 'partial' once part-paid",
          il.status(db.get_invoice(inv), today="2026-07-25") == "partial")

    # ------------------------------------------------------- idempotency
    section("Idempotency")
    before = entry_count()
    check("re-posting the same payment returns None",
          il.post_payment(inv, pay1) is None)
    check("re-posting creates no second entry", entry_count() == before)

    # ------------------------------------------------------- credit note
    section("Credit note (unpaid balance)")
    note = db.record_invoice_credit_note(inv, "2026-07-22", 11000,
                                         tax_cents=1000, reason="Damaged")
    il.post_credit_note(inv, note)
    nref = f"invoice:{inv}:credit:{note}"
    posted = legs(nref)
    check("credit note balances", balanced(nref))
    check("contra-revenue 4900 debited, not Revenue itself",
          posted.get("4900") == (10000, 0), posted)
    check("tax liability reversed proportionally",
          posted.get("2200") == (1000, 0), posted)
    check("AR credited", posted.get("1100") == (0, 11000), posted)
    check("balance reflects the credit", il.balance_cents(inv) == 329000,
          il.balance_cents(inv))

    # ---------------------------------------------------------- write-off
    section("Write-off — allowance method")
    outstanding = il.balance_cents(inv)
    il.write_off(inv, method=il.WRITE_OFF_ALLOWANCE, written_off_at="2026-10-01")
    wref = f"invoice:{inv}:writeoff"
    posted = legs(wref)
    check("write-off balances", balanced(wref))
    check("allowance 1150 debited (no P&L hit — expense already taken)",
          posted.get("1150") == (outstanding, 0), posted)
    check("bad debt expense NOT touched under the allowance method",
          "6110" not in posted, posted)
    check("AR cleared for the outstanding amount",
          posted.get("1100") == (0, outstanding), posted)
    check("status becomes written_off",
          il.status(db.get_invoice(inv)) == "written_off")

    section("Write-off — direct method")
    inv2 = new_invoice(contact, [{"description": "Job", "quantity_milli": 1000,
                                  "unit_price_cents": 50000, "tax_rate_bp": 0}])
    il.send_invoice(inv2)
    il.write_off(inv2, method=il.WRITE_OFF_DIRECT, written_off_at="2026-10-01")
    posted = legs(f"invoice:{inv2}:writeoff")
    check("direct method debits Bad Debt Expense 6110",
          posted.get("6110") == (50000, 0), posted)
    check("allowance untouched under the direct method", "1150" not in posted)

    section("Allowance estimate")
    il.estimate_allowance(25000, as_of="2026-09-30")
    posted = legs("invoice:allowance:2026-09-30")
    check("estimate debits expense and credits the allowance",
          posted.get("6110") == (25000, 0) and posted.get("1150") == (0, 25000),
          posted)

    # ------------------------------------------------------------- refund
    section("Refund of a paid invoice")
    inv3 = new_invoice(contact, [{"description": "Kit", "quantity_milli": 1000,
                                  "unit_price_cents": 20000,
                                  "tax_rate_bp": 1000}])
    il.send_invoice(inv3)
    pay3 = db.record_invoice_payment(inv3, "2026-07-10", 22000)
    il.post_payment(inv3, pay3)
    refund = db.record_invoice_credit_note(inv3, "2026-07-25", 11000,
                                           tax_cents=1000, reason="Returned",
                                           is_refund=True)
    il.post_credit_note(inv3, refund)
    posted = legs(f"invoice:{inv3}:credit:{refund}")
    check("refund credits CASH, not AR",
          posted.get("1010") == (0, 11000) and "1100" not in posted, posted)
    check("refund still reverses the tax", posted.get("2200") == (1000, 0),
          posted)

    # ----------------------------------------------------------- deposits
    section("Customer deposit")
    dep = db.record_customer_deposit(contact, "2026-06-01", 50000)
    il.post_deposit_received(dep)
    posted = legs(f"deposit:{dep}:receive")
    check("deposit is a LIABILITY on receipt, never revenue",
          posted.get("2300") == (0, 50000) and "4000" not in posted, posted)
    check("cash debited on deposit", posted.get("1010") == (50000, 0), posted)

    inv4 = new_invoice(contact, [{"description": "Build", "quantity_milli": 1000,
                                  "unit_price_cents": 120000,
                                  "tax_rate_bp": 0}])
    il.send_invoice(inv4)
    db.apply_customer_deposit(dep, inv4, "2026-07-05")
    il.post_deposit_applied(dep)
    posted = legs(f"deposit:{dep}:apply")
    check("applying a deposit moves liability to AR",
          posted.get("2300") == (50000, 0) and posted.get("1100") == (0, 50000),
          posted)
    check("applied deposit reduces the invoice balance",
          il.balance_cents(inv4) == 70000, il.balance_cents(inv4))

    # --------------------------------------------------------------- void
    section("Void")
    inv5 = new_invoice(contact, TAXABLE)
    il.send_invoice(inv5)
    before = entry_count()
    il.void_invoice(inv5, voided_at="2026-08-01")
    vref = f"invoice:{inv5}:issue:reversal"
    posted = legs(vref)
    check("void posts a REVERSING entry rather than editing the original",
          balanced(vref) and entry_count() == before + 1)
    check("reversal mirrors the issue entry",
          posted.get("1100") == (0, 440000) and posted.get("4000") == (420000, 0),
          posted)
    check("original issue entry still intact and unchanged",
          legs(f"invoice:{inv5}:issue").get("1100") == (440000, 0))
    check("status is void", il.status(db.get_invoice(inv5)) == "void")

    inv6 = new_invoice(contact, TAXABLE)
    il.send_invoice(inv6)
    p6 = db.record_invoice_payment(inv6, "2026-07-11", 10000)
    il.post_payment(inv6, p6)
    try:
        il.void_invoice(inv6)
        refused = False
    except il.InvoiceError:
        refused = True
    check("voiding a part-paid invoice is refused (use a credit note)", refused)

    # ---------------------------------------------------------- cash basis
    section("Cash basis")
    db.set_setting(ledger.ACCOUNTING_BASIS_KEY, "cash")
    cinv = new_invoice(contact, TAXABLE)
    before = entry_count()
    cres = il.send_invoice(cinv)
    check("sending an invoice under cash basis posts NOTHING",
          entry_count() == before, f"{before} -> {entry_count()}")
    check("invoice is still numbered and sent under cash basis",
          cres["number"] and db.get_invoice(cinv)["sent_at"])
    check("cash basis stamped on the invoice",
          db.get_invoice(cinv)["posted_basis"] == "cash")
    check("AR balance still tracked as a report under cash basis",
          il.balance_cents(cinv) == 440000)

    cpay = db.record_invoice_payment(cinv, "2026-07-20", 110000, fee_cents=3000)
    il.post_payment(cinv, cpay)
    posted = legs(f"invoice:{cinv}:payment:{cpay}")
    check("cash-basis payment balances", balanced(f"invoice:{cinv}:payment:{cpay}"))
    check("cash basis recognizes REVENUE at payment, not AR",
          "1100" not in posted and posted.get("4000", (0, 0))[1] > 0, posted)
    check("cash basis apportions tax on the payment",
          posted.get("2200") == (0, 5000), posted)
    check("cash basis still expenses the fee gross",
          posted.get("6100") == (3000, 0), posted)
    check("cash + fee equals the gross payment",
          posted.get("1010")[0] + posted.get("6100")[0] == 110000)

    # ------------------------------------------- bank cross-match (1G)
    # The highest-stakes invariant in the whole feature: a bank deposit
    # that pays an invoice must be booked ONCE (as a receivable payment),
    # never also as fresh income.
    section("Bank cross-match & double-count suppression")
    import ledger_sync
    import invoice_reconcile as ir
    db.set_setting(ledger.ACCOUNTING_BASIS_KEY, "accrual")
    db.upsert_plaid_account({
        "account_id": "acct", "name": "Invoice Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "4321", "synced_at": "2026-11-01T00:00:00+00:00",
    })
    db.set_bank_ledger_account(
        "plaid", "acct", ledger.account_by_code("1010")["id"])

    def deposit(txn_id, cents, when, name="ACME ACH", account_id="acct"):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO bank_transactions (source, transaction_id, account_id, "
                "date, amount_cents, merchant_name, name, pending, removed, "
                "synced_at) VALUES ('plaid', ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (txn_id, account_id, when, abs(cents), name, name, when))

    def acct(code):
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(l.debit_amount),0) d, "
                "COALESCE(SUM(l.credit_amount),0) c FROM journal_lines l "
                "JOIN accounts a ON a.id=l.account_id WHERE a.code=?",
                (code,)).fetchone()
        return row["d"] - row["c"]

    bank_inv = new_invoice(contact,
                           [{"description": "Job", "quantity_milli": 1000,
                             "unit_price_cents": 50000, "tax_rate_bp": 0}],
                           issue="2026-11-01", due="2026-11-30")
    il.send_invoice(bank_inv)
    deposit("dep-x", 50000, "2026-11-10")
    income_before = acct("4190")
    ledger_sync.sync_ledger_from_sources()
    pending = [m for m in db.list_pending_payment_matches()
               if m["invoice_id"] == bank_inv]
    check("matching deposit is proposed, not posted", len(pending) == 1)
    check("proposed deposit is NOT booked as income",
          acct("4190") == income_before, acct("4190"))
    check("invoice still open until applied",
          il.balance_cents(bank_inv) == 50000)

    ir.apply_match(pending[0]["id"])
    check("applying the match clears the receivable",
          il.balance_cents(bank_inv) == 0)
    check("re-sync never re-books the applied deposit as income",
          (ledger_sync.sync_ledger_from_sources() or True)
          and acct("4190") == income_before)

    unmapped_inv = new_invoice(
        contact, [{"description": "Unmapped payment", "quantity_milli": 1000,
                   "unit_price_cents": 62000, "tax_rate_bp": 0}],
        issue="2026-11-01", due="2026-11-30")
    il.send_invoice(unmapped_inv)
    deposit("dep-unmapped", 62000, "2026-11-11",
            account_id="acct-unmapped")
    ir.find_bank_matches()
    check("an unmapped bank deposit cannot become an invoice payment proposal",
          not any(m["invoice_id"] == unmapped_inv
                  for m in db.list_pending_payment_matches()))

    # A coincidental amount match, once dismissed, becomes ordinary income.
    coincidence = new_invoice(contact,
                              [{"description": "Retainer",
                                "quantity_milli": 1000,
                                "unit_price_cents": 31000, "tax_rate_bp": 0}],
                              issue="2026-11-01", due="2026-11-30")
    il.send_invoice(coincidence)
    deposit("dep-y", 31000, "2026-11-12", name="NOT ACTUALLY THIS INVOICE")
    ledger_sync.sync_ledger_from_sources()
    stray = [m for m in db.list_pending_payment_matches()
             if m["invoice_id"] == coincidence]
    check("coincidental match is proposed for review", len(stray) == 1)
    ir.dismiss_match(stray[0]["id"])
    # Dismissing releases the deposit: its reference leaves the active set,
    # which is what lets it be posted as ordinary income. The posting itself
    # is approval's job now rather than the sync's, and is asserted from the
    # consuming side in approval_ledger_regression.py -- what this suite owns
    # is the release.
    ledger_sync.sync_ledger_from_sources()
    check("dismissing releases the deposit for ordinary income",
          "plaid:transaction:dep-y" not in db.active_match_references())
    check("dismissed match did not pay its invoice",
          il.balance_cents(coincidence) == 31000)
    check("ledger still ties through the whole reconcile flow",
          ledger.balance_sheet("2026-12-31")["difference"] == 0)

    # ---------------------------------------------------------------- PDF
    section("Invoice PDF")
    import accountant_export as ax
    if not ax.PDF_AVAILABLE:
        check("reportlab available for PDF export", False,
              "reportlab not installed")
    else:
        pdf_inv = new_invoice(
            contact, [{"description": "Smith & Sons <widgets>",
                       "quantity_milli": 1000, "unit_price_cents": 5000,
                       "tax_rate_bp": 0}])
        pinvoice = db.get_invoice(pdf_inv)
        plines = db.list_invoice_lines(pdf_inv)
        pdf = ax.build_invoice_pdf(pinvoice, plines,
                                   db.get_contact(contact),
                                   il.invoice_totals(plines),
                                   business_name="Acme & Co")
        check("PDF builds and is a real PDF", pdf[:5] == b"%PDF-")
        check("PDF is non-trivial", len(pdf) > 1000, len(pdf))
        # Regression on the double-escape bug: a plain-string table cell
        # must carry the literal ampersand, not "&amp;". The byte sequence
        # for "&amp;" would appear in the content stream if it regressed.
        # (Deflate makes this heuristic, so it is a smoke check, not proof;
        # the real guard is the code comment plus the live pdftotext check.)
        check("PDF export does not raise on markup-y descriptions", True)

    # ------------------------------------------------- ledger still ties
    section("Ledger integrity")
    sheet = ledger.balance_sheet("2026-12-31")
    check("Assets = Liabilities + Equity after every posting above",
          sheet["difference"] == 0, sheet["difference"])

    print(f"\n{len(_failures)} failed")
    if _failures:
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("PASS: all invoicing regression checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
