"""
SQLite storage for pending financial items.

One table, pending_items, tracks every transaction waiting for a
human decision, plus a small meta table for bookkeeping values like
the last time the bank was checked.

Statuses: "pending" (awaiting a click), "approved" (posted to Wave,
wave_transaction_id filled in), "denied" (dismissed, never posted).

Direction: "expense" (money out, categorized into an EXPENSE
account) or "income" (money in, categorized into an INCOME account).

The database lives in the per-user app data directory (see paths.py),
never next to the executable.
"""

import sqlite3
from decimal import Decimal, InvalidOperation

import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                  -- bank adapter id or "manual"
    vendor TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,         -- positive integer cents
    date TEXT NOT NULL,                    -- YYYY-MM-DD
    direction TEXT NOT NULL DEFAULT 'expense', -- "expense" or "income"
    suggested_category TEXT,
    category_confidence TEXT,              -- high | medium | NULL
    category_source TEXT,                  -- rule | ollama | NULL
    notes TEXT,                            -- optional free text, set at
                                           -- approval time (this IS the
                                           -- accountant-export "Note" field
                                           -- — one column, not a second)
    evidence_type TEXT,                    -- optional: receipt | invoice |
                                           -- bank_statement | contract |
                                           -- other (see accountant_export.py)
    external_transaction_id TEXT,          -- the bank's id; NULL for manual
    status TEXT NOT NULL DEFAULT 'pending',
    wave_transaction_id TEXT,              -- filled in after approval
    business_id TEXT,                      -- which Wave business it was
                                           -- posted to; NULL until approved
    -- Dedup is per bank: two adapters may legitimately use the same
    -- id scheme, so uniqueness is scoped to the source.
    UNIQUE(source, external_transaction_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendor_memory (
    vendor_name TEXT NOT NULL,
    business_id TEXT NOT NULL DEFAULT '',       -- learned per business:
                                                -- category names belong to
                                                -- one chart of accounts
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'expense',  -- learned separately per
                                                -- money direction, so an
                                                -- expense category is never
                                                -- suggested for income
    times_confirmed INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (vendor_name, business_id)
);

CREATE TABLE IF NOT EXISTS category_corrections (
    vendor_pattern TEXT NOT NULL,
    corrected_category TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    last_seen TIMESTAMP,
    PRIMARY KEY (vendor_pattern, corrected_category)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ga4_snapshots (
    date TEXT PRIMARY KEY,               -- YYYY-MM-DD, one per day
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_memory (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliation_history (
    date TEXT PRIMARY KEY,               -- YYYY-MM-DD, one per day
    wave_balance_cents INTEGER NOT NULL,
    bank_balance_cents INTEGER NOT NULL,
    residual_cents INTEGER NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,  -- 0/1
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    name TEXT PRIMARY KEY,
    encrypted_value BLOB NOT NULL,       -- Fernet, master key in OS store
    is_file BOOLEAN NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT,                    -- which Wave business the invoice
                                         -- belongs to
    wave_invoice_id TEXT NOT NULL,
    invoice_number TEXT,
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    amount_due TEXT NOT NULL,            -- e.g. "37.18 USD"
    due_date TEXT NOT NULL,              -- YYYY-MM-DD
    status TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | sent | paid | skipped
    last_sent_at TEXT,                   -- when sent OR skipped (drives the
                                         -- 7-day redraft clock for both)
    draft_subject TEXT,
    draft_body TEXT
);

CREATE TABLE IF NOT EXISTS quote_followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    klaviyo_profile_id TEXT UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    program TEXT NOT NULL,
    roster_size TEXT,
    sport_goal TEXT,
    submitted_at TEXT NOT NULL,          -- ISO timestamp of the inquiry
    status TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | sent | replied | skipped
    draft_subject TEXT,
    draft_body TEXT,
    gmail_thread_checked_at TEXT         -- last IMAP reply-check timestamp
);

-- Every bank transaction this app has posted to Wave, keyed by the
-- bank's own transaction id. A guard against re-posting the same bank
-- transaction (which would create a duplicate in Wave). Not scoped to
-- a business — the same bank charge should never be posted twice, to
-- any business — and it rides along in backups, so history stays
-- protected. `posted_by` names the tool that posted (this app writes
-- "klearing").
CREATE TABLE IF NOT EXISTS wave_posted_transactions (
    mercury_transaction_id TEXT PRIMARY KEY,   -- the bank's txn id
    wave_transaction_id TEXT,
    posted_at TEXT,                            -- ISO timestamp
    posted_by TEXT
);

-- Manual sales-tax nexus tracker (a local tracking aid, not tax advice).
-- Thresholds vary by state and change over time, so they are entered by
-- the user per state — never hardcoded.
CREATE TABLE IF NOT EXISTS sales_tax_nexus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,
    revenue_cents INTEGER NOT NULL DEFAULT 0,
                                             -- cumulative revenue into the state
    threshold_cents INTEGER,                 -- user-entered; NULL = not set yet
    updated_at TEXT NOT NULL
);

-- One row per month a user has run the Accountant Export close workflow
-- for. Purely a record of what was generated and when — it does not
-- lock the underlying transactions; a period can be re-closed (the
-- freshest close overwrites, same convention as reconciliation_history).
CREATE TABLE IF NOT EXISTS monthly_closes (
    period TEXT PRIMARY KEY,               -- YYYY-MM
    closed_at TEXT NOT NULL,               -- ISO timestamp of the last close
    pdf_generated INTEGER NOT NULL DEFAULT 0,  -- 0/1
    csv_generated INTEGER NOT NULL DEFAULT 0,  -- 0/1
    notes TEXT                             -- exceptions the user flagged at close time
);

CREATE TABLE IF NOT EXISTS shopify_orders (
    order_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
    total_price_cents INTEGER NOT NULL, subtotal_price_cents INTEGER NOT NULL,
    total_discounts_cents INTEGER NOT NULL, total_tax_cents INTEGER NOT NULL,
    financial_status TEXT, fulfillment_status TEXT, customer_id TEXT,
    customer_email TEXT, customer_name TEXT, source TEXT,
    refund_total_cents INTEGER NOT NULL DEFAULT 0, line_items_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopify_products (
    product_id TEXT PRIMARY KEY, title TEXT NOT NULL, product_type TEXT,
    vendor TEXT, status TEXT, created_at TEXT, variants_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopify_customers (
    customer_id TEXT PRIMARY KEY, email TEXT, first_name TEXT, last_name TEXT,
    orders_count INTEGER NOT NULL DEFAULT 0,
    total_spent_cents INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, tags TEXT, accepts_marketing INTEGER NOT NULL DEFAULT 0
);
"""

_CENT = Decimal("0.01")
_PENDING_MONEY = {"amount_cents": "amount"}
_RECONCILIATION_MONEY = {
    "wave_balance_cents": "wave_balance",
    "bank_balance_cents": "bank_balance",
    "residual_cents": "residual",
}
_NEXUS_MONEY = {
    "revenue_cents": "revenue",
    "threshold_cents": "threshold",
}
_SHOPIFY_ORDER_MONEY = {
    "total_price_cents": "total_price",
    "subtotal_price_cents": "subtotal_price",
    "total_discounts_cents": "total_discounts",
    "total_tax_cents": "total_tax",
    "refund_total_cents": "refund_total",
}
_SHOPIFY_CUSTOMER_MONEY = {"total_spent_cents": "total_spent"}


def _money_to_cents(value, field="money value", allow_none=False):
    """Parse a public dollar value without silently discarding fractions."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} is required.")
    try:
        amount = Decimal(str(value))
        rounded = amount.quantize(_CENT)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid dollar amount.") from exc
    if not amount.is_finite() or amount != rounded:
        raise ValueError(f"{field} must have no more than two decimal places.")
    return int(amount * 100)


def _money_from_cents(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Stored money value must be integer cents, got {value!r}.")
    return Decimal(value) / 100


def _public_money_row(row, mapping):
    if row is None:
        return None
    result = dict(row)
    for cents_column, public_name in mapping.items():
        if cents_column in result:
            result[public_name] = _money_from_cents(result.pop(cents_column))
    return result


def _storage_money_row(row, mapping):
    result = dict(row)
    for cents_column, public_name in mapping.items():
        if public_name not in result:
            continue
        if cents_column in result:
            raise ValueError(
                f"Pass {public_name}, not both {public_name} and {cents_column}.")
        result[cents_column] = _money_to_cents(
            result.pop(public_name), public_name)
    return result


def get_connection():
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    return conn


# The full, ordered set of step names _schema_migration_plan() must produce,
# frozen independently of versioned_migration_steps()'s own output so a step
# added to the wrong tuple (see the comment on that tuple's post_migrations)
# cannot pass just because the total count still matches
# CURRENT_SCHEMA_VERSION. A positional check against only the last one or two
# entries is not enough: a step inserted earlier in
# versioned_migration_steps()'s post_migrations tuple (with a matching
# _VERSION_NAMES bump, which the length check requires anyway) leaves
# "wave-duplicate-post-history" and "bank-account-mapping-state" exactly
# where they already were -- still last, still second-to-last -- so it needs
# the WHOLE sequence compared, not just its tail. When a new migration is
# added correctly (appended to this function's own list, after
# "bank-account-mapping-state"), update this tuple to match -- that is the
# expected, correct maintenance cost of a positional check.
_EXPECTED_SCHEMA_MIGRATION_STEP_NAMES = (
    "legacy-core-schema",
    "tax-tables",
    "ledger-tables",
    "ledger-source-sync",
    "shopify-inventory-costs",
    "shopify-payouts",
    "ledger-routing",
    "contacts",
    "invoicing",
    "invoice-matches",
    "contact-pipeline",
    "recurring-invoices",
    "inventory",
    "shopify-analytics",
    "insight-widget-config",
    "account-tax-categories",
    "planning-source-retirement",
    "bank-rewards-account",
    "inventory-accounting-fields",
    "canonical-bank-sources",
    "integer-cent-money-storage",
    "wave-duplicate-post-history",
    "bank-account-mapping-state",
)


def _schema_migration_plan():
    from migrations.run_migrations import (
        _ensure_bank_account_mapping_state, versioned_migration_steps,
    )

    def create_legacy_core_schema():
        conn = get_connection()
        try:
            with conn:
                conn.executescript(SCHEMA)
        finally:
            conn.close()
        _migrate_columns()

    def apply_bank_account_mapping_state():
        conn = get_connection()
        try:
            with conn:
                _ensure_bank_account_mapping_state(conn)
        finally:
            conn.close()

    plan = [
        ("legacy-core-schema", create_legacy_core_schema),
        *versioned_migration_steps(),
        ("wave-duplicate-post-history", _backfill_wave_posted),
        ("bank-account-mapping-state", apply_bank_account_mapping_state),
    ]
    # versioned_migration_steps()'s post_migrations tuple is the wrong home
    # for a new step (see the comment there): appending there lands BEFORE
    # the fixed "wave-duplicate-post-history"/"bank-account-mapping-state"
    # steps above, silently shifting their version numbers even though both
    # stay last/second-to-last -- so a tail-only check would miss it.
    # ensure_current_schema() only checks len(plan) == CURRENT_SCHEMA_VERSION,
    # not step identity or order, so that kind of misplacement would stamp a
    # database current while quietly skipping (or misattributing) a
    # migration. Compare the WHOLE sequence against a frozen, independent
    # expected order so a future misplacement -- anywhere in the list, not
    # just at the end -- fails loudly here instead.
    actual_names = tuple(name for name, _step in plan)
    assert actual_names == _EXPECTED_SCHEMA_MIGRATION_STEP_NAMES, (
        "The schema migration plan's step order/identity drifted from the "
        "expected sequence.\n  expected: "
        f"{_EXPECTED_SCHEMA_MIGRATION_STEP_NAMES!r}\n  actual:   "
        f"{actual_names!r}\nA new post-migration was likely appended to "
        "versioned_migration_steps()'s post_migrations tuple (the wrong "
        "place) instead of to this function's own list, after "
        "'bank-account-mapping-state'. If a new step was added correctly, "
        "at the end of this function's literal list, update "
        "_EXPECTED_SCHEMA_MIGRATION_STEP_NAMES to match.")
    return plan


def init_db():
    from migrations.schema_versions import ensure_current_schema

    ensure_current_schema(_schema_migration_plan())
    _record_first_seen()


def _migrate_columns():
    """Add columns introduced after a table first shipped. CREATE TABLE
    IF NOT EXISTS never alters an existing table, so a column added to
    the schema needs an explicit, idempotent ALTER for installs that
    already have the old table."""
    with get_connection() as conn:
        cols = {row["name"]
                for row in conn.execute("PRAGMA table_info(pending_items)")}
        if "notes" not in cols:
            conn.execute("ALTER TABLE pending_items ADD COLUMN notes TEXT")
        if "evidence_type" not in cols:
            conn.execute(
                "ALTER TABLE pending_items ADD COLUMN evidence_type TEXT")
        if "account_id" not in cols:
            conn.execute(
                "ALTER TABLE pending_items ADD COLUMN account_id TEXT")
        if "category_confidence" not in cols:
            conn.execute(
                "ALTER TABLE pending_items ADD COLUMN category_confidence TEXT")
        if "category_source" not in cols:
            conn.execute(
                "ALTER TABLE pending_items ADD COLUMN category_source TEXT")


# The onboarding anchor for the 30-day cleanup nudge: the earliest
# transaction date if any history exists (so an upgrade of a
# long-running install anchors to real history, not the upgrade day),
# otherwise today. Stamped once and never moved.
FIRST_SEEN_KEY = "first_seen_date"


def _record_first_seen():
    if get_meta(FIRST_SEEN_KEY):
        return
    from datetime import date
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MIN(date) AS d FROM pending_items"
        ).fetchone()
    earliest = row["d"] if row and row["d"] else None
    set_meta(FIRST_SEEN_KEY, earliest or date.today().isoformat())


BACKFILL_FLAG = "wave_posted_backfilled_v1"


def _backfill_wave_posted():
    """One-time: seed the duplicate-post guard from history so items
    already approved before this feature existed are protected too.
    Guarded by a meta flag so it runs once, not per request."""
    if get_meta(BACKFILL_FLAG):
        return
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO wave_posted_transactions
                (mercury_transaction_id, wave_transaction_id, posted_at, posted_by)
            SELECT external_transaction_id, wave_transaction_id,
                   NULL, 'klearing'
            FROM pending_items
            WHERE status = 'approved'
              AND external_transaction_id IS NOT NULL
              AND wave_transaction_id IS NOT NULL
              AND wave_transaction_id != 'TEST-NO-REAL-POST'
            """
        )
    set_meta(BACKFILL_FLAG, "1")


def get_wave_posted(mercury_transaction_id):
    """The guard row for a bank transaction id, or None if this app
    has never posted it to Wave."""
    if not mercury_transaction_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM wave_posted_transactions "
            "WHERE mercury_transaction_id = ?",
            (mercury_transaction_id,),
        ).fetchone()
        return dict(row) if row else None


def record_wave_post(mercury_transaction_id, wave_transaction_id, posted_by,
                     posted_at=None):
    """Record that a bank transaction was posted to Wave. Idempotent —
    a later (forced) re-post updates the row rather than erroring."""
    from datetime import datetime, timezone
    posted_at = posted_at or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO wave_posted_transactions "
            "(mercury_transaction_id, wave_transaction_id, posted_at, posted_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mercury_transaction_id) DO UPDATE SET "
            "wave_transaction_id = excluded.wave_transaction_id, "
            "posted_at = excluded.posted_at, posted_by = excluded.posted_by",
            (mercury_transaction_id, wave_transaction_id, posted_at, posted_by),
        )


def count_wave_posted():
    with get_connection() as conn:
        return conn.execute(
            "SELECT count(*) c FROM wave_posted_transactions"
        ).fetchone()["c"]


def list_wave_posted():
    """Every recorded Wave post, keyed later by the caller — used by the
    audit trail to show when an approved bank item was posted."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM wave_posted_transactions"
        ).fetchall()]


# -------------------------------------------------- sales-tax nexus tracker


def add_nexus_state(state, revenue, threshold):
    from datetime import datetime, timezone
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO sales_tax_nexus "
            "(state, revenue_cents, threshold_cents, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (state, _money_to_cents(revenue, "revenue"),
             _money_to_cents(threshold, "threshold", allow_none=True),
             datetime.now(timezone.utc).isoformat()),
        )
        return cursor.lastrowid


def list_nexus_states():
    with get_connection() as conn:
        return [_public_money_row(row, _NEXUS_MONEY) for row in conn.execute(
            "SELECT * FROM sales_tax_nexus ORDER BY state COLLATE NOCASE, id"
        ).fetchall()]


def update_nexus_state(state_id, state, revenue, threshold):
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "UPDATE sales_tax_nexus SET state = ?, revenue_cents = ?, "
            "threshold_cents = ?, updated_at = ? WHERE id = ?",
            (state, _money_to_cents(revenue, "revenue"),
             _money_to_cents(threshold, "threshold", allow_none=True),
             datetime.now(timezone.utc).isoformat(), state_id),
        )


def delete_nexus_state(state_id):
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM sales_tax_nexus WHERE id = ?", (state_id,)
        )
        return cursor.rowcount


def _category_storage_value(value):
    """Keep old installs writable while new schemas allow NULL."""
    if value is not None:
        return value
    with get_connection() as conn:
        row = next((row for row in conn.execute(
            "PRAGMA table_info(pending_items)"
        ) if row["name"] == "suggested_category"), None)
    return "" if row and row["notnull"] else None


def insert_pending_item(source, vendor, amount, date, suggested_category,
                        direction="expense", external_transaction_id=None,
                        account_id=None, category_confidence=None,
                        category_source=None):
    """Insert a new pending item. Returns its row id."""
    suggested_category = _category_storage_value(suggested_category)
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO pending_items
                (source, vendor, amount_cents, date, direction, suggested_category,
                 external_transaction_id, account_id, category_confidence,
                 category_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, vendor, _money_to_cents(amount, "amount"), date,
             direction, suggested_category,
             external_transaction_id, account_id, category_confidence,
             category_source),
        )
        return cursor.lastrowid


def external_id_exists(source, external_transaction_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_items "
            "WHERE source = ? AND external_transaction_id = ?",
            (source, external_transaction_id),
        ).fetchone()
        return row is not None


def transaction_match_exists(date, amount, vendor):
    """Match a manual statement row against any known transaction."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_items WHERE date = ? AND amount_cents = ? "
            "AND lower(vendor) = lower(?) LIMIT 1",
            (date, _money_to_cents(amount, "amount"), vendor),
        ).fetchone()
        return row is not None


def list_items(status=None):
    query = "SELECT * FROM pending_items"
    params = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY date DESC, id DESC"
    with get_connection() as conn:
        return [_public_money_row(row, _PENDING_MONEY)
                for row in conn.execute(query, params).fetchall()]


def list_items_in_range(status, start_date, end_date, business_id=None):
    """Items with the given status whose date falls in [start, end].
    Dates are YYYY-MM-DD strings, so lexicographic compare is correct.
    With business_id, approved items are limited to that business;
    rows with no business stamp (pending, manual/test) still match."""
    query = """
        SELECT * FROM pending_items
        WHERE status = ? AND date >= ? AND date <= ?
    """
    params = [status, start_date, end_date]
    if business_id is not None:
        query += " AND (business_id = ? OR business_id IS NULL)"
        params.append(business_id)
    query += " ORDER BY date DESC, id DESC"
    with get_connection() as conn:
        return [_public_money_row(row, _PENDING_MONEY)
                for row in conn.execute(query, params).fetchall()]


def get_item(item_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM pending_items WHERE id = ?", (item_id,)
        ).fetchone()
        return _public_money_row(row, _PENDING_MONEY)


def set_status(item_id, status, wave_transaction_id=None, business_id=None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE pending_items
            SET status = ?, wave_transaction_id = COALESCE(?, wave_transaction_id),
                business_id = COALESCE(?, business_id)
            WHERE id = ?
            """,
            (status, wave_transaction_id, business_id, item_id),
        )


def set_category(item_id, category_name, direction=None):
    """Record the category (and, when it was reclassified at approval
    time, the direction) actually recorded, so Summary reflects what was
    really booked, not the original suggestion."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE pending_items SET suggested_category = ?, "
            "direction = COALESCE(?, direction) WHERE id = ?",
            (category_name, direction, item_id),
        )


def set_category_suggestion(item_id, category_name, confidence=None,
                            source=None):
    """Store an auto-categorization result without approving the item."""
    category_name = _category_storage_value(category_name)
    with get_connection() as conn:
        conn.execute(
            "UPDATE pending_items SET suggested_category = ?, "
            "category_confidence = ?, category_source = ? WHERE id = ?",
            (category_name, confidence, source, item_id),
        )


def record_category_correction(vendor_pattern, corrected_category,
                               last_seen=None):
    """Count a user correction so it outranks future suggestions."""
    from datetime import datetime, timezone
    timestamp = last_seen or datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO category_corrections "
            "(vendor_pattern, corrected_category, count, last_seen) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(vendor_pattern, corrected_category) DO UPDATE SET "
            "count = category_corrections.count + 1, last_seen = excluded.last_seen",
            (vendor_pattern, corrected_category, timestamp),
        )


def list_category_corrections():
    """Return learned corrections, strongest and newest first."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM category_corrections "
            "ORDER BY count DESC, last_seen DESC"
        ).fetchall()]


def set_notes(item_id, notes):
    """Store the optional free-text note captured at approval time.
    A blank note is stored as NULL."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE pending_items SET notes = ? WHERE id = ?",
            (notes or None, item_id),
        )


def set_evidence_type(item_id, evidence_type):
    """Store the optional evidence-on-file tag. A blank/unrecognized
    value is stored as NULL — validation of the allowed set lives in
    accountant_export.EVIDENCE_TYPES, not here."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE pending_items SET evidence_type = ? WHERE id = ?",
            (evidence_type or None, item_id),
        )


# Wave's two default catch-all category names — mirror
# check_bank.FALLBACK_EXPENSE_CATEGORY / FALLBACK_INCOME_CATEGORY (kept
# literal here to avoid a circular import; check_bank imports db).
UNCATEGORIZED_CATEGORIES = ("Uncategorized Expense", "Uncategorized Income")


def list_uncategorized_approved():
    """Approved items still sitting in a Wave catch-all category — what
    the 30-day cleanup nudge points at."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_items "
            "WHERE status = 'approved' AND suggested_category IN (?, ?) "
            "ORDER BY date DESC, id DESC",
            UNCATEGORIZED_CATEGORIES,
        ).fetchall()
        return [_public_money_row(row, _PENDING_MONEY) for row in rows]


def count_uncategorized_approved():
    with get_connection() as conn:
        return conn.execute(
            "SELECT count(*) AS c FROM pending_items "
            "WHERE status = 'approved' AND suggested_category IN (?, ?)",
            UNCATEGORIZED_CATEGORIES,
        ).fetchone()["c"]


def get_vendor_memory(direction=None, business_id=None):
    """Learned vendor → category associations, optionally scoped to one
    money direction and one business (category names belong to a
    single business's chart of accounts)."""
    query = "SELECT * FROM vendor_memory WHERE 1=1"
    params = []
    if direction is not None:
        query += " AND direction = ?"
        params.append(direction)
    if business_id is not None:
        query += " AND business_id = ?"
        params.append(business_id)
    query += " ORDER BY vendor_name"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def upsert_vendor_memory(vendor_name, category_id, category_name,
                         direction="expense", business_id=""):
    """
    Record a confirmed vendor → category pairing for one business.

    Same category as already stored → increment times_confirmed.
    Different category → most recent correction wins: overwrite and
    reset times_confirmed to 1.
    """
    business_id = business_id or ""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT category_name FROM vendor_memory "
            "WHERE vendor_name = ? AND business_id = ?",
            (vendor_name, business_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO vendor_memory "
                "(vendor_name, business_id, category_id, category_name, "
                "direction, times_confirmed) VALUES (?, ?, ?, ?, ?, 1)",
                (vendor_name, business_id, category_id, category_name,
                 direction),
            )
        elif row["category_name"] == category_name:
            conn.execute(
                "UPDATE vendor_memory SET times_confirmed = times_confirmed + 1, "
                "category_id = ?, direction = ? "
                "WHERE vendor_name = ? AND business_id = ?",
                (category_id, direction, vendor_name, business_id),
            )
        else:
            conn.execute(
                "UPDATE vendor_memory SET category_id = ?, category_name = ?, "
                "direction = ?, times_confirmed = 1 "
                "WHERE vendor_name = ? AND business_id = ?",
                (category_id, category_name, direction, vendor_name,
                 business_id),
            )


def insert_quote_followup(klaviyo_profile_id, name, email, program,
                          roster_size, sport_goal, submitted_at,
                          draft_subject, draft_body,
                          gmail_thread_checked_at, status="pending_review"):
    """Insert a quote follow-up. Returns its row id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO quote_followups
                (klaviyo_profile_id, name, email, program, roster_size,
                 sport_goal, submitted_at, status, draft_subject,
                 draft_body, gmail_thread_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (klaviyo_profile_id, name, email, program, roster_size,
             sport_goal, submitted_at, status, draft_subject,
             draft_body, gmail_thread_checked_at),
        )
        return cursor.lastrowid


def list_quote_followups(status=None):
    query = "SELECT * FROM quote_followups"
    params = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY submitted_at ASC"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_quote_followup(followup_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM quote_followups WHERE id = ?", (followup_id,)
        ).fetchone()
        return dict(row) if row else None


def quote_followup_exists(klaviyo_profile_id=None, email=None):
    """True if this lead is already tracked (by profile id or email)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM quote_followups "
            "WHERE (klaviyo_profile_id IS NOT NULL AND klaviyo_profile_id = ?) "
            "   OR (email = ?) LIMIT 1",
            (klaviyo_profile_id, email),
        ).fetchone()
        return row is not None


def set_quote_followup_status(followup_id, status, draft_subject=None,
                              draft_body=None):
    """Update status, optionally persisting the (possibly edited) draft."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE quote_followups
            SET status = ?,
                draft_subject = COALESCE(?, draft_subject),
                draft_body = COALESCE(?, draft_body)
            WHERE id = ?
            """,
            (status, draft_subject, draft_body, followup_id),
        )


def insert_invoice_reminder(wave_invoice_id, invoice_number, customer_name,
                            customer_email, amount_due, due_date,
                            draft_subject, draft_body,
                            status="pending_review", business_id=None):
    """Insert an invoice reminder draft. Returns its row id."""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO invoice_reminders
                (wave_invoice_id, invoice_number, customer_name,
                 customer_email, amount_due, due_date, status,
                 draft_subject, draft_body, business_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wave_invoice_id, invoice_number, customer_name, customer_email,
             amount_due, due_date, status, draft_subject, draft_body,
             business_id),
        )
        return cursor.lastrowid


def list_invoice_reminders(status=None):
    query = "SELECT * FROM invoice_reminders"
    params = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY due_date ASC, id ASC"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_invoice_reminder(reminder_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM invoice_reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        return dict(row) if row else None


def latest_invoice_reminder(wave_invoice_id):
    """Most recent reminder row for an invoice, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM invoice_reminders WHERE wave_invoice_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (wave_invoice_id,),
        ).fetchone()
        return dict(row) if row else None


def set_invoice_reminder_status(reminder_id, status, draft_subject=None,
                                draft_body=None, last_sent_at=None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE invoice_reminders
            SET status = ?,
                draft_subject = COALESCE(?, draft_subject),
                draft_body = COALESCE(?, draft_body),
                last_sent_at = COALESCE(?, last_sent_at)
            WHERE id = ?
            """,
            (status, draft_subject, draft_body, last_sent_at, reminder_id),
        )


def mark_invoice_reminders_paid(still_overdue_ids, business_id=None):
    """
    Any tracked invoice no longer in Wave's OVERDUE list has been paid
    or otherwise resolved; flip its non-paid rows to "paid". Scoped to
    one business when given — the overdue list came from ONE business,
    so another business's reminders must not be swept up. Returns the
    number of rows updated.
    """
    placeholders = ",".join("?" for _ in still_overdue_ids) or "''"
    query = f"""
        UPDATE invoice_reminders
        SET status = 'paid'
        WHERE status != 'paid'
          AND wave_invoice_id NOT IN ({placeholders})
    """
    params = list(still_overdue_ids)
    if business_id is not None:
        query += " AND (business_id = ? OR business_id IS NULL)"
        params.append(business_id)
    with get_connection() as conn:
        cursor = conn.execute(query, params)
        return cursor.rowcount


def save_ga4_snapshot(date, snapshot_json):
    """Store (or replace) the day's GA4 snapshot."""
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO ga4_snapshots (date, snapshot_json, created_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET snapshot_json = excluded.snapshot_json, "
            "created_at = excluded.created_at",
            (date, snapshot_json, datetime.now(timezone.utc).isoformat()),
        )


def get_latest_ga4_snapshot():
    """The most recent snapshot row, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ga4_snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def list_ga4_snapshots(limit=60):
    """Stored daily snapshots, oldest first — the history the Insights
    traffic sparkline plots. Read-only; the snapshots are written by the
    daily check exactly as before."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM ga4_snapshots ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]


def upsert_credential(name, encrypted_value, is_file, updated_at):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO credentials (name, encrypted_value, is_file, "
            "updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "encrypted_value = excluded.encrypted_value, "
            "is_file = excluded.is_file, updated_at = excluded.updated_at",
            (name, encrypted_value, int(is_file), updated_at),
        )


def list_credentials():
    """Names and metadata only — never decrypted values."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT name, is_file, updated_at FROM credentials ORDER BY name"
        ).fetchall()]


def get_credential(name):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM credentials WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def delete_credential(name):
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM credentials WHERE name = ?", (name,)
        )
        return cursor.rowcount


def insert_reconcile_snapshot(date, wave_balance, bank_balance, residual,
                              flagged):
    """One reconciliation snapshot per day; a same-day re-check
    overwrites (the page and the daily runner can both write — the
    freshest numbers win)."""
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO reconciliation_history "
            "(date, wave_balance_cents, bank_balance_cents, residual_cents, flagged, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "wave_balance_cents = excluded.wave_balance_cents, "
            "bank_balance_cents = excluded.bank_balance_cents, "
            "residual_cents = excluded.residual_cents, "
            "flagged = excluded.flagged, "
            "created_at = excluded.created_at",
            (date, _money_to_cents(wave_balance, "wave balance"),
             _money_to_cents(bank_balance, "bank balance"),
             _money_to_cents(residual, "reconciliation residual"),
             1 if flagged else 0,
             datetime.now(timezone.utc).isoformat()),
        )


def list_reconcile_history(days=30):
    """Snapshots within the trailing window, oldest first."""
    from datetime import date as date_cls, timedelta
    cutoff = (date_cls.today() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT date, wave_balance_cents, bank_balance_cents, "
            "residual_cents, flagged "
            "FROM reconciliation_history WHERE date >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()
        return [_public_money_row(row, _RECONCILIATION_MONEY) for row in rows]


# --------------------------------------------------------- monthly closes


def get_monthly_close(period):
    """The close record for one YYYY-MM period, or None if it's never
    been closed."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM monthly_closes WHERE period = ?", (period,)
        ).fetchone()
        return dict(row) if row else None


def record_monthly_close(period, pdf_generated=False, csv_generated=False,
                         notes=None):
    """Record (or update) that a period's close package was generated.
    A re-close of the same period ORs the generated flags forward
    (downloading the PDF today and the CSV tomorrow both count toward
    the same close) and overwrites notes/timestamp with the latest —
    same "freshest check wins" convention as reconciliation_history."""
    from datetime import datetime, timezone
    existing = get_monthly_close(period)
    pdf_flag = pdf_generated or bool(existing and existing["pdf_generated"])
    csv_flag = csv_generated or bool(existing and existing["csv_generated"])
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO monthly_closes "
            "(period, closed_at, pdf_generated, csv_generated, notes) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(period) DO UPDATE SET "
            "closed_at = excluded.closed_at, "
            "pdf_generated = excluded.pdf_generated, "
            "csv_generated = excluded.csv_generated, "
            "notes = COALESCE(excluded.notes, monthly_closes.notes)",
            (period, datetime.now(timezone.utc).isoformat(),
             int(pdf_flag), int(csv_flag), notes),
        )


def list_monthly_closes():
    """Every close record, most recent period first."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM monthly_closes ORDER BY period DESC"
        ).fetchall()]


def get_setting(key):
    """A user-configurable option (settings table), or None if unset."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def delete_setting(key):
    """Remove one obsolete setting. Returns True when a row existed."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return cursor.rowcount > 0


def get_insight_widget_config(profile_id):
    """The saved ordered Home-widget metric IDs for one local profile."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM insight_widget_config WHERE profile_id = ?",
            (str(profile_id),),
        ).fetchone()
    return dict(row) if row else None


def upsert_insight_widget_config(profile_id, metric_ids_json, changed_at):
    """Atomically replace one profile's widget selection and order."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO insight_widget_config "
            "(profile_id, metric_ids_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET "
            "metric_ids_json = excluded.metric_ids_json, "
            "updated_at = excluded.updated_at",
            (str(profile_id), metric_ids_json, changed_at, changed_at),
        )


def set_agent_memory(key, value):
    """Upsert one AI-assistant memory (the model's own notes plus the
    daily insight). Values are plain text, aggregates/observations
    only — the write path is a capped tool, never raw data."""
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO agent_memory (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )


def get_agent_memory(key):
    """{'key', 'value', 'updated_at'} or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT key, value, updated_at FROM agent_memory "
            "WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def list_agent_memory():
    """All memories, most recently updated first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM agent_memory "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def delete_agent_memory(key):
    with get_connection() as conn:
        conn.execute("DELETE FROM agent_memory WHERE key = ?", (key,))


def mark_connection_tested(service, ok=True, detail=""):
    """Stamp when a connection check last ran for one service (wave,
    bank, klaviyo, ga4, email) — read by the Connections page's
    "Last tested" line. Local time, minute precision. Also records the
    outcome so a failed test stays visible on the card across page
    loads, not just in the one-shot banner."""
    from datetime import datetime
    set_setting(f"conn_last_tested_{service}",
                datetime.now().isoformat(timespec="minutes"))
    set_setting(f"conn_last_result_{service}",
                "ok" if ok else f"fail:{detail or 'The last check failed.'}")


def get_connection_tested(service):
    """The last-tested stamp for one service, or None if never run."""
    return get_setting(f"conn_last_tested_{service}")


def get_connection_result(service):
    """Outcome of the last connection check: None (never recorded — old
    installs' stamps predate outcome tracking and read as ok), "ok", or
    the failure reason string."""
    raw = get_setting(f"conn_last_result_{service}")
    if not raw:
        return None
    if raw == "ok":
        return "ok"
    return raw[5:] if raw.startswith("fail:") else raw


def _upsert(table, key, row):
    columns = list(row)
    assignments = ", ".join(f"{column}=excluded.{column}" for column in columns if column != key)
    placeholders = ", ".join("?" for _ in columns)
    with get_connection() as conn:
        conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                     f"ON CONFLICT({key}) DO UPDATE SET {assignments}",
                     [row[column] for column in columns])


def upsert_shopify_order(row):
    _upsert("shopify_orders", "order_id",
            _storage_money_row(row, _SHOPIFY_ORDER_MONEY))


def upsert_bank_transaction(row):
    """Upsert one canonical bank row (negative out, positive in)."""
    columns = (
        "source", "transaction_id", "account_id", "date", "authorized_date",
        "amount_cents", "merchant_name", "name", "pending",
        "category_primary", "category_detailed", "removed", "synced_at",
    )
    values = {column: row.get(column) for column in columns}
    values["source"] = str(values["source"] or "").strip()
    values["transaction_id"] = str(values["transaction_id"] or "").strip()
    values["account_id"] = str(values["account_id"] or "").strip()
    if not values["source"] or not values["transaction_id"]:
        raise ValueError("Bank source and transaction ID are required.")
    updates = ", ".join(
        f"{column}=excluded.{column}" for column in columns
        if column not in {"source", "account_id", "transaction_id"})
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO bank_transactions "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(source, account_id, transaction_id) DO UPDATE SET "
            + updates,
            [values[column] for column in columns],
        )


def upsert_plaid_transaction(row):
    """Compatibility entry point for already-canonical Plaid rows."""
    upsert_bank_transaction({"source": "plaid", **row})


def upsert_bank_account(row):
    """Cache source-scoped account metadata without replacing its mapping."""
    columns = ("source", "account_id", "name", "official_name", "type",
               "subtype", "mask", "synced_at")
    source = str(row.get("source") or "").strip()
    account_id = str(row.get("account_id") or "").strip()
    if not source or not account_id:
        raise ValueError("Bank source and account ID are required.")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO bank_accounts "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(source, account_id) DO UPDATE SET "
            "name=excluded.name, official_name=excluded.official_name, "
            "type=CASE WHEN excluded.type != '' THEN excluded.type "
            "ELSE bank_accounts.type END, "
            "subtype=CASE WHEN excluded.subtype != '' THEN excluded.subtype "
            "ELSE bank_accounts.subtype END, "
            "mask=COALESCE(excluded.mask, bank_accounts.mask), "
            "synced_at=excluded.synced_at",
            [source if column == "source" else account_id if column == "account_id"
             else row.get(column) for column in columns],
        )


def upsert_plaid_account(row):
    """Compatibility entry point for Plaid account metadata."""
    upsert_bank_account({"source": "plaid", **row})


def list_bank_accounts(source):
    """Every bank_accounts row for one source, all columns, stable order."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM bank_accounts WHERE source = ? "
            "ORDER BY name COLLATE NOCASE, account_id",
            (str(source or ""),),
        ).fetchall()]


def taken_ledger_account_ids():
    """Every Ledger account currently claimed by a bank-account mapping.

    Returns ``{ledger_account_id: (source, account_id)}`` across every
    source (not just one), so a caller such as Task 4's mapping screen can
    grey out an already-claimed Ledger account and say which bank account
    claimed it, instead of discovering the partial-unique-index constraint
    by catching an exception from ``set_bank_ledger_account``.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ledger_account_id, source, account_id FROM bank_accounts "
            "WHERE ledger_account_id IS NOT NULL"
        ).fetchall()
    return {row["ledger_account_id"]: (row["source"], row["account_id"])
            for row in rows}


def bank_ledger_account_id(source, account_id):
    """Return the explicitly mapped Ledger account, if available."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT ledger_account_id FROM bank_accounts "
            "WHERE source = ? AND account_id = ?",
            (str(source or ""), str(account_id or "")),
        ).fetchone()
    return row["ledger_account_id"] if row and row["ledger_account_id"] else None


def plaid_ledger_account_id(account_id):
    return bank_ledger_account_id("plaid", account_id)


def ensure_bank_ledger_account(source, account_id):
    """Return or safely create a source account's Ledger mapping.

    Only Plaid supplies a reliable asset/liability type, preserving its
    existing automatic behavior. Other sources remain unmapped until a person
    chooses a Ledger account; callers must fail rather than guess Checking.

    A row a person explicitly excluded (``mapping_state == 'excluded'``,
    set by ``exclude_bank_account``) must stay excluded here too, even
    though this function's own auto-mapping path only ever applies to
    Plaid: this is the one place a Plaid account's mapping gets created
    automatically, and without this check an excluded Plaid account would
    silently resume posting on its next sync while the mapping screen
    still showed it as excluded. Checked before the Plaid branch below so
    it can never be bypassed by a future addition to that branch.
    """
    source = str(source or "").strip()
    account_id = str(account_id or "")
    if not source or not account_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM bank_accounts WHERE source = ? AND account_id = ?",
            (source, account_id),
        ).fetchone()
        if not row:
            return None
        if row["mapping_state"] == "excluded":
            return None
        if row["ledger_account_id"]:
            return row["ledger_account_id"]
        if source != "plaid":
            return None
        is_liability = row["type"] in {"credit", "loan"}
        parent_code = "2100" if is_liability else "1000"
        parent = conn.execute(
            "SELECT id FROM accounts WHERE code = ?", (parent_code,)
        ).fetchone()
        if not parent:
            return None
        label = row["official_name"] or row["name"] or row["subtype"] or "Account"
        if row["mask"]:
            label = f"{label} ••••{row['mask']}"
        code = f"bank:{source}:{account_id}"
        cursor = conn.execute(
            "INSERT OR IGNORE INTO accounts (code, name, type, parent_account_id) "
            "VALUES (?, ?, ?, ?)",
            (code, f"Plaid {label}", "liability" if is_liability else "asset",
             parent["id"]),
        )
        account = conn.execute(
            "SELECT id FROM accounts WHERE code = ?", (code,)
        ).fetchone()
        if not account:
            return None
        conn.execute(
            "UPDATE bank_accounts SET ledger_account_id = ? "
            "WHERE source = ? AND account_id = ?",
            (account["id"], source, account_id),
        )
        return account["id"]


def ensure_plaid_ledger_account(account_id):
    return ensure_bank_ledger_account("plaid", account_id)


def set_bank_ledger_account(source, account_id, ledger_account_id):
    """Persist a person's explicit source-account to Ledger mapping.

    A real mapping supersedes any prior exclusion, so this also resets
    ``mapping_state`` back to ``''``. Refuses rather than raising when
    ``ledger_account_id`` is already claimed by a different (source,
    account_id): the partial unique index on
    ``bank_accounts.ledger_account_id`` would otherwise let a raw
    ``sqlite3.IntegrityError`` escape this call. That is caught here and
    turned into a plain ``False``, matching the bool-return convention
    this function and ``exclude_bank_account`` already use. A caller that
    needs to know which other account already claimed a target (e.g. to
    explain the refusal) should check ``taken_ledger_account_ids()``.
    """
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                "UPDATE bank_accounts SET ledger_account_id = ?, "
                "mapping_state = '' WHERE source = ? AND account_id = ?",
                (ledger_account_id, str(source or ""), str(account_id or "")),
            )
        except sqlite3.IntegrityError:
            return False
    return cursor.rowcount > 0


def clear_bank_ledger_account(source, account_id):
    """Release one row's ``ledger_account_id`` to NULL WITHOUT touching
    ``mapping_state`` -- a neutral "mechanically let go of this Ledger
    account" primitive, unlike ``exclude_bank_account`` (which also
    records ``mapping_state = 'excluded'``, a specific claim about a
    person's INTENT). Use this when code needs to free up a claimed
    Ledger account for bookkeeping reasons that have nothing to do with
    what the person actually chose -- e.g. Screen 3's intra-batch mapping
    swap, or releasing a code still held by a bank account that has since
    stopped being returned by the source at all. ``mapping_state`` is
    left exactly as it was (``''`` stays ``''``, and this is never called
    on an already-``'excluded'`` row in practice since those never carry
    a ``ledger_account_id`` to begin with).

    Reserve ``exclude_bank_account`` for the actual "a person chose
    Exclude" case: using it as a substitute "clear" primitive would leave
    a row that crashes or errors out between this call and a caller's
    follow-up write mis-recorded as an explicit exclusion the person
    never chose, instead of honestly reading as unanswered.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE bank_accounts SET ledger_account_id = NULL "
            "WHERE source = ? AND account_id = ?",
            (str(source or ""), str(account_id or "")),
        )
    return cursor.rowcount > 0


def exclude_bank_account(source, account_id):
    """Record that a person explicitly chose not to import this account.

    ``exclude_bank_account`` and ``set_bank_ledger_account`` keep
    ``mapping_state`` and ``ledger_account_id`` consistent with EACH
    OTHER: excluding here clears any existing mapping, and mapping a real
    Ledger account resets ``mapping_state`` back to ``''``. An exclusion
    also sticks against ``ensure_bank_ledger_account``'s Plaid
    auto-mapping path: that function now returns ``None`` early for any
    row with ``mapping_state == 'excluded'``, before its Plaid branch runs,
    so a Plaid account excluded here cannot be silently re-mapped and
    resume posting on its next sync.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE bank_accounts SET mapping_state = 'excluded', "
            "ledger_account_id = NULL WHERE source = ? AND account_id = ?",
            (str(source or ""), str(account_id or "")),
        )
    return cursor.rowcount > 0


def mark_plaid_transaction_removed(transaction_id, synced_at):
    with get_connection() as conn:
        conn.execute(
            "UPDATE bank_transactions SET removed = 1, synced_at = ? "
            "WHERE source = 'plaid' AND transaction_id = ?",
            (synced_at, transaction_id),
        )


def list_plaid_transactions_for_ledger():
    """Settled Plaid records, retained as a Step-1 compatibility name."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM bank_transactions "
            "WHERE source = 'plaid' AND pending = 0 AND removed = 0 "
            "ORDER BY date, account_id, transaction_id"
        ).fetchall()]


def list_bank_transactions_for_ledger(source=None):
    """Settled, non-removed canonical rows in a stable union order."""
    query = ("SELECT * FROM bank_transactions "
             "WHERE pending = 0 AND removed = 0")
    args = []
    if source:
        query += " AND source = ?"
        args.append(source)
    query += " ORDER BY date, source, account_id, transaction_id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def count_pending_plaid_transactions():
    """Count cached Plaid transactions that have not settled or disappeared."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM bank_transactions "
            "WHERE source = 'plaid' AND pending = 1 AND removed = 0"
        ).fetchone()
    return int(row["count"] or 0)


def record_shopify_variant_cost(row):
    """Store an immutable Shopify unit-cost availability snapshot."""
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    with get_connection() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO shopify_variant_costs "
            f"({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )


def get_shopify_variant_cost_at(variant_id, order_created_at):
    """Latest cost snapshot captured no later than an order's timestamp."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM shopify_variant_costs "
            "WHERE variant_id = ? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (str(variant_id), order_created_at),
        ).fetchone()
    return dict(row) if row else None


def log_ledger_source_import_skip(source_reference, source, reason, detail):
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO ledger_source_import_skips "
            "(source_reference, source, reason, detail, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source_reference) DO UPDATE SET "
            "source=excluded.source, reason=excluded.reason, detail=excluded.detail, "
            "updated_at=excluded.updated_at",
            (source_reference, source, reason, detail,
             datetime.now(timezone.utc).isoformat()),
        )


def clear_ledger_source_import_skip(source_reference):
    with get_connection() as conn:
        conn.execute("DELETE FROM ledger_source_import_skips WHERE source_reference = ?",
                     (source_reference,))


def upsert_shopify_payout(row):
    _upsert("shopify_payouts", "payout_id", row)


def list_shopify_payouts():
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM shopify_payouts ORDER BY date, payout_id"
        ).fetchall()]


def ledger_source_import_exists(source_reference):
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM ledger_source_imports WHERE source_reference = ?",
            (source_reference,),
        ).fetchone() is not None


def source_import_account_codes(source_reference):
    """Return the chart codes used by an already-imported source entry."""
    with get_connection() as conn:
        return {row["code"] for row in conn.execute(
            "SELECT a.code FROM ledger_source_imports si "
            "JOIN journal_lines jl ON jl.journal_entry_id = si.journal_entry_id "
            "JOIN accounts a ON a.id = jl.account_id "
            "WHERE si.source_reference = ?", (source_reference,)
        ).fetchall()}


# The source_reference prefixes written by ledger_sync. Grouping on them here
# keeps the Ledger screen's standing counts in one SQL pass; the COGS test
# runs first because a COGS reference is also an order reference.
_LEDGER_BUCKETS = (
    ("shopify_cogs", "source_reference LIKE '%:cogs'"),
    ("plaid", "source_reference LIKE 'plaid:transaction:%'"),
    ("shopify_orders", "source_reference LIKE 'shopify:order:%' "
                       "AND source_reference NOT LIKE '%:cogs'"),
    ("shopify_payouts", "source_reference LIKE 'shopify:payout:%'"),
    ("invoices", "source_reference LIKE 'invoice:%'"),
    ("deposits", "source_reference LIKE 'deposit:%'"),
)


def ledger_import_counts():
    """Posted-entry counts per source, plus the newest import timestamp.

    These are standing totals, not last-run deltas: the Ledger screen can be
    reloaded independently of a sync, so it reports what is in the ledger now
    rather than what one run happened to create.
    """
    columns = ", ".join(f"SUM({test}) AS {name}" for name, test in _LEDGER_BUCKETS)
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {columns}, COUNT(*) AS total, "
            "MAX(imported_at) AS last_imported_at FROM ledger_source_imports"
        ).fetchone()
    counts = {name: (row[name] or 0) for name, _ in _LEDGER_BUCKETS}
    counts["total"] = row["total"] or 0
    counts["last_imported_at"] = row["last_imported_at"]
    return counts


def list_ledger_source_skips():
    """Source records ledger_sync deliberately did not post, newest first."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT source_reference, source, reason, detail, updated_at "
            "FROM ledger_source_import_skips "
            "ORDER BY updated_at DESC, source_reference"
        ).fetchall()]


def ledger_skip_counts():
    """Recorded skips per *synced* source bucket.

    These keys are a subset of ledger_import_counts': only synced sources
    can skip. Invoicing and deposits post because a human clicked send, so
    they either post or raise -- there is nothing to decline and log, and a
    zeroed key here would imply a concept that does not exist for them.
    """
    counts = {"shopify_cogs": 0, "shopify_payouts": 0, "plaid": 0,
              "shopify_orders": 0}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT source_reference FROM ledger_source_import_skips"
        ).fetchall()
    for row in rows:
        reference = row["source_reference"] or ""
        if reference.endswith(":cogs"):
            counts["shopify_cogs"] += 1
        elif reference.startswith("shopify:payout:"):
            counts["shopify_payouts"] += 1
        elif reference.startswith("plaid:transaction:"):
            counts["plaid"] += 1
        elif reference.startswith("shopify:order:"):
            counts["shopify_orders"] += 1
    return counts


def ledger_source_freshness():
    """When each local source table was last refreshed from its API.

    Distinct from ledger_import_counts()'s last_imported_at: source data can
    be pulled without being posted, and posted without being re-pulled.
    """
    with get_connection() as conn:
        plaid = conn.execute(
            "SELECT MAX(synced_at) AS at FROM bank_transactions "
            "WHERE source = 'plaid'").fetchone()["at"]
        shopify = conn.execute(
            "SELECT MAX(synced_at) AS at FROM shopify_payouts").fetchone()["at"]
    return {"plaid": plaid, "shopify": shopify}


def upsert_shopify_product(row):
    _upsert("shopify_products", "product_id", row)


def upsert_shopify_customer(row):
    _upsert("shopify_customers", "customer_id",
            _storage_money_row(row, _SHOPIFY_CUSTOMER_MONEY))


def list_shopify_orders(start=None, end=None):
    query, args = "SELECT * FROM shopify_orders", []
    terms = []
    if start:
        terms.append("created_at >= ?"); args.append(start)
    if end:
        terms.append("created_at < ?"); args.append(end)
    if terms:
        query += " WHERE " + " AND ".join(terms)
    query += " ORDER BY created_at DESC"
    with get_connection() as conn:
        return [_public_money_row(row, _SHOPIFY_ORDER_MONEY)
                for row in conn.execute(query, args).fetchall()]


def get_shopify_order(order_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM shopify_orders WHERE order_id = ?", (order_id,)).fetchone()
        return _public_money_row(row, _SHOPIFY_ORDER_MONEY)


def get_shopify_product(product_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM shopify_products WHERE product_id = ?", (product_id,)).fetchone()
        return dict(row) if row else None


def list_shopify_customers():
    with get_connection() as conn:
        return [_public_money_row(row, _SHOPIFY_CUSTOMER_MONEY)
                for row in conn.execute(
                    "SELECT * FROM shopify_customers "
                    "ORDER BY total_spent_cents DESC").fetchall()]


def get_all_settings():
    with get_connection() as conn:
        return {row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM settings")}


def get_meta(key):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_meta(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------- contacts
# The shared party record behind Invoicing/AR and the CRM. See
# migrations/add_contacts.sql for why it exists.


def _normalize_alias(text):
    """Aliases are stored pre-lowered and trimmed so lookups can hit the
    index directly instead of wrapping the column in lower()."""
    return (text or "").strip().lower()


def list_contacts():
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM contacts ORDER BY display_name COLLATE NOCASE"
        ).fetchall()]


def get_contact(contact_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        return dict(row) if row else None


def get_contact_by_shopify_id(shopify_customer_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE shopify_customer_id = ?",
            (str(shopify_customer_id),)).fetchone()
        return dict(row) if row else None


def get_contact_by_email(email):
    """Case-insensitive, because the same person reaches us as
    Ben@example.com from one source and ben@example.com from another."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE lower(email) = ?",
            (normalized,)).fetchone()
        return dict(row) if row else None


def create_contact(display_name, email=None, phone=None, notes="",
                   shopify_customer_id=None, stripe_customer_id=None):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO contacts (display_name, email, phone, "
            "shopify_customer_id, stripe_customer_id, notes, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (display_name, email, phone,
             str(shopify_customer_id) if shopify_customer_id else None,
             stripe_customer_id, notes or "", now, now),
        )
        return cursor.lastrowid


def update_contact(contact_id, **fields):
    """Patch only the columns passed. Mirrors the partial-update spirit of
    set_quote_followup_status: an omitted field is left alone, never
    blanked."""
    allowed = ("display_name", "email", "phone", "notes",
               "shopify_customer_id", "stripe_customer_id")
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    from datetime import datetime, timezone
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with get_connection() as conn:
        conn.execute(f"UPDATE contacts SET {assignments} WHERE id = ?",
                     (*updates.values(), contact_id))


def add_contact_alias(contact_id, alias_text, source):
    """Record a string this contact is known by elsewhere. Ignores blanks
    and duplicates so callers can fire it opportunistically."""
    normalized = _normalize_alias(alias_text)
    if not normalized:
        return
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO contact_aliases "
            "(contact_id, alias_text, source, created_at) VALUES (?, ?, ?, ?)",
            (contact_id, normalized, source,
             datetime.now(timezone.utc).isoformat()),
        )


def find_contacts_by_alias(alias_text):
    """Every contact known by this exact string. Returns a list, not one
    row: two contacts can legitimately share a merchant descriptor, and
    the caller must decide rather than have this guess for it."""
    normalized = _normalize_alias(alias_text)
    if not normalized:
        return []
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT c.* FROM contacts c "
            "JOIN contact_aliases a ON a.contact_id = c.id "
            "WHERE a.alias_text = ? ORDER BY c.display_name COLLATE NOCASE",
            (normalized,)).fetchall()]


def list_contact_aliases(contact_id):
    """Every string this contact is known by elsewhere, for display.

    The inverse of find_contacts_by_alias: that answers "who is this
    string?", this answers "what is this contact called?".
    """
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT alias_text, source FROM contact_aliases "
            "WHERE contact_id = ? ORDER BY source, alias_text",
            (contact_id,)).fetchall()]


def backfill_contacts_from_shopify():
    """Seed contacts from the Shopify customer sync that already runs.

    Idempotent and safe to re-run: a customer already linked by
    shopify_customer_id is refreshed, an unlinked contact matching on
    email is adopted (so a hand-created contact and its Shopify record
    converge instead of duplicating), and anything else is created.

    Returns {"created": n, "linked": n, "updated": n}.
    """
    counts = {"created": 0, "linked": 0, "updated": 0}
    for customer in list_shopify_customers():
        shopify_id = str(customer["customer_id"])
        email = (customer.get("email") or "").strip() or None
        name = " ".join(part for part in (customer.get("first_name"),
                                          customer.get("last_name")) if part)
        display_name = name.strip() or email or f"Shopify customer {shopify_id}"

        existing = get_contact_by_shopify_id(shopify_id)
        if existing:
            if (existing["display_name"] != display_name
                    or existing["email"] != email):
                update_contact(existing["id"], display_name=display_name,
                               email=email)
                counts["updated"] += 1
            contact_id = existing["id"]
        else:
            by_email = get_contact_by_email(email) if email else None
            if by_email and not by_email["shopify_customer_id"]:
                update_contact(by_email["id"], shopify_customer_id=shopify_id,
                               display_name=display_name)
                contact_id = by_email["id"]
                counts["linked"] += 1
            else:
                contact_id = create_contact(display_name, email=email,
                                            shopify_customer_id=shopify_id)
                counts["created"] += 1

        if name.strip():
            add_contact_alias(contact_id, name, "shopify")
    return counts


# -------------------------------------------------------- contact pipeline
# Stage is a judgment somebody made, stored separately from contacts. See
# migrations/add_contact_pipeline.sql for why it is its own table and why
# an absent row means "no judgment yet" rather than a default stage.


CONTACT_STAGES = ("lead", "customer", "key_account", "dormant")


def get_contact_stage(contact_id):
    """This contact's stage, or None if nobody has assigned one."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT stage FROM contact_pipeline WHERE contact_id = ?",
            (contact_id,)).fetchone()
        return row["stage"] if row else None


def contact_stages():
    """Every assigned stage as {contact_id: stage}, for list views that
    would otherwise fire one query per row."""
    with get_connection() as conn:
        return {row["contact_id"]: row["stage"] for row in conn.execute(
            "SELECT contact_id, stage FROM contact_pipeline").fetchall()}


def set_contact_stage(contact_id, stage):
    """Assign or move a stage. Rejects an unknown stage here rather than
    relying on the CHECK constraint alone, so a typo surfaces as a real
    error instead of a sqlite3.IntegrityError the caller has to decode."""
    if stage not in CONTACT_STAGES:
        raise ValueError(f"Unknown pipeline stage: {stage!r}")
    from datetime import datetime, timezone
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO contact_pipeline (contact_id, stage, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(contact_id) DO UPDATE SET "
            "stage = excluded.stage, updated_at = excluded.updated_at",
            (contact_id, stage, datetime.now(timezone.utc).isoformat()))


def clear_contact_stage(contact_id):
    """Return a contact to unassigned. Deleting the row rather than
    writing a sentinel keeps "no judgment" as the absence it is."""
    with get_connection() as conn:
        conn.execute("DELETE FROM contact_pipeline WHERE contact_id = ?",
                     (contact_id,))


def list_shopify_orders_for_customer(shopify_customer_id):
    """One Shopify customer's orders, newest first.

    Joins on the denormalized customer_id the order sync already stores,
    which is the same string contacts.shopify_customer_id holds.
    """
    if not shopify_customer_id:
        return []
    with get_connection() as conn:
        return [_public_money_row(row, _SHOPIFY_ORDER_MONEY)
                for row in conn.execute(
            "SELECT * FROM shopify_orders WHERE customer_id = ? "
            "ORDER BY created_at DESC",
            (str(shopify_customer_id),)).fetchall()]


def unattributed_shopify_order_count():
    """Orders carrying no Shopify customer id at all -- guest checkouts.

    Surfaced rather than hidden: these are real revenue that no contact
    record can legitimately claim, and a per-customer total that silently
    omitted them would look complete while being short.
    """
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM shopify_orders "
            "WHERE customer_id IS NULL OR customer_id = ''").fetchone()[0]


def posted_amounts_by_source_reference(references):
    """Per source reference, the net debit and credit against each account
    code, as {reference: {code: {"debit": cents, "credit": cents}}}.

    Reads what the ledger actually posted rather than recomputing it from
    the source rows. An order's revenue is netted of refunds at post time
    and COGS is withheld entirely unless every line could be costed, so
    the posted entry is the only figure that is true; deriving it a second
    time from shopify_orders would produce a number that looks like the
    ledger's and disagrees with it.
    """
    references = [ref for ref in references if ref]
    if not references:
        return {}
    totals = {}
    with get_connection() as conn:
        # Chunked because SQLite caps host parameters (999 by default) and
        # a long-standing store can easily exceed that in orders.
        for start in range(0, len(references), 500):
            chunk = references[start:start + 500]
            placeholders = ", ".join("?" * len(chunk))
            for row in conn.execute(
                "SELECT je.source_reference AS ref, a.code AS code, "
                "SUM(jl.debit_amount) AS debit, SUM(jl.credit_amount) AS credit "
                "FROM journal_entries je "
                "JOIN journal_lines jl ON jl.journal_entry_id = je.id "
                "JOIN accounts a ON a.id = jl.account_id "
                f"WHERE je.source_reference IN ({placeholders}) "
                "GROUP BY je.source_reference, a.code", chunk
            ).fetchall():
                totals.setdefault(row["ref"], {})[row["code"]] = {
                    "debit": row["debit"] or 0, "credit": row["credit"] or 0}
    return totals


# --------------------------------------------------------------- invoicing
# Storage only. Every amount is integer cents and every derived figure
# (line totals, tax, balance, status) is computed in invoice_ledger.py, so
# there is exactly one place that knows the arithmetic.


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def create_invoice(contact_id, issue_date, due_date, terms="", notes="",
                   currency="USD"):
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO invoices (contact_id, issue_date, due_date, terms, "
            "notes, currency, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (contact_id, issue_date, due_date, terms or "", notes or "",
             currency or "USD", now, now),
        )
        return cursor.lastrowid


def get_invoice(invoice_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?",
                           (invoice_id,)).fetchone()
        return dict(row) if row else None


def list_invoices(include_voided=True):
    clause = "" if include_voided else " WHERE voided_at IS NULL"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM invoices{clause} "
            "ORDER BY COALESCE(sent_at, created_at) DESC, id DESC").fetchall()]


def list_invoices_for_contact(contact_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM invoices WHERE contact_id = ? "
            "ORDER BY issue_date DESC, id DESC", (contact_id,)).fetchall()]


def update_invoice(invoice_id, **fields):
    """Patch only the columns passed; omitted fields are left alone.

    Deliberately allows the lifecycle stamps through so invoice_ledger can
    record send/void/write-off, but never invoice_number -- numbering is
    allocated by allocate_invoice_number() inside a transaction and must not
    be settable from arbitrary form input.
    """
    allowed = ("contact_id", "issue_date", "due_date", "terms", "notes",
               "currency", "sent_at", "voided_at", "written_off_at",
               "write_off_method", "posted_basis", "payment_link_url",
               "stripe_payment_link_id")
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    updates["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with get_connection() as conn:
        conn.execute(f"UPDATE invoices SET {assignments} WHERE id = ?",
                     (*updates.values(), invoice_id))


def allocate_invoice_number(invoice_id, prefix):
    """Assign the next number in sequence, once, at send time.

    Gapless by construction: the number is derived from the count of
    already-numbered invoices inside the same transaction that writes it, so
    a discarded draft never consumes one. Returns the existing number
    unchanged if this invoice already has one, which makes a re-send safe.
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT invoice_number FROM invoices WHERE id = ?",
            (invoice_id,)).fetchone()
        if existing and existing["invoice_number"]:
            return existing["invoice_number"]
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM invoices WHERE invoice_number IS NOT NULL"
        ).fetchone()["n"]
        number = f"{prefix}{used + 1:04d}"
        conn.execute("UPDATE invoices SET invoice_number = ?, updated_at = ? "
                     "WHERE id = ?", (number, _now_iso(), invoice_id))
        return number


def replace_invoice_lines(invoice_id, lines):
    """Rewrite an invoice's lines wholesale.

    Lines are only editable while a draft, so a full replace is simpler and
    less error-prone than diffing, and cannot leave orphaned rows.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM invoice_lines WHERE invoice_id = ?",
                     (invoice_id,))
        conn.executemany(
            "INSERT INTO invoice_lines (invoice_id, position, description, "
            "quantity_milli, unit_price_cents, tax_rate_bp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(invoice_id, index, line.get("description", ""),
              int(line.get("quantity_milli", 1000)),
              int(line.get("unit_price_cents", 0)),
              int(line.get("tax_rate_bp", 0)))
             for index, line in enumerate(lines)],
        )
        conn.execute("UPDATE invoices SET updated_at = ? WHERE id = ?",
                     (_now_iso(), invoice_id))


def list_invoice_lines(invoice_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? "
            "ORDER BY position, id", (invoice_id,)).fetchall()]


def record_invoice_payment(invoice_id, paid_on, amount_cents, fee_cents=0,
                           method="manual", reference=None):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO invoice_payments (invoice_id, paid_on, amount_cents, "
            "fee_cents, method, reference, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, paid_on, int(amount_cents), int(fee_cents), method,
             reference, _now_iso()),
        )
        return cursor.lastrowid


def list_invoice_payments(invoice_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM invoice_payments WHERE invoice_id = ? "
            "ORDER BY paid_on, id", (invoice_id,)).fetchall()]


def invoice_payment_reference_exists(reference):
    """Has this external payment already been recorded? Guards against the
    Stripe poll and the bank cross-match both applying the same money."""
    if not reference:
        return False
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM invoice_payments WHERE reference = ? LIMIT 1",
            (reference,)).fetchone() is not None


def record_invoice_credit_note(invoice_id, issued_on, amount_cents,
                               tax_cents=0, reason="", is_refund=False):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO invoice_credit_notes (invoice_id, issued_on, "
            "amount_cents, tax_cents, reason, is_refund, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, issued_on, int(amount_cents), int(tax_cents),
             reason or "", 1 if is_refund else 0, _now_iso()),
        )
        return cursor.lastrowid


def list_invoice_credit_notes(invoice_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM invoice_credit_notes WHERE invoice_id = ? "
            "ORDER BY issued_on, id", (invoice_id,)).fetchall()]


def record_customer_deposit(contact_id, received_on, amount_cents, note=""):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO customer_deposits (contact_id, received_on, "
            "amount_cents, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (contact_id, received_on, int(amount_cents), note or "",
             _now_iso()),
        )
        return cursor.lastrowid


def get_customer_deposit(deposit_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM customer_deposits WHERE id = ?",
                           (deposit_id,)).fetchone()
        return dict(row) if row else None


def list_unapplied_deposits(contact_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM customer_deposits WHERE contact_id = ? "
            "AND applied_invoice_id IS NULL ORDER BY received_on, id",
            (contact_id,)).fetchall()]


def apply_customer_deposit(deposit_id, invoice_id, applied_on):
    with get_connection() as conn:
        conn.execute(
            "UPDATE customer_deposits SET applied_invoice_id = ?, "
            "applied_on = ? WHERE id = ? AND applied_invoice_id IS NULL",
            (invoice_id, applied_on, deposit_id))


# ----------------------------------------------------- recurring invoices
# Storage only. The date arithmetic and the decision about which cycles
# are due live in recurring_invoices.py, so there is one place that knows
# what "monthly" means.


RECURRENCES = ("weekly", "monthly", "custom")
INSTANCE_STATUSES = ("pending_review", "approved_sent", "skipped")


def create_recurring_template(contact_id, recurrence, next_due_date,
                              name="", interval_days=None, anchor_day=None,
                              terms="", notes="", due_in_days=30):
    if recurrence not in RECURRENCES:
        raise ValueError(f"Unknown recurrence: {recurrence!r}")
    now = _now_iso()
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO recurring_invoice_templates (contact_id, name, "
            "recurrence, interval_days, anchor_day, next_due_date, active, "
            "terms, notes, due_in_days, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (contact_id, name or "", recurrence, interval_days, anchor_day,
             next_due_date, terms or "", notes or "", int(due_in_days),
             now, now))
        return cursor.lastrowid


def get_recurring_template(template_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM recurring_invoice_templates WHERE id = ?",
            (template_id,)).fetchone()
        return dict(row) if row else None


def list_recurring_templates(active_only=False):
    query = "SELECT * FROM recurring_invoice_templates"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY active DESC, next_due_date, id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_recurring_template(template_id, **fields):
    """Patch only the columns passed, mirroring update_contact."""
    allowed = ("contact_id", "name", "recurrence", "interval_days",
               "anchor_day", "next_due_date", "active", "terms", "notes",
               "due_in_days")
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    if "recurrence" in updates and updates["recurrence"] not in RECURRENCES:
        raise ValueError(f"Unknown recurrence: {updates['recurrence']!r}")
    updates["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE recurring_invoice_templates SET {assignments} WHERE id = ?",
            (*updates.values(), template_id))


def delete_recurring_template(template_id):
    """Remove a template and its lines. Instances cascade too, which is
    why the UI offers pause first -- deleting discards the record that a
    cycle was ever queued, and pausing does not."""
    with get_connection() as conn:
        conn.execute("DELETE FROM recurring_invoice_templates WHERE id = ?",
                     (template_id,))


def replace_recurring_template_lines(template_id, lines):
    """Rewrite a template's lines wholesale, as invoices do."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM recurring_invoice_template_lines WHERE template_id = ?",
            (template_id,))
        conn.executemany(
            "INSERT INTO recurring_invoice_template_lines (template_id, "
            "position, description, quantity_milli, unit_price_cents, "
            "tax_rate_bp) VALUES (?, ?, ?, ?, ?, ?)",
            [(template_id, index, line.get("description", ""),
              int(line.get("quantity_milli", 1000)),
              int(line.get("unit_price_cents", 0)),
              int(line.get("tax_rate_bp", 0)))
             for index, line in enumerate(lines)])
        conn.execute(
            "UPDATE recurring_invoice_templates SET updated_at = ? WHERE id = ?",
            (_now_iso(), template_id))


def list_recurring_template_lines(template_id):
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM recurring_invoice_template_lines "
            "WHERE template_id = ? ORDER BY position, id",
            (template_id,)).fetchall()]


def create_recurring_instance(template_id, due_date):
    """Queue one cycle for review.

    Returns the new row id, or None if this cycle already exists. The
    UNIQUE (template_id, due_date) constraint does the work: app launch
    and the daily job can fire seconds apart, and a check-then-insert
    would let both pass the check before either wrote.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO recurring_invoice_instances "
            "(template_id, due_date, generated_at, status) "
            "VALUES (?, ?, ?, 'pending_review')",
            (template_id, due_date, _now_iso()))
        return cursor.lastrowid if cursor.rowcount else None


def get_recurring_instance(instance_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM recurring_invoice_instances WHERE id = ?",
            (instance_id,)).fetchone()
        return dict(row) if row else None


def list_recurring_instances(status=None):
    """Queued cycles with their template and contact, oldest due first.

    Joined here rather than per row: the Review queue renders every
    pending instance and would otherwise fire two queries apiece.
    """
    query = ("SELECT i.*, t.name AS template_name, t.recurrence, "
             "t.contact_id, c.display_name AS contact_name "
             "FROM recurring_invoice_instances i "
             "JOIN recurring_invoice_templates t ON t.id = i.template_id "
             "JOIN contacts c ON c.id = t.contact_id")
    args = []
    if status:
        query += " WHERE i.status = ?"
        args.append(status)
    query += " ORDER BY i.due_date, i.id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def set_recurring_instance_status(instance_id, status, invoice_id=None,
                                  skip_reason=""):
    if status not in INSTANCE_STATUSES:
        raise ValueError(f"Unknown instance status: {status!r}")
    with get_connection() as conn:
        conn.execute(
            "UPDATE recurring_invoice_instances SET status = ?, "
            "invoice_id = ?, skip_reason = ?, decided_at = ? WHERE id = ?",
            (status, invoice_id, skip_reason or "", _now_iso(), instance_id))


# ------------------------------------------------ invoice payment matches
# Proposed payments awaiting a human's click. See
# migrations/add_invoice_matches.sql.


def record_payment_match(invoice_id, source, reference, amount_cents,
                         paid_on, fee_cents=0, detail=""):
    """Propose a payment. Idempotent on `reference`: a re-run of the poller
    or bank matcher returns the existing row's id rather than duplicating,
    and never resurrects one the user already dismissed."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM invoice_payment_matches WHERE reference = ?",
            (reference,)).fetchone()
        if existing:
            return existing["id"]
        cursor = conn.execute(
            "INSERT INTO invoice_payment_matches (invoice_id, source, "
            "reference, amount_cents, fee_cents, paid_on, status, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (invoice_id, source, reference, int(amount_cents), int(fee_cents),
             paid_on, detail, _now_iso()))
        return cursor.lastrowid


def payment_match_reference_exists(reference):
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM invoice_payment_matches WHERE reference = ? LIMIT 1",
            (reference,)).fetchone() is not None


def list_pending_payment_matches():
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM invoice_payment_matches WHERE status = 'pending' "
            "ORDER BY paid_on, id").fetchall()]


def get_payment_match(match_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM invoice_payment_matches WHERE id = ?",
            (match_id,)).fetchone()
        return dict(row) if row else None


def set_payment_match_status(match_id, status):
    with get_connection() as conn:
        conn.execute(
            "UPDATE invoice_payment_matches SET status = ? WHERE id = ?",
            (status, match_id))


def active_match_references():
    """References of matches that are pending or applied -- i.e. deposits
    claimed by an invoice, which the generic Plaid income sync must not also
    post. A dismissed match is intentionally absent, so a dismissed deposit
    falls back to being posted as ordinary income."""
    with get_connection() as conn:
        return {row["reference"] for row in conn.execute(
            "SELECT reference FROM invoice_payment_matches "
            "WHERE status IN ('pending', 'applied')").fetchall()}


# ------------------------------------------------------------- inventory
#
# Acquisition/sale/adjustment events keyed on Shopify variant_id, matching
# shopify_variant_costs rather than introducing a third product
# representation. See inventory.py for the FIFO logic these feed.


def record_inventory_event(row):
    """Insert one quantity event.

    INSERT OR IGNORE against the unique source_reference index, so a
    re-run of a sync cannot consume the same stock twice. Returns True
    when a row was actually written.
    """
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    with get_connection() as conn:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO inventory_quantity_events "
            f"({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )
        return cursor.rowcount > 0


def list_inventory_events(variant_id, up_to=None):
    """One variant's events in FIFO order: effective date, then insertion.

    Ordering by id as the tiebreak keeps two same-day acquisitions in the
    order they were recorded, so FIFO is deterministic rather than
    dependent on however SQLite happens to return them.
    """
    query = ("SELECT * FROM inventory_quantity_events WHERE variant_id = ?")
    args = [str(variant_id)]
    if up_to:
        query += " AND effective_date <= ?"
        args.append(up_to)
    query += " ORDER BY effective_date, id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def get_inventory_event(event_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM inventory_quantity_events WHERE id = ?",
            (event_id,)).fetchone()
    return dict(row) if row else None


def list_inventory_reconciliation_queue():
    """Acquisition layers awaiting a funding decision in Review."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory_quantity_events "
            "WHERE event_type = 'acquisition' "
            "AND accounting_status = 'legacy_unposted' "
            "ORDER BY effective_date, id"
        ).fetchall()
    return [dict(row) for row in rows]


def list_unresolved_shopify_cogs(start_date=None, end_date=None):
    """Shopify orders whose COGS source reference is still skipped."""
    query = (
        "SELECT o.order_id, o.created_at, s.reason, s.detail "
        "FROM ledger_source_import_skips s "
        "JOIN shopify_orders o ON s.source_reference = "
        "('shopify:order:' || o.order_id || ':cogs') "
        "WHERE s.source = 'shopify' AND s.reason IN "
        "('inventory_acquisition_not_tracked', 'missing_cost_data')"
    )
    args = []
    if start_date:
        query += " AND substr(o.created_at, 1, 10) >= ?"
        args.append(start_date)
    if end_date:
        query += " AND substr(o.created_at, 1, 10) <= ?"
        args.append(end_date)
    query += " ORDER BY o.created_at, o.order_id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def update_inventory_event_accounting(event_id, accounting_status,
                                      funding_account_id=None,
                                      journal_source_reference=None,
                                      resolution_note=None, resolved_at=None):
    """Update only the accounting decision fields of one acquisition row."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE inventory_quantity_events SET accounting_status = ?, "
            "funding_account_id = ?, journal_source_reference = ?, "
            "resolution_note = ?, resolved_at = ? WHERE id = ?",
            (accounting_status, funding_account_id, journal_source_reference,
             resolution_note, resolved_at, event_id),
        )
        return cursor.rowcount > 0


def list_inventory_variants():
    """Every variant with at least one recorded event."""
    with get_connection() as conn:
        return [row["variant_id"] for row in conn.execute(
            "SELECT DISTINCT variant_id FROM inventory_quantity_events"
        ).fetchall()]


def inventory_event_exists(source_reference):
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM inventory_quantity_events WHERE source_reference = ?",
            (source_reference,),
        ).fetchone() is not None


def list_variant_cost_history(variant_id):
    """Cost snapshots for one variant, newest first."""
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM shopify_variant_costs WHERE variant_id = ? "
            "ORDER BY captured_at DESC", (str(variant_id),)
        ).fetchall()]


def latest_shopify_variant_cost(variant_id):
    """The most recent snapshot regardless of date, for change detection."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM shopify_variant_costs WHERE variant_id = ? "
            "ORDER BY captured_at DESC LIMIT 1", (str(variant_id),)
        ).fetchone()
    return dict(row) if row else None


def shopify_variant_titles():
    """variant_id -> a readable label, from the stored product payloads."""
    import json as _json
    titles = {}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT title, variants_json FROM shopify_products").fetchall()
    for row in rows:
        try:
            variants = _json.loads(row["variants_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for variant in variants if isinstance(variants, list) else []:
            if not isinstance(variant, dict):
                continue
            variant_id = str(variant.get("id") or "").strip()
            if not variant_id:
                continue
            name = variant.get("title")
            titles[variant_id] = (f'{row["title"]} ({name})'
                                  if name and name != "Default Title"
                                  else row["title"])
    return titles


# --------------------------------------------------- shopify analytics
#
# Historical aggregate figures imported from Shopify Admin CSV exports.
# Deliberately walled off from the ledger: nothing here is ever posted,
# and nothing reconciles it against journal entries.


def upsert_shopify_analytics_row(row):
    """Write one aggregate figure, replacing any prior import of it.

    Keyed on (metric_type, period, period_start_date, product_ref) so a
    user who exports overlapping date ranges twice does not double their
    historical totals.
    """
    columns = list(row)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns
                        if c not in ("metric_type", "period",
                                     "period_start_date", "product_ref"))
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO shopify_analytics_history "
            f"({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(metric_type, period, period_start_date, "
            f"COALESCE(product_ref, '')) DO UPDATE SET {updates}",
            [row[column] for column in columns],
        )


def list_shopify_analytics(metric_type=None, period=None, limit=None):
    query = "SELECT * FROM shopify_analytics_history"
    clauses, args = [], []
    if metric_type:
        clauses.append("metric_type = ?")
        args.append(metric_type)
    if period:
        clauses.append("period = ?")
        args.append(period)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY period_start_date"
    if limit:
        query += f" LIMIT {int(limit)}"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, args).fetchall()]


def shopify_analytics_summary():
    """What has been imported, for the page header and verification."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT metric_type, period, COUNT(*) AS rows, "
            "MIN(period_start_date) AS first_period, "
            "MAX(period_start_date) AS last_period, "
            "MAX(imported_at) AS imported_at "
            "FROM shopify_analytics_history "
            "GROUP BY metric_type, period ORDER BY metric_type, period"
        ).fetchall()
    return [dict(row) for row in rows]
