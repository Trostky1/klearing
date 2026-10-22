"""Detect that an invoice was probably paid, and never post it unasked.

Two independent signals, one rule. The Stripe poll (needs the network)
sees a completed payment-link checkout. The bank cross-match (purely
local) sees a Plaid deposit whose exact amount and date fit an open
invoice. Each writes a *pending* proposal to invoice_payment_matches; the
Review drawer turns that into "Payment detected: apply?". Applying is the
click that posts. Nothing here debits or credits the ledger on its own.

The bank matcher is deliberately conservative in the same way the Shopify
payout matcher is: an exact single amount+date match is proposed, and
anything ambiguous (no match, or several candidate deposits) is left
alone rather than guessed. A deposit claimed by an active proposal is
suppressed from the generic Plaid income sync (see is_claimed_deposit),
so the same money is never booked as both a receivable payment and fresh
income -- the exact mirror of how a Shopify payout deposit is suppressed.
"""

from datetime import date, timedelta

import db
import invoice_ledger
import ledger_sync
import stripe_client

# How long after a deposit before it stops being a plausible invoice
# payment. Generous, because customers pay slowly and the amount match is
# the real discriminator; the window only bounds coincidental collisions.
BANK_MATCH_WINDOW_DAYS = 180

CASH_ACCOUNT = ledger_sync.CHECKING


def _open_invoices():
    for invoice in db.list_invoices(include_voided=False):
        if not invoice["sent_at"] or invoice["written_off_at"]:
            continue
        balance = invoice_ledger.balance_cents(invoice["id"])
        if balance > 0:
            yield invoice, balance


def is_claimed_deposit(transaction_id):
    """True if this Plaid deposit is spoken for by an active invoice-payment
    proposal, so the generic income sync must not also post it."""
    return f"plaid:transaction:{transaction_id}" in db.active_match_references()


def find_bank_matches():
    """Propose invoice payments from unclaimed bank deposits. Local only.

    For each open invoice, a deposit qualifies when it is an inflow, its
    amount exactly equals the outstanding balance, it is dated on or after
    the invoice's issue date within the window, it is not a Shopify payout,
    and it is not already claimed. Exactly one qualifying deposit is
    proposed; zero or several are left alone (skip-and-say-nothing rather
    than guess). Returns the number of new proposals.
    """
    with db.get_connection() as conn:
        checking_id = conn.execute(
            "SELECT id FROM accounts WHERE code = ?", (CASH_ACCOUNT,)
        ).fetchone()["id"]
    deposits = []
    for transaction in db.list_bank_transactions_for_ledger():
        if int(transaction["amount_cents"]) <= 0:
            continue
        source = transaction.get("source") or "plaid"
        mapped = db.bank_ledger_account_id(source, transaction.get("account_id"))
        # invoice_ledger currently posts detected bank payments to Checking.
        # A deposit from any source must therefore explicitly map to Checking;
        # an absent mapping cannot safely become a payment proposal.
        if mapped != checking_id:
            continue
        deposits.append(transaction)
    payout_ids = [p["payout_id"] for p in db.list_shopify_payouts()]
    proposed = 0
    for invoice, balance in _open_invoices():
        claimed = db.active_match_references()
        try:
            floor = date.fromisoformat(invoice["issue_date"])
        except (TypeError, ValueError):
            continue
        ceiling = floor + timedelta(days=BANK_MATCH_WINDOW_DAYS)

        candidates = []
        for deposit in deposits:
            reference = ledger_sync.bank_transaction_reference(deposit)
            if reference in claimed:
                continue
            if abs(int(deposit["amount_cents"])) != balance:
                continue
            try:
                when = date.fromisoformat(deposit["date"])
            except (TypeError, ValueError):
                continue
            if not (floor <= when <= ceiling):
                continue
            if ledger_sync._is_shopify_payout(deposit, payout_ids):
                continue
            candidates.append(deposit)

        # Exactly one, or the signal is not clean enough to act on.
        if len(candidates) != 1:
            continue
        deposit = candidates[0]
        db.record_payment_match(
            invoice["id"], "bank",
            ledger_sync.bank_transaction_reference(deposit),
            abs(int(deposit["amount_cents"])), deposit["date"],
            detail=(deposit.get("merchant_name") or deposit.get("name")
                    or "Bank deposit"))
        proposed += 1
    return proposed


def poll_stripe():
    """Propose invoice payments from completed Stripe payment links. Network.

    For each open invoice carrying a payment link, every newly-paid
    checkout session becomes a proposal, with the real processor fee read
    from Stripe (not estimated). Returns the number of new proposals. Safe
    to call when Stripe is not connected -- it simply does nothing.
    """
    if not stripe_client.is_configured():
        return 0
    proposed = 0
    for invoice in db.list_invoices(include_voided=False):
        link_id = invoice.get("stripe_payment_link_id")
        if not link_id or not invoice["sent_at"] or invoice["written_off_at"]:
            continue
        if invoice_ledger.balance_cents(invoice["id"]) <= 0:
            continue
        try:
            sessions = stripe_client.payments_for_link(link_id)
        except Exception:  # noqa: BLE001 - a poll failure is not fatal
            continue
        for session in sessions:
            if not session["paid"]:
                continue
            reference = f"stripe:session:{session['session_id']}"
            if db.payment_match_reference_exists(reference):
                continue
            try:
                fee = stripe_client.payment_intent_fee(session["payment_intent"])
            except Exception:  # noqa: BLE001 - retry the fee on a later poll
                fee = 0
            db.record_payment_match(
                invoice["id"], "stripe", reference,
                session["amount_total_cents"], date.today().isoformat(),
                fee_cents=fee, detail="Paid online via Stripe")
            proposed += 1
    return proposed


def apply_match(match_id):
    """Turn a proposal into a real, posted payment. The click behind
    "nothing posts without a click"."""
    match = db.get_payment_match(match_id)
    if not match or match["status"] != "pending":
        raise invoice_ledger.InvoiceError("That payment is no longer pending.")
    payment_id = db.record_invoice_payment(
        match["invoice_id"], match["paid_on"], match["amount_cents"],
        fee_cents=match["fee_cents"], method=match["source"],
        reference=match["reference"])
    invoice_ledger.post_payment(match["invoice_id"], payment_id)
    db.set_payment_match_status(match_id, "applied")
    return payment_id


def dismiss_match(match_id):
    """Reject a proposal. A dismissed bank deposit is no longer suppressed,
    so the next ledger sync books it as ordinary income -- the correct
    outcome when the amount match was a coincidence."""
    match = db.get_payment_match(match_id)
    if not match or match["status"] != "pending":
        raise invoice_ledger.InvoiceError("That payment is no longer pending.")
    db.set_payment_match_status(match_id, "dismissed")
