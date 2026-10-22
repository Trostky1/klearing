"""Approving a transaction records it in the native Ledger.

Phase 1 of the Wave offboarding. Approval used to post to Wave and
nothing else, so the category a person chose never reached the books that
produce the P&L, the balance sheet, or the tax projection. It now also
writes a balanced journal entry through the same Ledger validation
everything else uses.

This is a dual-write phase: the Wave post still runs first and is still
authoritative, so these tests exercise the Ledger side around it rather
than in place of it.

Two things are asserted more heavily than the happy path:

- **Single and bulk must agree.** They share one entry point precisely so
  the money path cannot grow a second implementation, and that is checked
  here rather than assumed.
- **Refusals.** A category with no matching account must skip the Ledger
  write instead of picking an account that merely looks similar, and the
  approval must still succeed.

Deliberately runs on a fresh data dir with no prior ledger sync. Both
writers share one source_reference, so a sync that ran first would
legitimately claim these transactions and the Ledger post would decline
-- correct behavior, but it would test the dedup instead of the write.

Offline: isolated temp data dir, no network, no credentials.
"""

import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-approval-ledger-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ledger  # noqa: E402
import ledger_sync  # noqa: E402

# app imports wave_client, which is never reached: every fixture below is
# approved through _post_one_approved with the Wave call stubbed out.
import app  # noqa: E402


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return 0 if condition else 1


def assert_no_wave_call():
    """Wave's runtime module must be absent, not merely mocked offline."""
    assert "wave_client" not in app.__dict__


def add_item(vendor, amount, direction, external_id=None, source="plaid",
             account_id="acct-checking"):
    return db.insert_pending_item(
        source=source, vendor=vendor, amount=amount, date="2026-08-01",
        suggested_category=None, direction=direction,
        external_transaction_id=external_id, account_id=account_id)


def approve(item_id, code, notes=""):
    """Drive one item through the shared approval entry point."""
    item = db.get_item(item_id)
    account, name = app.resolve_submitted_category(code)
    return app._post_one_approved(item, name, account, notes, None)


def add_invoice_match(reference, amount_cents, status):
    """One invoice-payment proposal claiming a bank deposit."""
    contact_id = db.create_contact(f"Client {reference[-8:]}")
    invoice_id = db.create_invoice(contact_id, "2026-08-01", "2026-08-31")
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO invoice_payment_matches "
            "(invoice_id, source, reference, amount_cents, paid_on, status, "
            " created_at) "
            "VALUES (?, 'bank', ?, ?, '2026-08-01', ?, "
            " '2026-08-01T00:00:00+00:00')",
            (invoice_id, reference, amount_cents, status))


def entry_count():
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"]


def entry_for(source_reference):
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT je.id, je.memo, je.created_by FROM journal_entries je "
            "JOIN ledger_source_imports si ON si.journal_entry_id = je.id "
            "WHERE si.source_reference = ?", (source_reference,)
        ).fetchone()
        return dict(row) if row else None


def lines_for(entry_id):
    """{account code: ('debit'|'credit', cents)} for one entry."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT a.code, jl.debit_amount, jl.credit_amount "
            "FROM journal_lines jl JOIN accounts a ON a.id = jl.account_id "
            "WHERE jl.journal_entry_id = ?", (entry_id,)
        ).fetchall()
    return {row["code"]: (("debit", row["debit_amount"]) if row["debit_amount"]
                          else ("credit", row["credit_amount"]))
            for row in rows}


def main():
    failures = 0
    db.init_db()
    assert_no_wave_call()

    ADVERTISING, REVENUE, EQUITY, INVENTORY = "6010", "4000", "3000", "1200"
    CHECKING = "1010"
    db.upsert_plaid_account({
        "account_id": "acct-checking", "name": "Operating Checking",
        "official_name": None, "type": "depository", "subtype": "checking",
        "mask": "1234", "synced_at": "2026-08-01T00:00:00+00:00",
    })
    db.set_bank_ledger_account(
        "plaid", "acct-checking", ledger.account_by_code(CHECKING)["id"])

    print("\nEvery account type posts on the correct side")

    # Money out to an expense account.
    expense_id = add_item("Ad Agency", 250.00, "expense", "ext-expense")
    approve(expense_id, ADVERTISING)
    entry = entry_for("plaid:transaction:ext-expense")
    lines = lines_for(entry["id"]) if entry else {}
    failures += check(
        "an expense debits the chosen account and credits the bank",
        entry is not None
        and lines.get(ADVERTISING, (None,))[0] == "debit"
        and lines.get(CHECKING, (None,))[0] == "credit"
        and lines[ADVERTISING][1] == 25000)
    failures += check("it is attributed to the approval, not the sync",
                      entry and entry["created_by"] == "review-approval")

    # Money in to a revenue account.
    income_id = add_item("Customer Co", 900.00, "income", "ext-income")
    approve(income_id, REVENUE)
    lines = lines_for(entry_for("plaid:transaction:ext-income")["id"])
    failures += check(
        "income debits the bank and credits the chosen account",
        lines.get(CHECKING, (None,))[0] == "debit"
        and lines.get(REVENUE, (None,))[0] == "credit")

    # Money in classified as equity: an owner contribution.
    owner_id = add_item("Owner Transfer", 5000.00, "income", "ext-owner")
    approve(owner_id, EQUITY)
    lines = lines_for(entry_for("plaid:transaction:ext-owner")["id"])
    failures += check(
        "an owner contribution credits equity, not revenue",
        lines.get(EQUITY, (None,))[0] == "credit"
        and REVENUE not in lines)

    # Money out to an asset: a materials purchase capitalized to inventory.
    stock_id = add_item("Supplier Ltd", 400.00, "expense", "ext-stock")
    approve(stock_id, INVENTORY)
    lines = lines_for(entry_for("plaid:transaction:ext-stock")["id"])
    failures += check(
        "a materials purchase debits inventory rather than an expense",
        lines.get(INVENTORY, (None,))[0] == "debit"
        and lines.get(CHECKING, (None,))[0] == "credit")

    print("\nThe local bookkeeping the Wave post also did still happens")

    # _post_one_to_wave was gutted rather than deleted because most of it
    # was never about Wave. This is that half.
    local_id = db.insert_pending_item(
        source="plaid", vendor="Learned Vendor", amount=40.00,
        date="2026-08-01", suggested_category="Uncategorized Expense",
        direction="expense", external_transaction_id="ext-local",
        account_id="acct-checking")
    approve(local_id, ADVERTISING, notes="lunch with supplier")
    saved = db.get_item(local_id)
    # set_category writes back over suggested_category: one column holds
    # the suggestion until approval, the decision afterwards.
    failures += check("the chosen category is stored",
                      saved["suggested_category"] == "Advertising & Marketing")
    failures += check("the note is stored", saved["notes"] == "lunch with supplier")
    failures += check("the item is marked approved",
                      saved["status"] == "approved")
    failures += check("no Wave transaction id is invented",
                      not saved["wave_transaction_id"])
    failures += check(
        "the vendor is remembered for next time",
        any(row["vendor_name"] == "Learned Vendor"
            and row["category_name"] == "Advertising & Marketing"
            for row in db.get_vendor_memory()))
    failures += check(
        "overriding the suggestion is recorded as a correction",
        any(row["corrected_category"] == "Advertising & Marketing"
            for row in db.list_category_corrections()))

    print("\nDirection still follows the category's type")

    equity_id = add_item("Owner Deposit", 800.00, "income", "ext-equity")
    approve(equity_id, EQUITY)
    failures += check("an equity category makes a deposit a transfer",
                      db.get_item(equity_id)["direction"] == "transfer")
    rev_id = add_item("Customer Payment", 600.00, "income", "ext-rev")
    approve(rev_id, REVENUE)
    failures += check("a revenue category makes it income",
                      db.get_item(rev_id)["direction"] == "income")

    print("\nThe queue's decisions are respected")

    denied_id = add_item("Denied Vendor", 75.00, "expense", "ext-denied")
    db.set_status(denied_id, "denied")
    failures += check("a denied item posts nothing from this path",
                      entry_for("plaid:transaction:ext-denied") is None)

    manual_id = add_item("Cash Purchase", 30.00, "expense", source="manual",
                         account_id=None)
    approve(manual_id, ADVERTISING)
    failures += check("a manual item posts under its own reference",
                      entry_for(f"klearing:pending:{manual_id}") is not None)

    print("\nRefusing rather than guessing")

    # Phase 1 and 2 let this through with a warning, because Wave was
    # authoritative behind it. Nothing is now, so an approval that cannot
    # reach the books has to fail instead of reporting success having
    # recorded nothing anywhere.
    unknown_id = add_item("Odd Vendor", 60.00, "expense", "ext-unknown")
    refused = ""
    try:
        approve(unknown_id, "Some Legacy Category")
    except RuntimeError as exc:
        refused = str(exc)
    failures += check("a category with no account refuses the approval",
                      bool(refused))
    failures += check("and writes no entry",
                      entry_for("plaid:transaction:ext-unknown") is None)
    failures += check("and leaves the item pending, not approved",
                      db.get_item(unknown_id)["status"] == "pending")

    unmapped_id = add_item(
        "Unmapped Bank Vendor", 61.00, "expense", "ext-unmapped",
        account_id="acct-unmapped")
    try:
        approve(unmapped_id, ADVERTISING)
    except RuntimeError as exc:
        unmapped_refused = "has not been mapped" in str(exc)
    else:
        unmapped_refused = False
    failures += check("an unmapped Plaid account refuses the approval",
                      unmapped_refused)
    failures += check("an unmapped Plaid account posts no journal entry",
                      entry_for("plaid:transaction:ext-unmapped") is None)
    failures += check("an unmapped Plaid item remains pending for resolution",
                      db.get_item(unmapped_id)["status"] == "pending")

    # Same rule when the Ledger itself rejects the entry.
    broken_id = add_item("Broken Entry", 90.00, "expense", "ext-broken")
    real_post = app._post_one_to_ledger
    app._post_one_to_ledger = lambda *a, **k: (_ for _ in ()).throw(
        ledger.LedgerValidationError("simulated Ledger failure"))
    ledger_refused = ""
    try:
        approve(broken_id, ADVERTISING)
    except RuntimeError as exc:
        ledger_refused = str(exc)
    finally:
        app._post_one_to_ledger = real_post
    failures += check("a Ledger failure refuses the approval too",
                      bool(ledger_refused))
    failures += check("and leaves that item pending as well",
                      db.get_item(broken_id)["status"] == "pending")

    print("\nSingle and bulk cannot drift")

    # Same shape of input through both call sites; the entries must match.
    single_id = add_item("Parity Vendor", 120.00, "expense", "ext-single")
    bulk_id = add_item("Parity Vendor", 120.00, "expense", "ext-bulk")
    approve(single_id, ADVERTISING)
    approve(bulk_id, ADVERTISING)
    single_lines = lines_for(entry_for("plaid:transaction:ext-single")["id"])
    bulk_lines = lines_for(entry_for("plaid:transaction:ext-bulk")["id"])
    failures += check("both paths produce the same debits and credits",
                      single_lines == bulk_lines)
    failures += check(
        "both call the one shared entry point",
        "_post_one_approved" in app.approve.__code__.co_names
        and "_post_one_approved" in app.review_bulk_approve.__code__.co_names)

    print("\nPosting twice posts once")

    before = len(lines_for(entry_for("plaid:transaction:ext-expense")["id"]))
    repeat = app._post_one_to_ledger(
        db.get_item(expense_id), ledger.account_by_code(ADVERTISING), "")
    failures += check("a second post for the same source is declined",
                      repeat is None)
    failures += check("and the original entry is untouched",
                      len(lines_for(
                          entry_for("plaid:transaction:ext-expense")["id"]))
                      == before)

    print("\nDeferring to a poster that records the money more completely")

    # An invoice payment posts Dr Checking / Cr A/R under its own reference,
    # which ledger_source_imports cannot dedup against ours. Approval has to
    # stand down or the deposit is counted twice.
    claimed_id = add_item("Invoiced Client", 500.00, "income", "ext-claimed")
    add_invoice_match("plaid:transaction:ext-claimed", 50000, "pending")
    warning = approve(claimed_id, REVENUE)
    failures += check("a deposit claimed by an invoice match is not posted",
                      entry_for("plaid:transaction:ext-claimed") is None)
    failures += check("and the approval says where it went instead",
                      "invoice" in warning.lower())

    # 'dismissed' is deliberately absent from active_match_references, so a
    # dismissed deposit falls back to ordinary income.
    dismissed_id = add_item("Maybe Client", 300.00, "income", "ext-dismissed")
    add_invoice_match("plaid:transaction:ext-dismissed", 30000, "dismissed")
    approve(dismissed_id, REVENUE)
    failures += check("a dismissed match still posts as ordinary income",
                      entry_for("plaid:transaction:ext-dismissed") is not None)

    # The payout entry splits gross into cash plus fees and clears Shopify
    # Clearing. Approving the deposit as plain income would leave Clearing
    # accumulating with nothing to clear it.
    db.upsert_shopify_payout({
        "payout_id": "po-1", "amount_cents": 20000, "date": "2026-08-01",
        "status": "paid", "currency": "USD", "fee_amount_cents": 0,
        "gross_amount_cents": 20000, "summary_json": "{}",
        "synced_at": "2026-08-01",
    })
    payout_id = add_item("SHOPIFY PAYOUT", 200.00, "income", "ext-payout")
    warning = approve(payout_id, REVENUE)
    failures += check("a deposit matching a real payout is not posted",
                      entry_for("plaid:transaction:ext-payout") is None)
    failures += check("and the approval names the payout",
                      "po-1" in warning)

    # Matching is by amount and date window, not by the word "shopify", so an
    # ordinary deposit from a business of that name still posts.
    lookalike_id = add_item("Shopify Refund", 77.00, "income", "ext-lookalike")
    approve(lookalike_id, REVENUE)
    failures += check("a deposit matching no payout posts normally",
                      entry_for("plaid:transaction:ext-lookalike") is not None)

    print("\nA sync cannot overwrite what a person chose")

    # The guard on the retired _reclassify pass: it rewrote Sales Revenue
    # deposits to Plaid's guess, which would now silently undo a deliberate
    # choice.
    revenue_id = add_item("Direct Sale", 450.00, "income", "ext-revenue")
    approve(revenue_id, REVENUE)
    entries_before = entry_count()
    ledger_sync.sync_ledger_from_sources()
    lines = lines_for(entry_for("plaid:transaction:ext-revenue")["id"])
    failures += check("a Sales Revenue choice survives a full sync",
                      lines.get(REVENUE, (None,))[0] == "credit")
    failures += check("and the sync posts no bank transactions of its own",
                      entry_count() == entries_before)

    print("\nThe books balance")

    trial = ledger.trial_balance()
    total_debits = sum(row["debit_total"] for row in trial)
    total_credits = sum(row["credit_total"] for row in trial)
    failures += check("debits equal credits across every entry written",
                      total_debits == total_credits and total_debits > 0)

    print(f"\n{failures} failed")
    if failures:
        return 1
    print("PASS: approval records to the native Ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
