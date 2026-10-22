"""Minimal native double-entry ledger for Klearing Phase 1.

This module owns only the ledger primitives. It does not translate existing
bank, Shopify, Plaid, or Wave activity into journal entries yet.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import db
import continuity_artifact
import tax_categories

ACCOUNT_TYPES = frozenset({"asset", "liability", "equity", "revenue", "expense"})
_CENT = Decimal("0.01")
DEFAULT_CREATED_BY = "ledger-source-sync"
# The one place this default lives -- create_sourced_journal_entry(),
# _create_sourced_journal_entry_locked(), and create_sourced_journal_entries()'s
# per-spec default all reference this constant rather than repeating the
# same string literal three times.

# Accrual vs cash changes *when* revenue is recognized, which changes which
# journal entries exist at all -- under cash basis an invoice posts nothing
# when it is sent, only when it is paid. It is a user-visible setting rather
# than an inferred one because getting it wrong silently misstates income,
# and only the owner (or their accountant) knows which basis they file on.
#
# Scope note: this governs the invoicing paths only. Existing Plaid/Shopify
# source-sync postings are unchanged by it -- those are already driven by
# actual cash movement and post identically on either basis.
ACCOUNTING_BASIS_KEY = "accounting_basis"
ACCOUNTING_BASES = ("accrual", "cash")
DEFAULT_ACCOUNTING_BASIS = "accrual"


class LedgerValidationError(ValueError):
    """A journal entry or account reference is invalid."""


def accounting_basis():
    """The configured basis, defaulting to accrual.

    Defaults to accrual rather than cash because accrual is the stricter,
    more complete record: it posts everything cash basis would, plus the
    receivable. A user who is actually on cash basis and never touches the
    setting sees extra AR entries -- visible, and correctable. The opposite
    default would silently omit entries they needed.
    """
    value = (db.get_setting(ACCOUNTING_BASIS_KEY) or "").strip().lower()
    return value if value in ACCOUNTING_BASES else DEFAULT_ACCOUNTING_BASIS


def is_accrual_basis():
    return accounting_basis() == "accrual"


def _amount_to_cents(value, field_name):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerValidationError(f"{field_name} must be a valid amount.")
    if not amount.is_finite() or amount < 0:
        raise LedgerValidationError(f"{field_name} must be non-negative.")
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded != amount:
        raise LedgerValidationError(f"{field_name} cannot have more than two decimals.")
    return int(rounded * 100)


def _cents_to_amount(cents):
    return Decimal(cents) / 100


def _entry_date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise LedgerValidationError("date must be an ISO date (YYYY-MM-DD).")


def list_accounts():
    """Return the Chart of Accounts, including parent relationships."""
    db.init_db()
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, code, name, type, parent_account_id, tax_category "
            "FROM accounts ORDER BY code"
        ).fetchall()]


def account_by_code(code):
    """Return one account by its stable seed/user-defined code."""
    db.init_db()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, code, name, type, parent_account_id, tax_category "
            "FROM accounts WHERE code = ?", (str(code),)
        ).fetchone()
    return dict(row) if row else None


ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")

# Where a user-created account of each type is allocated. Mirrors the seed's
# banding in migrations/add_ledger_tables.sql: parents sit at x000 and their
# children step by ten. Expenses default to the Operating Expenses band
# rather than 5000, because Cost of Goods Sold carries costing meaning that
# ledger_sync and inventory rely on -- a user-invented category must never
# land there by accident.
_TYPE_BAND = {
    "asset": "1000",
    "liability": "2000",
    "equity": "3000",
    "revenue": "4000",
    "expense": "6000",
}


def _next_free_code(base_code):
    """First unused code stepping by ten inside one parent's band."""
    with db.get_connection() as conn:
        return _next_free_code_locked(conn, base_code)


def _next_free_code_locked(conn, base_code):
    """Same allocation as _next_free_code, but reads through an
    ALREADY-OPEN connection instead of opening its own -- so it sees
    uncommitted inserts made earlier in the SAME transaction. Required by
    create_accounts() below: two specs in one batch must not race for the
    same freshly-allocated code, which they would if each one queried
    "used" codes on its own separate, committed-only connection."""
    base = int(base_code)
    used = {row["code"] for row in conn.execute(
        "SELECT code FROM accounts WHERE code >= ? AND code < ?",
        (str(base), str(base + 1000)),
    ).fetchall()}
    for candidate in range(base + 10, base + 1000, 10):
        code = str(candidate)
        if code not in used:
            return code
    raise LedgerValidationError(
        "That part of the Chart of Accounts is full. Pick a different type.")


def _create_account_locked(conn, name, account_type, parent_code=None,
                           tax_category=None):
    """The real body of create_account(), run against an ALREADY-OPEN
    connection so the caller controls the transaction boundary --
    create_account() itself wraps a single call to this in its own
    ``with db.get_connection() as conn:``, and create_accounts() wraps a
    whole batch of calls in ONE such block. Every validation rule a
    single account creation has ever needed, or will ever need, lives
    here exactly once: create_accounts() inherits every one of them for
    free, and gets true all-or-nothing atomicity across a whole batch by
    construction (SQLite's own commit/rollback), rather than by a caller
    trying to keep pace with this function's rules by hand in a separate
    pre-check that is only correct until the next rule is added here.

    Order of checks matches create_account()'s original, single-item
    behavior exactly (name/length/type, tax category, parent, code
    allocation, THEN the duplicate-name check) so wrapping this in
    create_account() changes nothing observable for its existing callers.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise LedgerValidationError("Give the account a name.")
    if len(clean_name) > 100:
        raise LedgerValidationError("Account names are limited to 100 characters.")
    if account_type not in ACCOUNT_TYPES:
        raise LedgerValidationError(
            "Choose one of: " + ", ".join(ACCOUNT_TYPES) + ".")

    try:
        normalized_category = tax_categories.normalize_account_category(
            account_type, tax_category)
    except ValueError as exc:
        raise LedgerValidationError(str(exc)) from exc

    parent = None
    if parent_code:
        parent_row = conn.execute(
            "SELECT id, code, name, type, parent_account_id, tax_category "
            "FROM accounts WHERE code = ?", (str(parent_code),)
        ).fetchone()
        if parent_row is None:
            raise LedgerValidationError("That parent account no longer exists.")
        parent = dict(parent_row)
        if parent["type"] != account_type:
            raise LedgerValidationError(
                "A sub-account must share its parent's type.")

    base_code = parent["code"] if parent else _TYPE_BAND[account_type]
    code = _next_free_code_locked(conn, base_code)

    existing = conn.execute(
        "SELECT 1 FROM accounts WHERE lower(name) = lower(?)",
        (clean_name,),
    ).fetchone()
    if existing:
        raise LedgerValidationError(
            f'"{clean_name}" already exists in your Chart of Accounts.')

    cursor = conn.execute(
        "INSERT INTO accounts (code, name, type, parent_account_id, "
        "tax_category) VALUES (?, ?, ?, ?, ?)",
        (code, clean_name, account_type,
         parent["id"] if parent else None, normalized_category),
    )
    row = conn.execute(
        "SELECT id, code, name, type, parent_account_id, tax_category "
        "FROM accounts WHERE id = ?", (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


def create_account(name, account_type, parent_code=None, tax_category=None):
    """Add one user-defined account to the Chart of Accounts.

    The starter Chart is deliberately small, so a business with its own
    vocabulary needs a way to extend it. Codes are allocated rather than
    chosen: they are the stable reference integrations key on, and letting
    a user pick one invites a collision with a future seed account.

    ``tax_category`` stays None unless the caller supplies a valid Schedule C
    mapping. Per tax_categories, None means *unresolved*, never "safe to
    omit" -- tax_financials surfaces it as needing attention instead of
    guessing a treatment for money it cannot classify.
    """
    db.init_db()
    with db.get_connection() as conn:
        account = _create_account_locked(
            conn, name, account_type, parent_code=parent_code,
            tax_category=tax_category)
        continuity_artifact.mark_dirty(conn, "chart-of-accounts-changed")
    return account


def create_accounts(specs):
    """Create every (name, account_type) pair in ``specs`` inside ONE
    transaction: if creating ANY of them fails for any reason a single
    create_account() call could fail for -- duplicate name against the
    existing Chart OR against another spec earlier in this same batch, a
    name that's too long, an invalid type, code-band exhaustion, or
    anything a future validation rule adds -- the WHOLE batch is rolled
    back by SQLite's own transaction mechanics. None of the earlier,
    otherwise-valid accounts in the batch are left behind as orphans.

    This is the single source of truth for "create several accounts
    atomically." A caller must never try to enumerate create_account()'s
    individual failure modes by hand to decide a batch is safe -- that
    approach is only correct until this module's validation rules change,
    and it was already incomplete the day such a thing shipped (name
    length and code-band exhaustion are just as real a way to orphan an
    earlier account in the batch as a duplicate name is). Any caller
    creating more than one account as part of a single logical operation
    -- including money-critical ones, e.g. opening-balance journal entries
    needing several new accounts at once -- should use this function
    rather than looping create_account() itself.

    ``specs`` is a list of (name, account_type) 2-tuples. Parent account
    and tax category are not supported here -- Screen 3's "create a new
    account for this bank account" flow, the only caller today, never
    needs them. Extend the tuple shape (and the _create_account_locked
    call below) if a future caller does.

    Returns the created account dicts, in the same order as ``specs``.
    Raises LedgerValidationError, with nothing committed, on any failure.
    """
    db.init_db()
    if not specs:
        return []
    with db.get_connection() as conn:
        created = [_create_account_locked(conn, name, account_type)
                  for name, account_type in specs]
        continuity_artifact.mark_dirty(conn, "chart-of-accounts-changed")
    return created


def set_account_tax_category(account_id, category):
    """Set or clear one account's validated Schedule C mapping."""
    db.init_db()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, code, name, type, parent_account_id, tax_category "
            "FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise LedgerValidationError("That Ledger account no longer exists.")
        try:
            normalized = tax_categories.normalize_account_category(
                row["type"], category)
        except ValueError as exc:
            raise LedgerValidationError(str(exc)) from exc
        conn.execute(
            "UPDATE accounts SET tax_category = ? WHERE id = ?",
            (normalized, account_id),
        )
        updated = conn.execute(
            "SELECT id, code, name, type, parent_account_id, tax_category "
            "FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        continuity_artifact.mark_dirty(conn, "account-metadata-changed")
    return dict(updated)


def _validate_journal_entry(date_value, memo, lines, created_by,
                            source_reference):
    """Normalize an entry before it is saved in a database transaction."""
    entry_date = _entry_date(date_value)
    memo = (memo or "").strip()
    created_by = (created_by or "").strip()
    if not memo:
        raise LedgerValidationError("memo is required.")
    if not created_by:
        raise LedgerValidationError("created_by is required.")
    if not isinstance(lines, (list, tuple)) or len(lines) < 2:
        raise LedgerValidationError("a journal entry needs at least two lines.")

    normalized = []
    debit_total = 0
    credit_total = 0
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict) or line.get("account_id") is None:
            raise LedgerValidationError(f"line {index} needs an account_id.")
        debit = _amount_to_cents(line.get("debit_amount", 0),
                                 f"line {index} debit_amount")
        credit = _amount_to_cents(line.get("credit_amount", 0),
                                  f"line {index} credit_amount")
        if (debit == 0) == (credit == 0):
            raise LedgerValidationError(
                f"line {index} must have exactly one positive debit or credit."
            )
        normalized.append((line["account_id"], debit, credit))
        debit_total += debit
        credit_total += credit

    if debit_total != credit_total:
        raise LedgerValidationError(
            f"journal entry is not balanced: debits={debit_total / 100:.2f}, "
            f"credits={credit_total / 100:.2f}."
        )
    return (entry_date, memo, created_by, source_reference, normalized,
            debit_total, credit_total)


def _save_journal_entry(conn, entry_date, memo, created_by, source_reference,
                        normalized, debit_total, credit_total):
    account_ids = {row[0] for row in conn.execute("SELECT id FROM accounts")}
    unknown = [account_id for account_id, _, _ in normalized
               if account_id not in account_ids]
    if unknown:
        raise LedgerValidationError(
            "journal entry references unknown account(s): "
            + ", ".join(str(value) for value in unknown)
        )
    created_at = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "INSERT INTO journal_entries "
        "(date, memo, created_by, source_reference, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entry_date, memo, created_by, source_reference, created_at),
    )
    entry_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO journal_lines "
        "(journal_entry_id, account_id, debit_amount, credit_amount) "
        "VALUES (?, ?, ?, ?)",
        [(entry_id, account_id, debit, credit)
         for account_id, debit, credit in normalized],
    )
    continuity_artifact.mark_dirty(conn, "posted-journal-changed")
    return {
        "id": entry_id,
        "date": entry_date,
        "memo": memo,
        "created_by": created_by,
        "source_reference": source_reference,
        "debit_total": _cents_to_amount(debit_total),
        "credit_total": _cents_to_amount(credit_total),
    }


def create_journal_entry(date_value, memo, lines, created_by="system",
                         source_reference=None):
    """Validate and save one balanced journal entry atomically.

    Amounts passed to this function are decimal currency units (for example,
    125.50); the database stores exact integer cents.
    Each line must contain an account_id and exactly one positive debit or
    credit amount. The entry must contain at least two lines and balance.
    """
    prepared = _validate_journal_entry(date_value, memo, lines, created_by,
                                       source_reference)

    db.init_db()
    with db.get_connection() as conn:
        return _save_journal_entry(conn, *prepared)


def _create_sourced_journal_entry_locked(conn, date_value, memo, lines,
                                         source_reference,
                                         created_by=DEFAULT_CREATED_BY):
    """The real body of create_sourced_journal_entry(), run against an
    ALREADY-OPEN connection so the caller controls the transaction
    boundary -- mirrors _create_account_locked()'s precedent exactly.
    create_sourced_journal_entry() itself wraps a single call to this in
    its own ``with db.get_connection() as conn:``, and
    create_sourced_journal_entries() below wraps a whole batch of calls
    in ONE such block, so a failure on spec N rolls back every spec
    already saved earlier in the SAME call too, by construction
    (SQLite's own commit/rollback on the shared connection) -- not by a
    caller trying to keep pace with this function's failure modes by
    hand.

    ``lines`` amounts are decimal currency units (e.g. 125.50), never
    raw cents and never a float -- _amount_to_cents (via
    _validate_journal_entry) is the one place that turns them into
    integer cents for storage.

    Returns ``None`` when ``source_reference`` was already posted
    (idempotent skip -- the existing-import check below runs against
    THIS connection, so it also sees an insert made earlier in the same
    batch, not just an already-committed one from a separate call).
    Returns the created entry dict otherwise. Raises
    LedgerValidationError on any other failure -- nothing from THIS call
    is committed, and neither is anything else already run on the same
    shared connection when called from create_sourced_journal_entries().
    """
    source_reference = (source_reference or "").strip()
    if not source_reference:
        raise LedgerValidationError("source_reference is required.")
    prepared = _validate_journal_entry(date_value, memo, lines, created_by,
                                       source_reference)
    existing = conn.execute(
        "SELECT journal_entry_id FROM ledger_source_imports "
        "WHERE source_reference = ?", (source_reference,)
    ).fetchone()
    if existing:
        return None
    entry = _save_journal_entry(conn, *prepared)
    conn.execute(
        "INSERT INTO ledger_source_imports "
        "(source_reference, journal_entry_id, imported_at) VALUES (?, ?, ?)",
        (source_reference, entry["id"], datetime.now(timezone.utc).isoformat()),
    )
    return entry


def create_sourced_journal_entry(date_value, memo, lines, source_reference,
                                  created_by=DEFAULT_CREATED_BY):
    """Create an entry once per source reference, atomically and idempotently.

    ``ledger_source_imports`` is intentionally the durable duplicate guard;
    callers receive ``None`` when the source was already posted.
    """
    db.init_db()
    with db.get_connection() as conn:
        return _create_sourced_journal_entry_locked(
            conn, date_value, memo, lines, source_reference, created_by)


def create_sourced_journal_entries(specs):
    """Create every sourced-journal-entry spec in ``specs`` inside ONE
    transaction: if creating ANY of them fails for any reason a single
    create_sourced_journal_entry() call could fail for, every entry
    already saved earlier in the SAME call is rolled back too, by
    SQLite's own transaction mechanics -- mirrors create_accounts()'s
    precedent for Chart-of-Accounts rows in that respect, and for the
    same reason: a caller posting several related entries as one
    logical operation (e.g. SimpleFIN's setup flow posting one
    opening-balance entry per mapped account) must never end up with
    some of them posted and others silently missing because entry 3 of
    5 failed validation.

    ``specs`` is a list of dicts, each with keys ``date_value``,
    ``memo``, ``lines``, ``source_reference``, and optionally
    ``created_by`` (defaults to DEFAULT_CREATED_BY per spec, matching
    the singular function's own default).

    On ONE specific point this deliberately does NOT mirror
    create_accounts(): create_accounts() RAISES on an intra-batch
    duplicate name, and so does this function on an intra-batch
    duplicate ``source_reference`` -- checked up front, before any
    connection is even opened, so a caller bug (two specs deriving the
    same reference from a non-unique field) is loud and refuses the
    WHOLE batch rather than silently losing one of the two entries.
    This is DIFFERENT from -- and must not be confused with -- the
    legitimate, graceful case: a source_reference already COMMITTED by
    an earlier, SEPARATE request (a genuine double-submit/retry). That
    case is still a silent, idempotent skip (None in that spec's result
    slot), exactly as create_sourced_journal_entry() has always
    behaved, and is what keeps a retried finish request from double-
    posting. Only a duplicate reference WITHIN this same specs list is
    treated as an error; a duplicate against something already in
    ledger_source_imports from before this call is not.

    Returns a list of results in the same order as ``specs`` -- each
    either the created entry dict, or None for an idempotent skip
    (already committed by an earlier, separate call).
    Raises LedgerValidationError, with NOTHING in the batch committed,
    if any spec fails for any reason -- including an intra-batch
    duplicate source_reference.
    """
    db.init_db()
    if not specs:
        return []

    # Intra-batch duplicate check -- pure Python, no connection needed,
    # so it can never leave a partial write behind. Blank/empty
    # references are deliberately excluded here: each spec's own
    # required-reference check (inside _create_sourced_journal_entry_locked)
    # already raises a clear, specific error for that case, and folding
    # them into "duplicate" here would produce a more confusing message
    # for what is really a separate problem.
    seen_references = set()
    for spec in specs:
        reference = (spec.get("source_reference") or "").strip()
        if not reference:
            continue
        if reference in seen_references:
            raise LedgerValidationError(
                f'duplicate source_reference "{reference}" within the '
                "same batch -- this is a caller bug (two specs derived "
                "the same reference), not a legitimate double-submit "
                "(which is only about a reference already committed by "
                "an earlier, separate request).")
        seen_references.add(reference)

    with db.get_connection() as conn:
        return [
            _create_sourced_journal_entry_locked(
                conn, spec["date_value"], spec["memo"], spec["lines"],
                spec["source_reference"],
                spec.get("created_by", DEFAULT_CREATED_BY))
            for spec in specs
        ]


def trial_balance():
    """Return current debit, credit, and normal balances by account."""
    db.init_db()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT a.id, a.code, a.name, a.type, a.parent_account_id, "
            "a.tax_category, "
            "COALESCE(SUM(jl.debit_amount), 0) AS debit_cents, "
            "COALESCE(SUM(jl.credit_amount), 0) AS credit_cents "
            "FROM accounts a LEFT JOIN journal_lines jl "
            "ON jl.account_id = a.id "
            "GROUP BY a.id, a.code, a.name, a.type, a.parent_account_id, "
            "a.tax_category "
            "ORDER BY a.code"
        ).fetchall()
    result = []
    for row in rows:
        debit = row["debit_cents"]
        credit = row["credit_cents"]
        raw_balance = debit - credit
        normal_balance = (raw_balance if row["type"] in ("asset", "expense")
                          else -raw_balance)
        result.append({
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "type": row["type"],
            "parent_account_id": row["parent_account_id"],
            "tax_category": row["tax_category"],
            "debit_total": _cents_to_amount(debit),
            "credit_total": _cents_to_amount(credit),
            "balance": _cents_to_amount(raw_balance),
            "normal_balance": _cents_to_amount(normal_balance),
        })
    return result


def _activity_by_account(start_date=None, end_date=None):
    """Return dated journal activity grouped by account.

    Date predicates live on the journal-entry join so accounts with no
    activity in the requested period remain visible with zero totals. Only
    journal lines that were actually saved are included; skipped or unmatched
    source records have no lines and therefore cannot affect these reports.
    """
    normalized_start = _entry_date(start_date) if start_date is not None else None
    normalized_end = _entry_date(end_date) if end_date is not None else None
    if (normalized_start is not None and normalized_end is not None
            and normalized_start > normalized_end):
        raise LedgerValidationError("start_date must be on or before end_date.")

    join_predicates = []
    params = []
    if normalized_start is not None:
        join_predicates.append("je.date >= ?")
        params.append(normalized_start)
    if normalized_end is not None:
        join_predicates.append("je.date <= ?")
        params.append(normalized_end)
    date_join = ""
    if join_predicates:
        date_join = " AND " + " AND ".join(join_predicates)

    db.init_db()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT a.id, a.code, a.name, a.type, a.parent_account_id, "
            "a.tax_category, "
            "parent.code AS parent_code, "
            "COALESCE(SUM(CASE WHEN je.id IS NOT NULL "
            "THEN jl.debit_amount ELSE 0 END), 0) AS debit_cents, "
            "COALESCE(SUM(CASE WHEN je.id IS NOT NULL "
            "THEN jl.credit_amount ELSE 0 END), 0) AS credit_cents "
            "FROM accounts a "
            "LEFT JOIN accounts parent ON parent.id = a.parent_account_id "
            "LEFT JOIN journal_lines jl ON jl.account_id = a.id "
            "LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id"
            + date_join + " "
            "GROUP BY a.id, a.code, a.name, a.type, a.parent_account_id, "
            "a.tax_category, "
            "parent.code ORDER BY a.code",
            params,
        ).fetchall()
    return rows


def _report_account(row, amount):
    """Format one account's activity for a structured financial report."""
    debit = row["debit_cents"]
    credit = row["credit_cents"]
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "type": row["type"],
        "parent_account_id": row["parent_account_id"],
        "tax_category": row["tax_category"],
        "debit_total": _cents_to_amount(debit),
        "credit_total": _cents_to_amount(credit),
        "amount": _cents_to_amount(amount),
    }


def profit_and_loss(start_date, end_date):
    """Return revenue, COGS, operating expenses, and net income for a range.

    Dates are inclusive. COGS is the seeded ``5000`` account and any future
    sub-accounts beneath it; all other expense accounts are operating
    expenses. Amounts use each account type's normal sign convention.
    """
    normalized_start = _entry_date(start_date)
    normalized_end = _entry_date(end_date)
    rows = _activity_by_account(normalized_start, normalized_end)

    revenue = []
    cogs = []
    operating_expenses = []
    for row in rows:
        if row["type"] == "revenue":
            revenue.append(_report_account(
                row, row["credit_cents"] - row["debit_cents"]))
        elif row["type"] == "expense":
            amount = row["debit_cents"] - row["credit_cents"]
            account = _report_account(row, amount)
            if row["code"] == "5000" or row["parent_code"] == "5000":
                cogs.append(account)
            else:
                operating_expenses.append(account)

    zero = Decimal("0")
    revenue_total = sum((row["amount"] for row in revenue), zero)
    cogs_total = sum((row["amount"] for row in cogs), zero)
    operating_total = sum((row["amount"] for row in operating_expenses), zero)
    gross_profit = revenue_total - cogs_total
    net_income = gross_profit - operating_total
    return {
        "start_date": normalized_start,
        "end_date": normalized_end,
        "revenue": revenue,
        "revenue_by_account": revenue,
        "revenue_total": revenue_total,
        "cogs": cogs,
        "cogs_by_account": cogs,
        "cogs_total": cogs_total,
        "gross_profit": gross_profit,
        "operating_expenses": operating_expenses,
        "operating_expenses_by_account": operating_expenses,
        "operating_expenses_total": operating_total,
        "net_income": net_income,
    }


def balance_sheet(as_of_date):
    """Return asset, liability, and equity balances through an as-of date.

    Revenue and expense activity is reported as ``current_earnings`` because
    the Phase 1 ledger has no closing-entry workflow yet. The balance check
    therefore uses account equity plus cumulative current earnings.
    """
    normalized_date = _entry_date(as_of_date)
    rows = _activity_by_account(end_date=normalized_date)
    assets = []
    liabilities = []
    equity_accounts = []
    revenue_total = Decimal("0")
    expense_total = Decimal("0")

    for row in rows:
        if row["type"] == "asset":
            assets.append(_report_account(
                row, row["debit_cents"] - row["credit_cents"]))
        elif row["type"] == "liability":
            liabilities.append(_report_account(
                row, row["credit_cents"] - row["debit_cents"]))
        elif row["type"] == "equity":
            equity_accounts.append(_report_account(
                row, row["credit_cents"] - row["debit_cents"]))
        elif row["type"] == "revenue":
            revenue_total += _cents_to_amount(
                row["credit_cents"] - row["debit_cents"])
        elif row["type"] == "expense":
            expense_total += _cents_to_amount(
                row["debit_cents"] - row["credit_cents"])

    zero = Decimal("0")
    assets_total = sum((row["amount"] for row in assets), zero)
    liabilities_total = sum((row["amount"] for row in liabilities), zero)
    equity_accounts_total = sum((row["amount"] for row in equity_accounts), zero)
    current_earnings = revenue_total - expense_total
    equity_total = equity_accounts_total + current_earnings
    difference = assets_total - liabilities_total - equity_total
    return {
        "as_of_date": normalized_date,
        "assets": assets,
        "assets_total": assets_total,
        "liabilities": liabilities,
        "liabilities_total": liabilities_total,
        "equity": equity_accounts,
        "equity_accounts": equity_accounts,
        "equity_accounts_total": equity_accounts_total,
        "current_earnings": current_earnings,
        "equity_total": equity_total,
        "difference": difference,
        "balances": difference == zero,
        "is_balanced": difference == zero,
    }
