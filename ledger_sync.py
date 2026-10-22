"""Manual, idempotent import of locally synced Plaid and Shopify source data.

This Phase 1 bridge deliberately does not call external APIs, schedule work,
or update existing source entries. It posts only new local source records.
Refund, order-change, and transfer adjustment workflows remain future
accounting phases. COGS uses only cost snapshots captured before an order.
"""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import db
import inventory
import ledger

CREATED_BY = "ledger-source-sync"
CHECKING = "1010"
SHOPIFY_CLEARING = "1015"
ACCOUNTS_RECEIVABLE = "1100"
SALES_REVENUE = "4000"
INVENTORY = "1200"
TRANSFER_CLEARING = "1090"
COGS = "5000"
PAYMENT_PROCESSING_FEES = "6100"
BANK_FEES = "6020"
ADVERTISING = "6010"
SOFTWARE = "6030"
PAYROLL = "6060"
UNCATEGORIZED_EXPENSE = "6080"
REFUNDS_RECEIVED = "6090"
UNCATEGORIZED_INCOME = "4190"
INTEREST_INCOME = "4200"
BANK_REWARDS_CASHBACK_INCOME = "4210"
# Invoicing/AR (see migrations/add_invoicing.sql). Declared here because
# _accounts_by_code's required set is the single guard that turns a missing
# account into a loud startup error instead of a KeyError mid-post.
SALES_TAX_PAYABLE = "2200"
CUSTOMER_DEPOSITS = "2300"
ALLOWANCE_DOUBTFUL = "1150"
SALES_RETURNS = "4900"
BAD_DEBT_EXPENSE = "6110"

PAID_SHOPIFY_STATUSES = {"paid", "partially_paid", "partially_refunded"}
SKIPPED_SHOPIFY_STATUSES = {"refunded", "voided"}


class SkipReason:
    """Every reason a source record can decline to post.

    These are persisted values, not display strings: they land in
    ledger_source_import_skips.reason and are read back by the Ledger Sync
    screen and by scripts/ledger_verify.py. Renaming one silently orphans
    the rows already carrying the old value, so treat them as a stored
    vocabulary rather than as labels. Both readers prettify generically
    (underscores to spaces), so adding one needs no display-side change.

    Gathered here because they were previously inline literals in a dozen
    places, which made the taxonomy impossible to see and made it easy to
    misread a reason as a source or a status.
    """

    # Plaid
    AMBIGUOUS_TRANSFER_MATCH = "ambiguous_transfer_match"
    PLAID_ALREADY_IMPORTED = "plaid_already_imported"

    # Shopify payouts
    INVALID_PAYOUT_DATE = "invalid_payout_date"
    UNMATCHED_PAYOUT = "unmatched_payout"
    AMBIGUOUS_PAYOUT_MATCH = "ambiguous_payout_match"
    PAYOUT_NOT_PAID = "payout_not_paid"
    COMPLEX_PAYOUT_COMPONENTS = "complex_payout_components"
    UNMAPPED_BANK_ACCOUNT = "unmapped_bank_account"

    # Shopify COGS
    REVENUE_NOT_IMPORTED = "revenue_not_imported"
    MISSING_COST_DATA = "missing_cost_data"
    ZERO_COGS = "zero_cogs"
    INVENTORY_ACQUISITION_NOT_TRACKED = "inventory_acquisition_not_tracked"


def _accounts_by_code():
    return {account["code"]: account["id"] for account in ledger.list_accounts()}


def _amount_from_cents(cents):
    return Decimal(int(cents)) / 100


def _shopify_net_amount(order):
    try:
        return Decimal(str(order["total_price"])) - Decimal(
            str(order.get("refund_total") or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _plaid_expense_account(transaction):
    category = " ".join((transaction.get("category_primary") or "",
                         transaction.get("category_detailed") or "")).upper()
    if "BANK_FEES" in category or "BANK FEE" in category:
        return BANK_FEES
    if "ADVERTISING" in category or "MARKETING" in category:
        return ADVERTISING
    if "SOFTWARE" in category or "SUBSCRIPTION" in category:
        return SOFTWARE
    if "PAYROLL" in category:
        return PAYROLL
    return UNCATEGORIZED_EXPENSE


def _is_transfer(transaction):
    category = " ".join((transaction.get("category_primary") or "",
                         transaction.get("category_detailed") or "")).upper()
    return "TRANSFER" in category


def _plaid_deposit_account(transaction):
    """Choose a non-sales account for a Plaid cash inflow.

    Only Plaid's category metadata is trusted here.  A merchant or transaction
    name may contain a word such as "cashback" without describing bank income,
    so it must never change the account.  Plaid cannot reliably distinguish
    owner contributions and loan proceeds; all other deposits stay visible in
    Uncategorized Income for review.
    """
    category = " ".join((transaction.get("category_primary") or "",
                         transaction.get("category_detailed") or "")).upper()
    if "REFUND" in category:
        return REFUNDS_RECEIVED
    if "INTEREST" in category:
        return INTEREST_INCOME
    if any(marker in category for marker in ("CASHBACK", "CASH BACK", "REWARD")):
        return BANK_REWARDS_CASHBACK_INCOME
    return UNCATEGORIZED_INCOME


def _is_shopify_payout(transaction, known_payout_ids=()):
    counterparty = " ".join((transaction.get("merchant_name") or "",
                              transaction.get("name") or "")).lower()
    return ("shopify" in counterparty
            or any(str(payout_id).lower() in counterparty
                   for payout_id in known_payout_ids))


def bank_transaction_reference(transaction):
    """Collision-safe reference, preserving shipped Plaid references."""
    source = transaction.get("source") or "plaid"
    transaction_id = transaction["transaction_id"]
    if source == "plaid":
        return f"plaid:transaction:{transaction_id}"
    return f"bank:{source}:{transaction.get('account_id') or ''}:{transaction_id}"


def _create_entry(date_value, memo, debit_code, credit_code, amount,
                  source_reference, accounts):
    return ledger.create_sourced_journal_entry(
        date_value,
        memo,
        [
            {"account_id": accounts[debit_code], "debit_amount": amount},
            {"account_id": accounts[credit_code], "credit_amount": amount},
        ],
        source_reference=source_reference,
        created_by=CREATED_BY,
    )


def _create_entry_ids(date_value, memo, debit_account_id, credit_account_id,
                      amount, source_reference):
    return ledger.create_sourced_journal_entry(
        date_value,
        memo,
        [
            {"account_id": debit_account_id, "debit_amount": amount},
            {"account_id": credit_account_id, "credit_amount": amount},
        ],
        source_reference=source_reference,
        created_by=CREATED_BY,
    )


def _plaid_account_id(transaction):
    """Return the source account's Ledger ID, or None when it is unmapped."""
    return db.ensure_plaid_ledger_account(transaction.get("account_id"))


def _log_unmapped_plaid_transaction(transaction, source_reference, summary,
                                    context="transaction"):
    account_id = transaction.get("account_id") or "?"
    db.log_ledger_source_import_skip(
        source_reference, "plaid", SkipReason.UNMAPPED_BANK_ACCOUNT,
        f"Plaid bank account {account_id} has no Ledger mapping; {context} "
        "was not posted.",
    )
    summary["plaid"]["skipped"] += 1


def _transfer_candidates(transaction, transactions):
    """Find safely pairable, opposite-side transfers in connected accounts."""
    try:
        transaction_date = date.fromisoformat(transaction["date"])
    except (TypeError, ValueError):
        return []
    amount_cents = int(transaction["amount_cents"])
    return [candidate for candidate in transactions
            if candidate["transaction_id"] != transaction["transaction_id"]
            and _is_transfer(candidate)
            and str(candidate.get("account_id") or "")
            != str(transaction.get("account_id") or "")
            and int(candidate["amount_cents"]) == -amount_cents
            and abs((date.fromisoformat(candidate["date"]) - transaction_date).days) <= 5]


def _sync_transfer(transaction, transactions, accounts, summary):
    """Post a paired transfer directly, or one visible side through clearing."""
    transaction_id = transaction["transaction_id"]
    source_reference = f"plaid:transaction:{transaction_id}:transfer"
    candidates = _transfer_candidates(transaction, transactions)
    if len(candidates) > 1:
        detail = "More than one opposite transfer has the same amount and date window."
        db.log_ledger_source_import_skip(source_reference, "plaid",
                                         SkipReason.AMBIGUOUS_TRANSFER_MATCH, detail)
        summary["plaid"]["skipped"] += 1
        return

    account_id = _plaid_account_id(transaction)
    if account_id is None:
        _log_unmapped_plaid_transaction(
            transaction, source_reference, summary, "transfer")
        return
    if len(candidates) == 1:
        other = candidates[0]
        other_account_id = _plaid_account_id(other)
        if other_account_id is None:
            _log_unmapped_plaid_transaction(
                other, source_reference, summary, "transfer counterpart")
            return
        single_refs = (source_reference,
                       f"plaid:transaction:{other['transaction_id']}:transfer")
        # Once one side was posted through Transfer Clearing, post only the
        # remaining side there too. This clears the suspense balance without
        # rewriting the immutable first entry.
        if any(db.ledger_source_import_exists(reference) for reference in single_refs):
            candidates = []
        elif account_id != other_account_id:
            outgoing = transaction if int(transaction["amount_cents"]) < 0 else other
            incoming = other if outgoing is transaction else transaction
            pair_reference = "plaid:transfer:" + ":".join(sorted((
                str(transaction_id), str(other["transaction_id"]))))
            entry = _create_entry_ids(
                outgoing["date"], "Plaid transfer between connected accounts",
                _plaid_account_id(incoming),
                _plaid_account_id(outgoing),
                _amount_from_cents(abs(int(outgoing["amount_cents"]))),
                pair_reference,
            )
            for reference in single_refs:
                db.clear_ledger_source_import_skip(reference)
            if entry is None:
                summary["plaid"]["already_imported"] += 1
            else:
                summary["plaid"]["created"] += 1
            return

    amount = _amount_from_cents(abs(int(transaction["amount_cents"])))
    if int(transaction["amount_cents"]) < 0:
        debit_account_id, credit_account_id = accounts[TRANSFER_CLEARING], account_id
    else:
        debit_account_id, credit_account_id = account_id, accounts[TRANSFER_CLEARING]
    entry = _create_entry_ids(
        transaction["date"], "Plaid transfer pending counterpart",
        debit_account_id, credit_account_id, amount, source_reference,
    )
    db.clear_ledger_source_import_skip(source_reference)
    if entry is None:
        summary["plaid"]["already_imported"] += 1
    else:
        summary["plaid"]["created"] += 1


def _sync_plaid(accounts, summary):
    """RETIRED: no longer called by sync_ledger_from_sources.

    This posted every settled bank transaction with a category derived from
    Plaid's own metadata, with no idea whether a person had approved it. That
    meant a denied transaction still landed in the books and a chosen category
    never did. Approval is the only route now (app._post_one_to_ledger).

    Kept, unreferenced, as the readable record of what the retired behaviour
    was -- the accounts it routed to are the same ones approval offers, and
    the two guards it carried (invoice-claimed deposits, Shopify payouts) moved
    to approval rather than disappearing. Deleting it is Phase 5 cleanup.
    """
    known_payout_ids = [payout["payout_id"] for payout in db.list_shopify_payouts()]
    # Deposits claimed by an active invoice-payment proposal must not also
    # post here as generic income -- the same suppression a Shopify payout
    # gets. Computed once; the reconciler has already run this sync (see
    # sync_ledger_from_sources), so the set is current.
    claimed_refs = db.active_match_references()
    transactions = db.list_plaid_transactions_for_ledger()
    for transaction in transactions:
        source_reference = f"plaid:transaction:{transaction['transaction_id']}"
        amount_cents = int(transaction["amount_cents"])
        if amount_cents == 0:
            summary["plaid"]["skipped"] += 1
            continue
        amount = _amount_from_cents(abs(amount_cents))
        memo = (transaction.get("merchant_name") or transaction.get("name")
                or "Plaid transaction")
        if _is_transfer(transaction):
            _sync_transfer(transaction, transactions, accounts, summary)
            continue
        bank_account_id = _plaid_account_id(transaction)
        if bank_account_id is None:
            _log_unmapped_plaid_transaction(
                transaction, source_reference, summary)
            continue
        if amount_cents < 0:
            entry = _create_entry_ids(
                transaction["date"], f"Plaid expense: {memo}",
                accounts[_plaid_expense_account(transaction)], bank_account_id,
                amount, source_reference,
            )
        else:
            if _is_shopify_payout(transaction, known_payout_ids):
                summary["plaid"]["skipped"] += 1
                continue
            if source_reference in claimed_refs:
                # Claimed by an invoice-payment proposal (pending or
                # applied). An applied one already posted Dr Checking /
                # Cr AR under its own reference; a pending one is awaiting
                # the human's click. Either way, not income.
                summary["plaid"]["skipped"] += 1
                continue
            credit_account = _plaid_deposit_account(transaction)
            entry = _create_entry_ids(
                transaction["date"], f"Plaid deposit: {memo}",
                bank_account_id, accounts[credit_account], amount, source_reference,
            )
        if entry is None:
            summary["plaid"]["already_imported"] += 1
        else:
            summary["plaid"]["created"] += 1


def _reclassify_misclassified_plaid_deposits(accounts, summary):
    """RETIRED: no longer called by sync_ledger_from_sources.

    It rewrote any deposit posted to Sales Revenue under a
    ``plaid:transaction:`` reference, moving it to Plaid's guessed account.
    That was a correction while _sync_plaid owned those references. Approval
    owns them now, so the same pass would take a Sales Revenue account a person
    deliberately picked and silently replace it with Uncategorized Income --
    reintroducing the exact bug this phase removes.

    Retired rather than deleted for the same reason as _sync_plaid, and because
    an install carrying genuinely misposted history may still want it run once,
    deliberately, rather than on every sync.
    """
    payout_ids = [payout["payout_id"] for payout in db.list_shopify_payouts()]
    for transaction in db.list_plaid_transactions_for_ledger():
        if int(transaction["amount_cents"]) <= 0 or _is_transfer(transaction):
            continue
        if _is_shopify_payout(transaction, payout_ids):
            continue
        source_reference = f"plaid:transaction:{transaction['transaction_id']}"
        if SALES_REVENUE not in db.source_import_account_codes(source_reference):
            continue
        target = _plaid_deposit_account(transaction)
        reclass_reference = f"ledger:reclass:{source_reference}"
        entry = _create_entry(
            transaction["date"], "Reclassify prior Plaid deposit from Sales Revenue",
            SALES_REVENUE, target,
            _amount_from_cents(abs(int(transaction["amount_cents"]))),
            reclass_reference, accounts,
        )
        if entry is not None:
            summary["plaid"]["reclassified"] += 1


def _sync_shopify(accounts, summary):
    for order in db.list_shopify_orders():
        status = (order.get("financial_status") or "").strip().lower()
        amount = _shopify_net_amount(order)
        if status in SKIPPED_SHOPIFY_STATUSES or amount <= 0:
            summary["shopify"]["skipped"] += 1
            continue
        debit_account = (SHOPIFY_CLEARING if status in PAID_SHOPIFY_STATUSES
                         else ACCOUNTS_RECEIVABLE)
        source_reference = f"shopify:order:{order['order_id']}"
        entry = _create_entry(
            order["created_at"][:10], f"Shopify order #{order['order_id']}",
            debit_account, SALES_REVENUE, amount, source_reference, accounts,
        )
        if entry is None:
            summary["shopify"]["already_imported"] += 1
        else:
            summary["shopify"]["created"] += 1


def _payout_detail(payout):
    return f"Shopify payout {payout['payout_id']}"


def _payout_has_complex_components(payout):
    try:
        payload = json.loads(payout.get("summary_json") or "{}")
        raw = payload.get("raw") or {}
        summary = raw.get("summary") or raw
    except (TypeError, ValueError):
        return True
    for field in ("refunds_gross_amount", "adjustments_gross_amount",
                  "reserved_funds_gross_amount", "retried_payouts_gross_amount"):
        try:
            if Decimal(str(summary.get(field) or 0)) != 0:
                return True
        except (InvalidOperation, TypeError, ValueError):
            return True
    return False


def _log_unmatched_payout(payout, summary, reason, detail):
    source_reference = f"shopify:payout:{payout['payout_id']}"
    db.log_ledger_source_import_skip(source_reference, "shopify", reason, detail)
    summary["shopify_payouts"]["unmatched"] += 1
    summary["shopify_payouts"]["unmatched_payouts"].append({
        "payout_id": payout["payout_id"], "reason": reason, "detail": detail,
    })


def match_payout_to_bank(payout, transactions):
    """Return the one exact bank deposit for a Shopify payout, or a reason.

    This pure matcher is shared by ledger posting and Insights. Keeping both
    callers on this function prevents a dashboard match from disagreeing with
    what the ledger is willing to recognize as the same bank deposit.
    """
    try:
        payout_date = date.fromisoformat(payout["date"])
    except (TypeError, ValueError):
        return None, SkipReason.INVALID_PAYOUT_DATE
    candidates = []
    for transaction in transactions:
        # Canonical bank signs are load-bearing here: only a positive inflow
        # can match a payout. Reversing this guard silently disables matching.
        if int(transaction["amount_cents"]) <= 0:
            continue
        try:
            transaction_date = date.fromisoformat(transaction["date"])
        except (TypeError, ValueError):
            continue
        if not (payout_date <= transaction_date <= payout_date + timedelta(days=5)):
            continue
        if abs(int(transaction["amount_cents"])) != int(payout["amount_cents"]):
            continue
        candidates.append(transaction)
    if not candidates:
        return None, SkipReason.UNMATCHED_PAYOUT
    payout_id = str(payout["payout_id"]).lower()
    with_reference = [candidate for candidate in candidates
                      if payout_id in " ".join((candidate.get("merchant_name") or "",
                                                 candidate.get("name") or "")).lower()]
    if len(with_reference) == 1:
        return with_reference[0], None
    with_shopify = [candidate for candidate in candidates
                    if "shopify" in " ".join((candidate.get("merchant_name") or "",
                                               candidate.get("name") or "")).lower()]
    if len(candidates) == 1 and len(with_shopify) == 1:
        return with_shopify[0], None
    return None, SkipReason.AMBIGUOUS_PAYOUT_MATCH


# Compatibility for downstream callers while active code uses the neutral name.
match_payout_to_plaid = match_payout_to_bank


def _sync_bank_transfers(accounts, summary):
    """Post paired moves between the owner's own connected accounts.

    The one part of the retired _sync_plaid that stays automatic. Every
    other bank transaction now reaches the books only by being approved,
    because a person's category is the whole point; a transfer has no
    category to choose, and pairing the two halves is something only this
    pass can do -- Review sees them as two unrelated rows.
    """
    transactions = db.list_bank_transactions_for_ledger(source="plaid")
    for transaction in transactions:
        if int(transaction["amount_cents"]) == 0:
            continue
        if _is_transfer(transaction):
            _sync_transfer(transaction, transactions, accounts, summary)


def claimed_by_transfer_sync(item):
    """True when the paired-transfer sync owns this bank transaction.

    A move between two of the owner's own connected accounts is a
    mechanical fact, not a categorisation decision -- there is nothing for
    a person to choose, which is why it posts automatically as one entry
    between the two ledger accounts instead of arriving in Review as two
    unrelated halves. Approval stands down so the pair is not also booked
    one side at a time.

    Reuses _is_transfer, the same test the sync itself applies, so the two
    cannot disagree about what a transfer is. Note this is a different
    thing from check_bank's owner-transfer suggestion, which is a deposit
    from a personal account and does belong in Review.
    """
    ext_id = item.get("external_transaction_id")
    if not ext_id:
        return False
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT category_primary, category_detailed FROM bank_transactions "
            "WHERE source = 'plaid' AND transaction_id = ? "
            "AND pending = 0 AND removed = 0",
            (ext_id,),
        ).fetchone()
    return row is not None and _is_transfer(dict(row))


def deposit_claimed_by_payout(item):
    """The Shopify payout an approved deposit belongs to, or None.

    Approval must not post a deposit that _sync_shopify_payouts is going to
    post, because the payout entry is the richer one: it splits the gross into
    cash plus processing fees and clears the Shopify Clearing account that the
    order sync debited. A deposit approved as plain income leaves Clearing
    accumulating with nothing to clear it.

    Deliberately reuses match_payout_to_bank rather than testing for the word
    "shopify" in the vendor. Only a deposit that uniquely matches a real,
    not-yet-imported payout is claimed, so an ordinary deposit from a business
    that happens to be named Shopify still posts normally. Sharing that matcher
    is also what stops approval and the payout sync from disagreeing about what
    counts as the same deposit.
    """
    if item.get("direction") == "expense":
        return None  # payouts are money in
    # Bank-cache rows use the canonical convention: money in is positive.
    candidate = {
        "amount_cents": int(round(float(item["amount"]) * 100)),
        "date": item["date"],
        "merchant_name": item.get("vendor") or "",
        "name": "",
    }
    for payout in db.list_shopify_payouts():
        if db.ledger_source_import_exists(f"shopify:payout:{payout['payout_id']}"):
            continue  # already posted; it cannot claim anything further
        matched, _reason = match_payout_to_bank(payout, [candidate])
        if matched is not None:
            return payout
    return None


def _sync_shopify_payouts(accounts, summary):
    transactions = db.list_bank_transactions_for_ledger()
    for payout in db.list_shopify_payouts():
        source_reference = f"shopify:payout:{payout['payout_id']}"
        if db.ledger_source_import_exists(source_reference):
            db.clear_ledger_source_import_skip(source_reference)
            summary["shopify_payouts"]["already_imported"] += 1
            continue
        if payout["status"] != "paid":
            _log_unmatched_payout(
                payout, summary, SkipReason.PAYOUT_NOT_PAID,
                f"{_payout_detail(payout)} has status {payout['status']!r}.",
            )
            continue
        if _payout_has_complex_components(payout):
            _log_unmatched_payout(
                payout, summary, SkipReason.COMPLEX_PAYOUT_COMPONENTS,
                f"{_payout_detail(payout)} includes refunds, adjustments, "
                "reserves, or retried-payout components.",
            )
            continue
        fee_cents = int(payout.get("fee_amount_cents") or 0)
        gross_cents = payout.get("gross_amount_cents")
        if fee_cents < 0 or gross_cents is None \
                or int(gross_cents) != int(payout["amount_cents"]) + fee_cents:
            _log_unmatched_payout(
                payout, summary, SkipReason.COMPLEX_PAYOUT_COMPONENTS,
                f"{_payout_detail(payout)} has components that cannot be "
                "reconciled with the simple cash-plus-fee entry.",
            )
            continue
        transaction, reason = match_payout_to_bank(payout, transactions)
        if transaction is None:
            detail = (
                f"No unique settled bank transaction matched {_payout_detail(payout)} "
                "by exact amount, payout date through five days later, and Shopify "
                "descriptor or payout ID."
            )
            _log_unmatched_payout(payout, summary, reason or SkipReason.UNMATCHED_PAYOUT, detail)
            continue
        bank_reference = bank_transaction_reference(transaction)
        if db.ledger_source_import_exists(bank_reference):
            _log_unmatched_payout(
                payout, summary, SkipReason.PLAID_ALREADY_IMPORTED,
                f"Matched bank transaction {transaction['transaction_id']} was "
                "already imported under another source reference.",
            )
            continue
        bank_account_id = db.ensure_bank_ledger_account(
            transaction.get("source"), transaction.get("account_id"))
        if bank_account_id is None:
            _log_unmatched_payout(
                payout, summary, SkipReason.UNMAPPED_BANK_ACCOUNT,
                f"Matched bank account {transaction.get('account_id') or '?'} "
                f"from {transaction.get('source') or '?'} has no Ledger mapping.",
            )
            continue
        lines = [
            {"account_id": bank_account_id,
             "debit_amount": _amount_from_cents(payout["amount_cents"])},
        ]
        if fee_cents:
            lines.append({"account_id": accounts[PAYMENT_PROCESSING_FEES],
                          "debit_amount": _amount_from_cents(fee_cents)})
        lines.append({"account_id": accounts[SHOPIFY_CLEARING],
                      "credit_amount": _amount_from_cents(gross_cents)})
        entry = ledger.create_sourced_journal_entry(
            payout["date"], f"Shopify payout #{payout['payout_id']}", lines,
            source_reference=source_reference, created_by=CREATED_BY,
        )
        db.clear_ledger_source_import_skip(source_reference)
        if entry is None:
            summary["shopify_payouts"]["already_imported"] += 1
        else:
            summary["shopify_payouts"]["created"] += 1


def _cogs_for_order(order, cogs_reference=None):
    """Cost of goods for one Shopify order, from tracked inventory.

    Returns (total_cents, missing_details, sale_lines). sale_lines is
    [(variant_id, quantity, source_reference)] so the caller can record
    the stock going out — but only after the entry actually posts.

    Cost comes from inventory acquisitions via FIFO, not from the Shopify
    cost snapshot alone. The snapshot knows what a unit is worth; only an
    acquisition knows that units were bought, and crediting Inventory
    without something having debited it is what made Inventory drift
    negative before. See inventory.py.

    Any line that cannot be costed fails the whole order. A COGS figure
    covering four of an order's five lines is not this order's COGS.
    """
    try:
        line_items = json.loads(order.get("line_items_json") or "[]")
    except (TypeError, ValueError):
        return None, ["Order line items could not be parsed."], []
    if not isinstance(line_items, list) or not line_items:
        return None, ["Order has no line items with unit-cost data."], []

    sale_date = str(order.get("created_at") or "")[:10]
    total_cents = 0
    missing = []
    sale_lines = []

    for index, item in enumerate(line_items, 1):
        if not isinstance(item, dict):
            missing.append(f"Line {index} is not a Shopify line-item object.")
            continue
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            continue
        variant_id = str(item.get("variant_id") or "").strip()
        label = item.get("title") or variant_id or f"line {index}"
        if not variant_id:
            missing.append(f"{label}: no Shopify variant_id.")
            continue

        # One sale event per order line, so a re-run consumes stock once
        # and re-costing can exclude its own outflow.
        line_reference = (f"{cogs_reference}:{variant_id}"
                          if cogs_reference else None)
        line_cents, reason = inventory.cost_of_sale(
            variant_id, quantity, sale_date,
            exclude_source_reference=line_reference)
        if line_cents is None:
            # Two different failures wear the same shape here and need
            # different fixes, so they get different messages: not
            # knowing what a unit cost (Shopify cost data missing) is not
            # the same as knowing the cost but never recording that any
            # were bought (no acquisition). Checking the snapshot tells
            # them apart.
            snapshot = db.get_shopify_variant_cost_at(variant_id, sale_date)
            if not snapshot or snapshot.get("unit_cost_cents") is None:
                missing.append(
                    f"{label} ({variant_id}): no cost snapshot captured "
                    f"before {order['created_at']}.")
            else:
                missing.append(f"{label} ({variant_id}): {reason}.")
            continue
        total_cents += line_cents
        sale_lines.append((variant_id, quantity, line_reference))

    if missing:
        return None, missing, []
    return total_cents, [], sale_lines


def _sync_shopify_cogs(accounts, summary):
    """Post cost of goods for Shopify sales that have tracked inventory.

    This used to compute a correct figure and then refuse to post it
    unconditionally, because nothing tracked how inventory was acquired
    and crediting Inventory with no matching debit misstates assets.
    Acquisitions are tracked now (inventory.py), so an order whose lines
    are all covered by FIFO layers posts; anything else still refuses and
    says why, exactly as before.
    """
    for order in db.list_shopify_orders():
        revenue_reference = f"shopify:order:{order['order_id']}"
        source_reference = revenue_reference + ":cogs"

        # Nothing to recompute once it has posted. Checking here rather
        # than relying on the insert returning None keeps the costing
        # pass off already-settled orders entirely.
        if db.ledger_source_import_exists(source_reference):
            summary["shopify_cogs"]["already_imported"] += 1
            continue

        if not db.ledger_source_import_exists(revenue_reference):
            summary["shopify_cogs"]["skipped"] += 1
            detail = "Revenue entry has not been posted for this Shopify order."
            db.log_ledger_source_import_skip(source_reference, "shopify",
                                             SkipReason.REVENUE_NOT_IMPORTED,
                                             detail)
            summary["shopify_cogs"]["skipped_orders"].append({
                "order_id": order["order_id"],
                "reason": SkipReason.REVENUE_NOT_IMPORTED, "detail": detail,
            })
            continue

        total_cents, missing, sale_lines = _cogs_for_order(
            order, cogs_reference=source_reference)

        if missing:
            detail = " ".join(missing)
            # Distinguish "we have no idea what this cost" from "we know
            # the cost but never recorded owning any", because the fix
            # differs: one needs Shopify cost data, the other needs an
            # acquisition recording.
            reason = (SkipReason.INVENTORY_ACQUISITION_NOT_TRACKED
                      if "tracked acquisitions cover" in detail
                      else SkipReason.MISSING_COST_DATA)
            db.log_ledger_source_import_skip(source_reference, "shopify",
                                             reason, detail)
            summary["shopify_cogs"]["skipped"] += 1
            summary["shopify_cogs"]["skipped_orders"].append({
                "order_id": order["order_id"], "reason": reason,
                "detail": detail,
            })
            continue

        if total_cents == 0:
            detail = ("All order lines have an available zero unit cost; "
                      "no COGS entry posted.")
            db.log_ledger_source_import_skip(source_reference, "shopify",
                                             SkipReason.ZERO_COGS, detail)
            summary["shopify_cogs"]["skipped"] += 1
            summary["shopify_cogs"]["skipped_orders"].append({
                "order_id": order["order_id"], "reason": SkipReason.ZERO_COGS,
                "detail": detail,
            })
            continue

        entry = _create_entry(
            str(order.get("created_at") or "")[:10],
            f"Shopify COGS for order {order['order_id']}",
            COGS, INVENTORY, _amount_from_cents(total_cents),
            source_reference, accounts,
        )
        if entry is None:
            summary["shopify_cogs"]["already_imported"] += 1
            continue

        # Stock leaves only once the entry is on the books, so a failed
        # post never silently consumes inventory.
        for variant_id, quantity, line_reference in sale_lines:
            inventory.record_sale(
                variant_id, quantity, str(order.get("created_at") or "")[:10],
                line_reference,
                note=f"Shopify order {order['order_id']}")
        db.clear_ledger_source_import_skip(source_reference)
        summary["shopify_cogs"]["created"] += 1


def sync_ledger_from_sources():
    """Post new local Plaid and Shopify records as balanced journal entries."""
    db.init_db()
    accounts = _accounts_by_code()
    required = {CHECKING, SHOPIFY_CLEARING, ACCOUNTS_RECEIVABLE,
                SALES_REVENUE, BANK_FEES, ADVERTISING, SOFTWARE, PAYROLL,
                UNCATEGORIZED_EXPENSE, INVENTORY, COGS,
                PAYMENT_PROCESSING_FEES, TRANSFER_CLEARING, REFUNDS_RECEIVED,
                UNCATEGORIZED_INCOME, INTEREST_INCOME,
                BANK_REWARDS_CASHBACK_INCOME,
                SALES_TAX_PAYABLE, CUSTOMER_DEPOSITS, ALLOWANCE_DOUBTFUL,
                SALES_RETURNS, BAD_DEBT_EXPENSE}
    missing = sorted(required - set(accounts))
    if missing:
        raise ledger.LedgerValidationError(
            "Ledger source-sync accounts are missing: " + ", ".join(missing)
        )
    summary = {
        "plaid": {"created": 0, "already_imported": 0, "skipped": 0,
                  "reclassified": 0},
        "shopify": {"created": 0, "already_imported": 0, "skipped": 0},
        "shopify_cogs": {"created": 0, "already_imported": 0, "skipped": 0,
                          "skipped_orders": []},
        "shopify_payouts": {"created": 0, "already_imported": 0,
                             "unmatched": 0, "unmatched_payouts": []},
    }
    # Propose invoice payments from bank deposits, so a deposit that pays an
    # invoice is claimed here and then suppressed at approval time rather than
    # double-booked as income. The suppression used to live in _sync_plaid;
    # approval owns it now (see app._post_one_to_ledger). Imported locally:
    # invoice_reconcile -> invoice_ledger -> ledger_sync would be a module
    # import cycle if done at the top.
    import invoice_reconcile
    summary["invoice_matches"] = invoice_reconcile.find_bank_matches()
    # Bank transactions are deliberately absent from this pipeline. They reach
    # the books only by being approved in Review, so that the category a person
    # chose is the one that lands and a denied item lands nowhere. See
    # _sync_plaid's own docstring for what it is kept for. Transfers are the
    # single exception, and _sync_bank_transfers says why.
    _sync_bank_transfers(accounts, summary)
    _sync_shopify(accounts, summary)
    _sync_shopify_payouts(accounts, summary)
    _sync_shopify_cogs(accounts, summary)
    summary["created"] = (summary["plaid"]["created"]
                          + summary["plaid"]["reclassified"]
                          + summary["shopify"]["created"]
                          + summary["shopify_payouts"]["created"]
                          + summary["shopify_cogs"]["created"])
    return summary
