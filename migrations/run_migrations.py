"""Apply Klearing's additive database migrations safely and repeatedly."""

import sqlite3
import sys
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


# Running this file directly should use the same paths module as the app.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import paths
import tax_categories
from migrations.schema_versions import StorageCompatibilityError


TAX_TABLES = ("kl_transactions", "kl_tax_settings", "kl_units_config")
LEDGER_TABLES = ("accounts", "journal_entries", "journal_lines")
LEDGER_SOURCE_SYNC_TABLES = ("bank_transactions", "ledger_source_imports")
SHOPIFY_COST_TABLES = ("shopify_variant_costs", "ledger_source_import_skips")
SHOPIFY_PAYOUT_TABLES = ("shopify_payouts",)
LEDGER_ROUTING_TABLES = ("bank_accounts",)
CONTACTS_TABLES = ("contacts", "contact_aliases")
INVOICING_TABLES = ("invoices", "invoice_lines", "invoice_payments",
                    "invoice_credit_notes", "customer_deposits")
INVOICE_MATCHES_TABLES = ("invoice_payment_matches",)
CONTACT_PIPELINE_TABLES = ("contact_pipeline",)
RECURRING_INVOICE_TABLES = ("recurring_invoice_templates",
                            "recurring_invoice_template_lines",
                            "recurring_invoice_instances")
INVENTORY_TABLES = ("inventory_quantity_events",)
SHOPIFY_ANALYTICS_TABLES = ("shopify_analytics_history",)
INSIGHT_WIDGET_TABLES = ("insight_widget_config",)
MIGRATIONS = (
    (TAX_TABLES, "add_tax_tables.sql"),
    (LEDGER_TABLES, "add_ledger_tables.sql"),
    (LEDGER_SOURCE_SYNC_TABLES, "add_ledger_source_sync.sql"),
    (SHOPIFY_COST_TABLES, "add_shopify_inventory_costs.sql"),
    (SHOPIFY_PAYOUT_TABLES, "add_shopify_payouts.sql"),
    (LEDGER_ROUTING_TABLES, "add_ledger_routing.sql"),
    (CONTACTS_TABLES, "add_contacts.sql"),
    (INVOICING_TABLES, "add_invoicing.sql"),
    (INVOICE_MATCHES_TABLES, "add_invoice_matches.sql"),
    (CONTACT_PIPELINE_TABLES, "add_contact_pipeline.sql"),
    (RECURRING_INVOICE_TABLES, "add_recurring_invoices.sql"),
    (INVENTORY_TABLES, "add_inventory.sql"),
    (SHOPIFY_ANALYTICS_TABLES, "add_shopify_analytics.sql"),
    (INSIGHT_WIDGET_TABLES, "add_insight_widget_config.sql"),
)

ACCOUNT_TAX_CATEGORY_FIELD = "accounts.tax_category"
PLANNING_SOURCE_RETIREMENT = "kl_units_config.source_auto_retired"
BANK_REWARDS_CASHBACK_ACCOUNT = "accounts.bank_rewards_cashback_income"
INVENTORY_ACCOUNTING_FIELDS = "inventory_quantity_events.accounting_status"
BANK_SOURCE_MIGRATION = "bank_sources.canonical_v1"
MONEY_CENTS_MIGRATION = "legacy_money.integer_cents_v1"
BANK_ACCOUNT_MAPPING_STATE_FIELD = "bank_accounts.mapping_state"
MONEY_FLOAT_NORMALIZATION_META_KEY = (
    "money_cents_float_precision_normalization_v1")

_CENT = Decimal("0.01")
_LEGACY_MONEY_COLUMNS = {
    "pending_items": ("id", {
        "amount": "amount_cents INTEGER NOT NULL DEFAULT 0",
    }),
    "reconciliation_history": ("date", {
        "wave_balance": "wave_balance_cents INTEGER NOT NULL DEFAULT 0",
        "bank_balance": "bank_balance_cents INTEGER NOT NULL DEFAULT 0",
        "residual": "residual_cents INTEGER NOT NULL DEFAULT 0",
    }),
    "sales_tax_nexus": ("id", {
        "revenue": "revenue_cents INTEGER NOT NULL DEFAULT 0",
        "threshold": "threshold_cents INTEGER",
    }),
    "shopify_orders": ("order_id", {
        "total_price": "total_price_cents INTEGER NOT NULL DEFAULT 0",
        "subtotal_price": "subtotal_price_cents INTEGER NOT NULL DEFAULT 0",
        "total_discounts": "total_discounts_cents INTEGER NOT NULL DEFAULT 0",
        "total_tax": "total_tax_cents INTEGER NOT NULL DEFAULT 0",
        "refund_total": "refund_total_cents INTEGER NOT NULL DEFAULT 0",
    }),
    "shopify_customers": ("customer_id", {
        "total_spent": "total_spent_cents INTEGER NOT NULL DEFAULT 0",
    }),
}

# Approved 2026-08-19 after a read-only production preflight. These are the
# six known residuals created by legacy binary-float subtraction. The row key
# and exact stored representation are both required so this cannot become a
# general-purpose rounding escape hatch.
_APPROVED_FLOAT_PRECISION_NORMALIZATIONS = {
    ("reconciliation_history", "2026-07-13", "residual",
     Decimal("1.4210854715202004e-14")),
    ("reconciliation_history", "2026-07-19", "residual",
     Decimal("1.4210854715202004e-14")),
    ("reconciliation_history", "2026-07-20", "residual",
     Decimal("1.4210854715202004e-14")),
    ("reconciliation_history", "2026-07-22", "residual",
     Decimal("2.7755575615628914e-16")),
    ("reconciliation_history", "2026-07-25", "residual",
     Decimal("-40.519999999999996")),
    ("reconciliation_history", "2026-07-26", "residual",
     Decimal("-50.00000000000001")),
}
_FLOAT_PRECISION_NORMALIZATION_MEMO = (
    "Approved float-precision normalization: six specifically identified "
    "reconciliation_history residuals were rounded to their nearest cent; "
    "all other sub-cent legacy values remain migration errors."
)


def _sql_path(filename):
    """The bundled SQL file when frozen, otherwise this source file's peer."""
    return paths.resource_path(f"migrations/{filename}")


def _ensure_account_tax_category(conn):
    """Add the validated nullable account mapping to existing Ledgers.

    The table-oriented migration loop cannot detect a column added after a
    table shipped.  Inspecting ``PRAGMA table_info`` makes this upgrade safe
    and idempotent for both existing installs and fresh databases.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "tax_category" in columns:
        return "skipped"

    def quoted(values):
        return ", ".join(f"'{value}'" for value in values)

    revenue = quoted(tax_categories.REVENUE_CATEGORIES)
    expense = quoted(tax_categories.EXPENSE_CATEGORIES)
    conn.execute(
        "ALTER TABLE accounts ADD COLUMN tax_category TEXT "
        "CHECK (tax_category IS NULL "
        f"OR (type = 'revenue' AND tax_category IN ({revenue})) "
        f"OR (type = 'expense' AND tax_category IN ({expense})))"
    )
    conn.executemany(
        "UPDATE accounts SET tax_category = ? "
        "WHERE code = ? AND tax_category IS NULL",
        [(category, code) for code, category
         in tax_categories.DEFAULT_ACCOUNT_CATEGORIES.items()],
    )
    return "created"


def _retire_auto_planning_source(conn):
    """Normalize the removed Auto source without inventing planning values."""
    row = conn.execute(
        "SELECT source FROM kl_units_config WHERE id = 1"
    ).fetchone()
    if row is None or row[0] in {"shopify", "manual"}:
        return "skipped"
    has_shopify_table = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'shopify_orders'"
    ).fetchone())
    has_shopify_data = has_shopify_table and bool(conn.execute(
        "SELECT 1 FROM shopify_orders LIMIT 1"
    ).fetchone())
    conn.execute(
        "UPDATE kl_units_config SET source = ? WHERE id = 1",
        ("shopify" if has_shopify_data else "manual",),
    )
    return "created"


def _ensure_bank_rewards_cashback_account(conn, existing_tables):
    """Add the explicit Plaid rewards account to every existing Ledger.

    This is intentionally table-gated rather than added to an already-shipped
    ``accounts`` SQL migration: existing installs skip that SQL file because
    their Ledger tables already exist.  The stable code keeps the insert
    additive and safe to repeat.
    """
    if "accounts" not in existing_tables:
        return "skipped"
    existing = conn.execute(
        "SELECT name, type, parent_account_id FROM accounts WHERE code = '4210'"
    ).fetchone()
    if existing is not None:
        if (existing[0], existing[1], existing[2]) == (
                "Bank Rewards & Cashback Income", "revenue", None):
            return "skipped"
        raise RuntimeError(
            "Account code 4210 is already in use and cannot be safely used "
            "for Bank Rewards & Cashback Income."
        )
    conn.execute(
        "INSERT INTO accounts (code, name, type, parent_account_id) "
        "VALUES ('4210', 'Bank Rewards & Cashback Income', 'revenue', NULL)"
    )
    return "created"


def _ensure_inventory_accounting_fields(conn, existing_tables):
    """Add Ledger linkage/status to pre-existing quantity events safely."""
    if "inventory_quantity_events" not in existing_tables:
        return "skipped"
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(inventory_quantity_events)")}
    additions = (
        ("accounting_status", "TEXT NOT NULL DEFAULT 'legacy_unposted'"),
        ("funding_account_id", "INTEGER"),
        ("journal_source_reference", "TEXT"),
        ("resolution_note", "TEXT"),
        ("resolved_at", "TEXT"),
    )
    created = False
    for name, definition in additions:
        if name not in columns:
            conn.execute(
                f"ALTER TABLE inventory_quantity_events ADD COLUMN {name} {definition}")
            created = True
    # Sales and adjustments already correspond to a posted COGS/count action;
    # only acquisition rows need a funding-account reconciliation decision.
    conn.execute(
        "UPDATE inventory_quantity_events SET accounting_status = 'ledger_posted' "
        "WHERE event_type <> 'acquisition' AND accounting_status = 'legacy_unposted'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inventory_events_accounting_status "
        "ON inventory_quantity_events(accounting_status, event_type)"
    )
    return "created" if created else "skipped"


def _reject_duplicate_ledger_account_mappings(conn):
    """Refuse to upgrade rather than let a pre-existing duplicate mapping
    crash the unique-index creation with a raw ``sqlite3.IntegrityError``.

    Nothing forbade two bank_accounts rows sharing one ``ledger_account_id``
    before this migration, so an install that reached that state under the
    old rules must be told in plain English and given a chance to fix it,
    not have the app die silently mid-migration with the database stuck one
    version short forever (see ``main.py``'s ``except StorageCompatibilityError``
    handling, which already knows how to render exactly this).
    """
    duplicates = conn.execute(
        "SELECT ledger_account_id FROM bank_accounts "
        "WHERE ledger_account_id IS NOT NULL "
        "GROUP BY ledger_account_id HAVING COUNT(*) > 1 "
        "ORDER BY ledger_account_id"
    ).fetchall()
    if not duplicates:
        return
    details = []
    for (ledger_account_id,) in duplicates:
        rows = conn.execute(
            "SELECT source, account_id FROM bank_accounts "
            "WHERE ledger_account_id = ? ORDER BY source, account_id",
            (ledger_account_id,),
        ).fetchall()
        pairs = ", ".join(f"{source}:{account_id}" for source, account_id in rows)
        details.append(f"Ledger account {ledger_account_id} is mapped by: {pairs}")
    raise StorageCompatibilityError(
        "Klearing cannot upgrade this database: two bank accounts are mapped "
        "to the same Ledger account, which the new schema no longer allows. "
        + " | ".join(details) +
        ". Unmap one of each pair (Settings > Connections) so every Ledger "
        "account has at most one bank-account mapping, then try again. No "
        "data was changed."
    )


def _ensure_bank_account_mapping_state(conn):
    """Add the UI-only mapping-decision tracker to existing Ledgers.

    ``mapping_state`` has no effect on posting: ``ledger_account_id IS
    NULL`` alone still means "never post this account" (see
    ``db.ensure_bank_ledger_account`` / ``ledger_sync.SkipReason
    .UNMAPPED_BANK_ACCOUNT``, both unchanged by this migration). This
    column only lets the account-mapping screen (Task 4) tell "no answer
    yet" (``''``, the default) apart from "the user explicitly excluded
    this account" (``'excluded'``) — without it, the screen cannot
    distinguish an account nobody has looked at yet from one someone
    deliberately opted out of.

    Also adds a partial unique index so one Ledger account can never be
    the explicit mapping target of two different bank-account identities
    at once, scoped globally across every source (Plaid, SimpleFIN, or
    any future adapter) rather than only within one source, since a
    Ledger account receiving postings from two distinct bank identities
    is unsafe regardless of where they come from. Nothing forbade that
    before this migration, so a pre-flight check runs first and refuses
    (readably) rather than letting the index creation itself raise a raw
    ``sqlite3.IntegrityError`` on an install that happens to already be in
    that state. Index creation is otherwise naturally idempotent via
    ``IF NOT EXISTS``.

    Wrapped in its own explicit transaction (started only if ``conn``
    isn't already inside one) so "no data was changed" on refusal is
    literally true on every call path, not just the one where an earlier
    DML statement happened to have already opened an implicit transaction.
    Python's sqlite3 module only auto-opens a transaction ahead of DML
    (INSERT/UPDATE/DELETE/REPLACE), never ahead of DDL -- so a caller that
    runs only ``ALTER TABLE``/``CREATE INDEX`` here (as
    ``db._schema_migration_plan()``'s dedicated step does) would otherwise
    autocommit the ``ALTER TABLE ADD COLUMN`` before the duplicate check
    even ran, leaving a stray column behind on refusal even though the
    index never got created (harmless for classification, but not what
    "no data was changed" promises).
    """
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bank_accounts)")}
        column_created = "mapping_state" not in columns
        if column_created:
            conn.execute(
                "ALTER TABLE bank_accounts ADD COLUMN mapping_state TEXT "
                "NOT NULL DEFAULT '' CHECK (mapping_state IN ('', 'excluded'))"
            )
        _reject_duplicate_ledger_account_mappings(conn)
        index_created = not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_bank_accounts_ledger_unique'"
        ).fetchone()
        if index_created:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_accounts_ledger_unique "
                "ON bank_accounts(ledger_account_id) WHERE ledger_account_id IS NOT NULL"
            )
    except Exception:
        if own_transaction:
            conn.rollback()
        raise
    else:
        if own_transaction:
            conn.commit()
    return "created" if (column_created or index_created) else "skipped"


def _migrate_legacy_plaid_sources(conn):
    """Move legacy Plaid caches into source-scoped canonical bank tables.

    The old raw cache used Plaid's sign (positive = money out). Negating at
    this one-time boundary makes every stored source obey BankAdapter's
    negative-money-out contract. Dropping the old tables is the idempotency
    marker, so a second migration can never flip signs twice.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    changed = False
    if "plaid_transactions" in tables:
        conn.execute(
            "INSERT OR IGNORE INTO bank_transactions "
            "(source, transaction_id, account_id, date, authorized_date, "
            " amount_cents, merchant_name, name, pending, category_primary, "
            " category_detailed, removed, synced_at) "
            "SELECT 'plaid', transaction_id, COALESCE(account_id, ''), date, "
            " authorized_date, -amount_cents, merchant_name, name, pending, "
            " category_primary, category_detailed, removed, synced_at "
            "FROM plaid_transactions"
        )
        conn.execute("DROP TABLE plaid_transactions")
        changed = True
    if "plaid_accounts" in tables:
        conn.execute(
            "INSERT OR IGNORE INTO bank_accounts "
            "(source, account_id, name, official_name, type, subtype, mask, "
            " ledger_account_id, synced_at) "
            "SELECT 'plaid', account_id, name, official_name, type, subtype, "
            " mask, ledger_account_id, synced_at FROM plaid_accounts"
        )
        conn.execute("DROP TABLE plaid_accounts")
        changed = True
    return "created" if changed else "skipped"


def _legacy_money_cents(table, row_key, column, value, normalized):
    """Return exact cents, allowing only the six approved float artifacts."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
        rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot migrate {table}.{column} for row {row_key!r}: "
            f"invalid money value {value!r}.") from exc
    if not amount.is_finite():
        raise RuntimeError(
            f"Cannot migrate {table}.{column} for row {row_key!r}: "
            f"non-finite money value {value!r}.")
    if amount != rounded:
        approval = (table, str(row_key), column, amount)
        if approval not in _APPROVED_FLOAT_PRECISION_NORMALIZATIONS:
            raise RuntimeError(
                f"Cannot migrate {table}.{column} for row {row_key!r}: "
                f"ambiguous sub-cent value {value!r}.")
        normalized.append(approval)
    return int(rounded * 100)


def _migrate_legacy_money_to_cents(conn):
    """Replace legacy dollar REAL columns with integer-cent columns.

    Every conversion is validated before the surrounding transaction commits.
    A failure rolls back all added columns and backfills, preventing a partial
    migration or a later 100x reinterpretation.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    changed = False
    normalized = []
    for table, (key_column, money_columns) in _LEGACY_MONEY_COLUMNS.items():
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(
            f"PRAGMA table_info({table})")}
        for legacy_column, cents_definition in money_columns.items():
            cents_column = f"{legacy_column}_cents"
            has_legacy = legacy_column in columns
            has_cents = cents_column in columns
            if has_legacy and has_cents:
                raise RuntimeError(
                    f"Cannot migrate {table}: both {legacy_column} and "
                    f"{cents_column} exist; refusing an ambiguous partial "
                    "money migration.")
            if not has_legacy:
                continue
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {cents_definition}")
            rows = conn.execute(
                f"SELECT {key_column}, {legacy_column} FROM {table}").fetchall()
            for row_key, value in rows:
                cents = _legacy_money_cents(
                    table, row_key, legacy_column, value, normalized)
                conn.execute(
                    f"UPDATE {table} SET {cents_column} = ? "
                    f"WHERE {key_column} = ?",
                    (cents, row_key),
                )
            conn.execute(
                f"ALTER TABLE {table} DROP COLUMN {legacy_column}")
            columns.remove(legacy_column)
            columns.add(cents_column)
            changed = True
    if normalized and "meta" in tables:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MONEY_FLOAT_NORMALIZATION_META_KEY,
             _FLOAT_PRECISION_NORMALIZATION_MEMO),
        )
    return "created" if changed else "skipped"


def _run_money_migration_atomically(conn):
    """Give the strict money migration its own rollback boundary."""
    conn.execute("SAVEPOINT legacy_money_integer_cents")
    try:
        result = _migrate_legacy_money_to_cents(conn)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT legacy_money_integer_cents")
        conn.execute("RELEASE SAVEPOINT legacy_money_integer_cents")
        raise
    conn.execute("RELEASE SAVEPOINT legacy_money_integer_cents")
    return result


def _apply_versioned_table_migration(tables, filename):
    """Apply one historical table head for the schema-version orchestrator."""
    with closing(sqlite3.connect(paths.db_path())) as conn, conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if any(name not in existing for name in tables):
            conn.executescript(_sql_path(filename).read_text(encoding="utf-8"))


def _apply_versioned_post_migration(migration):
    with closing(sqlite3.connect(paths.db_path())) as conn, conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if migration in {
                _ensure_bank_rewards_cashback_account,
                _ensure_inventory_accounting_fields,
        }:
            migration(conn, existing)
        else:
            migration(conn)


def versioned_migration_steps():
    """Ordered pre-version migrations, one callable per ledger version."""
    steps = []
    for tables, filename in MIGRATIONS:
        name = Path(filename).stem.replace("add_", "").replace("_", "-")
        steps.append((
            name,
            lambda tables=tables, filename=filename:
                _apply_versioned_table_migration(tables, filename),
        ))
    post_migrations = (
        ("account-tax-categories", _ensure_account_tax_category),
        ("planning-source-retirement", _retire_auto_planning_source),
        ("bank-rewards-account", _ensure_bank_rewards_cashback_account),
        ("inventory-accounting-fields", _ensure_inventory_accounting_fields),
        ("canonical-bank-sources", _migrate_legacy_plaid_sources),
        ("integer-cent-money-storage", _run_money_migration_atomically),
        # DO NOT add new steps here. This tuple's output is spliced into
        # db._schema_migration_plan() BEFORE the fixed "wave-duplicate-post
        # -history" (v22) and "bank-account-mapping-state" (v23) steps that
        # db.py appends after it. Adding a step here would silently push
        # those two later and shift every version number after them --
        # ensure_current_schema() only checks the total step count against
        # CURRENT_SCHEMA_VERSION, not step identity or order, so a stamped
        # database would happily skip whatever moved. A brand new
        # post-migration belongs in db._schema_migration_plan() instead,
        # appended after "bank-account-mapping-state" (see the asserts
        # there pinning both fixed steps to their expected slot).
    )
    for name, migration in post_migrations:
        steps.append((
            name,
            lambda migration=migration:
                _apply_versioned_post_migration(migration),
        ))
    return steps


def run_migrations(report=True):
    """Apply additive migrations and return each table's status."""
    results = {}
    with sqlite3.connect(paths.db_path()) as conn:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for tables, filename in MIGRATIONS:
            missing = [name for name in tables if name not in existing]
            if missing:
                conn.executescript(_sql_path(filename).read_text(encoding="utf-8"))
                existing.update(tables)
            results.update({name: "created" if name in missing else "skipped"
                            for name in tables})
        results[ACCOUNT_TAX_CATEGORY_FIELD] = _ensure_account_tax_category(conn)
        results[PLANNING_SOURCE_RETIREMENT] = _retire_auto_planning_source(conn)
        results[BANK_REWARDS_CASHBACK_ACCOUNT] = (
            _ensure_bank_rewards_cashback_account(conn, existing))
        results[INVENTORY_ACCOUNTING_FIELDS] = (
            _ensure_inventory_accounting_fields(conn, existing))
        results[BANK_SOURCE_MIGRATION] = _migrate_legacy_plaid_sources(conn)
        results[MONEY_CENTS_MIGRATION] = _run_money_migration_atomically(conn)
        results[BANK_ACCOUNT_MAPPING_STATE_FIELD] = (
            _ensure_bank_account_mapping_state(conn))
    if report:
        for tables, _ in MIGRATIONS:
            for name in tables:
                print(f"{name}: {results[name]}")
        print(f"{ACCOUNT_TAX_CATEGORY_FIELD}: "
              f"{results[ACCOUNT_TAX_CATEGORY_FIELD]}")
        print(f"{PLANNING_SOURCE_RETIREMENT}: "
              f"{results[PLANNING_SOURCE_RETIREMENT]}")
        print(f"{BANK_REWARDS_CASHBACK_ACCOUNT}: "
              f"{results[BANK_REWARDS_CASHBACK_ACCOUNT]}")
        print(f"{INVENTORY_ACCOUNTING_FIELDS}: "
              f"{results[INVENTORY_ACCOUNTING_FIELDS]}")
        print(f"{BANK_SOURCE_MIGRATION}: {results[BANK_SOURCE_MIGRATION]}")
        print(f"{MONEY_CENTS_MIGRATION}: {results[MONEY_CENTS_MIGRATION]}")
        print(f"{BANK_ACCOUNT_MAPPING_STATE_FIELD}: "
              f"{results[BANK_ACCOUNT_MAPPING_STATE_FIELD]}")
    return results


if __name__ == "__main__":
    run_migrations(report=True)
