"""Permanent proof that every legacy app-money path stores integer cents."""

import os
import sqlite3
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-money-cents-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import check_bank  # noqa: E402
import db  # noqa: E402
import paths  # noqa: E402
import plaid_client  # noqa: E402
import setup_wizard  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    MONEY_CENTS_MIGRATION,
    MONEY_FLOAT_NORMALIZATION_META_KEY,
    run_migrations,
)


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}")


def seed_production_shaped_legacy_db():
    with sqlite3.connect(paths.db_path()) as conn:
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE pending_items (
                id INTEGER PRIMARY KEY, amount REAL NOT NULL);
            CREATE TABLE reconciliation_history (
                date TEXT PRIMARY KEY, wave_balance REAL NOT NULL,
                bank_balance REAL NOT NULL, residual REAL NOT NULL);
            CREATE TABLE sales_tax_nexus (
                id INTEGER PRIMARY KEY, revenue REAL NOT NULL,
                threshold REAL);
            CREATE TABLE shopify_orders (
                order_id TEXT PRIMARY KEY, total_price REAL NOT NULL,
                subtotal_price REAL NOT NULL, total_discounts REAL NOT NULL,
                total_tax REAL NOT NULL, refund_total REAL NOT NULL);
            CREATE TABLE shopify_customers (
                customer_id TEXT PRIMARY KEY, total_spent REAL NOT NULL);
            INSERT INTO pending_items VALUES (1, 0.29);
            INSERT INTO sales_tax_nexus VALUES (1, 123.45, 100000.00);
            INSERT INTO shopify_orders VALUES
                ('order-1', 12.34, 11.11, 0.23, 1.00, 2.34);
            INSERT INTO shopify_customers VALUES ('customer-1', 44.44);
        """)
        residuals = (
            ("2026-07-13", 158.27, 137.19, 1.4210854715202004e-14),
            ("2026-07-19", 137.19, 62.57, 1.4210854715202004e-14),
            ("2026-07-20", 137.19, 62.57, 1.4210854715202004e-14),
            ("2026-07-22", 33.73, 34.16, 2.7755575615628914e-16),
            ("2026-07-25", 5.38, 44.92, -40.519999999999996),
            ("2026-07-26", -5.24, 44.92, -50.00000000000001),
        )
        conn.executemany(
            "INSERT INTO reconciliation_history VALUES (?, ?, ?, ?)",
            residuals,
        )


def migration_checks():
    seed_production_shaped_legacy_db()
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    with sqlite3.connect(paths.db_path()) as conn:
        schemas = {
            table: {row[1]: row[2] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            for table in (
                "pending_items", "reconciliation_history", "sales_tax_nexus",
                "shopify_orders", "shopify_customers")
        }
        pending = conn.execute(
            "SELECT amount_cents, typeof(amount_cents) FROM pending_items"
        ).fetchone()
        nexus = conn.execute(
            "SELECT revenue_cents, threshold_cents FROM sales_tax_nexus"
        ).fetchone()
        order = conn.execute(
            "SELECT total_price_cents, subtotal_price_cents, "
            "total_discounts_cents, total_tax_cents, refund_total_cents "
            "FROM shopify_orders"
        ).fetchone()
        customer = conn.execute(
            "SELECT total_spent_cents FROM shopify_customers").fetchone()
        residuals = conn.execute(
            "SELECT residual_cents FROM reconciliation_history ORDER BY date"
        ).fetchall()
        memo = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (MONEY_FLOAT_NORMALIZATION_META_KEY,),
        ).fetchone()
    check("legacy money migration is idempotent",
          first[MONEY_CENTS_MIGRATION] == "created"
          and second[MONEY_CENTS_MIGRATION] == "skipped")
    check("legacy REAL columns are replaced by INTEGER cents", all(
        column_type == "INTEGER"
        for schema in schemas.values()
        for name, column_type in schema.items()
        if name.endswith("_cents")))
    check("exact legacy values backfill without a 100x error",
          pending == (29, "integer")
          and nexus == (12345, 10000000)
          and order == (1234, 1111, 23, 100, 234)
          and customer == (4444,))
    check("only the six approved float residuals normalize",
          [row[0] for row in residuals] == [0, 0, 0, 0, -4052, -5000]
          and memo is not None
          and "float-precision normalization" in memo[0])


def rejection_check():
    rejected = tempfile.TemporaryDirectory(
        prefix="klearing-money-reject-", ignore_cleanup_errors=True)
    os.environ["KLEARING_DATA_DIR"] = rejected.name
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute(
            "CREATE TABLE pending_items "
            "(id INTEGER PRIMARY KEY, amount REAL NOT NULL)")
        conn.execute("INSERT INTO pending_items VALUES (1, 0.291)")
    error = None
    try:
        run_migrations(report=False)
    except RuntimeError as exc:
        error = str(exc)
    with sqlite3.connect(paths.db_path()) as conn:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(pending_items)")}
        value = conn.execute("SELECT amount FROM pending_items").fetchone()[0]
    check("every unapproved sub-cent legacy value fails atomically",
          "ambiguous sub-cent value" in (error or "")
          and columns == {"id", "amount"}
          and value == 0.291)
    rejected.cleanup()


def boundary_amount_rejection_checks():
    """Cross-cutting proof that every boundary-local amount-to-cents
    converter in this codebase agrees on rejecting the same class of
    malformed input, even though each legitimately has its own exception
    type and its own negative-value policy (setup_wizard's
    _opening_balance_to_cents additionally rejects negatives -- an
    opening balance can't be negative -- where plaid_client's and
    check_bank's converters allow them, since a raw bank/Plaid amount can
    be a legitimate debit). NOT sharing one implementation across these
    three boundaries is the correct, established house pattern here --
    this check exists only to keep them from silently drifting apart on
    the inputs they DO agree should be rejected (sub-cent precision).

    Deliberately independent of ingestion_checks()'s tempdir/DB setup --
    none of plaid_client._amount_to_cents, check_bank._bank_amount_to_cents,
    or setup_wizard._opening_balance_to_cents touch the database -- so
    this runs and is provable entirely on its own, called from run()
    BEFORE ingestion_checks() rather than folded inside it."""
    plaid_rejected = bank_rejected = setup_rejected = False
    try:
        plaid_client._amount_to_cents("0.291")
    except plaid_client.PlaidAPIError:
        plaid_rejected = True
    try:
        check_bank._bank_amount_to_cents("0.291")
    except RuntimeError:
        bank_rejected = True
    try:
        setup_wizard._opening_balance_to_cents("0.291", "test balance")
    except ValueError:
        setup_rejected = True
    check("Plaid, generic-bank, and SimpleFIN-cutover boundaries all "
          "reject sub-cent inputs",
          plaid_rejected and bank_rejected and setup_rejected)


def ingestion_checks():
    fresh = tempfile.TemporaryDirectory(
        prefix="klearing-money-ingestion-", ignore_cleanup_errors=True)
    os.environ["KLEARING_DATA_DIR"] = fresh.name
    db.init_db()
    item_id = db.insert_pending_item(
        "test", "Exact Vendor", "0.29", "2026-08-19", None)
    db.insert_reconcile_snapshot(
        date.today().isoformat(), "1.23", "0.94", "0.29", False)
    db.add_nexus_state("Iowa", "12.34", "100000.00")
    db.upsert_shopify_order({
        "order_id": "exact-order", "created_at": "2026-08-19T00:00:00Z",
        "total_price": "0.29", "subtotal_price": "0.29",
        "total_discounts": "0.00", "total_tax": "0.00",
        "refund_total": "0.00", "line_items_json": "[]",
    })
    db.upsert_shopify_customer({
        "customer_id": "exact-customer", "total_spent": "0.29",
    })
    with db.get_connection() as conn:
        stored = (
            conn.execute("SELECT amount_cents FROM pending_items").fetchone()[0],
            conn.execute(
                "SELECT residual_cents FROM reconciliation_history").fetchone()[0],
            conn.execute(
                "SELECT revenue_cents FROM sales_tax_nexus").fetchone()[0],
            conn.execute(
                "SELECT total_price_cents FROM shopify_orders").fetchone()[0],
            conn.execute(
                "SELECT total_spent_cents FROM shopify_customers").fetchone()[0],
        )
    check("fresh writes store exact integer cents across every table",
          stored == (29, 29, 1234, 29, 29))
    public_values = (
        db.get_item(item_id)["amount"],
        db.list_reconcile_history(days=1)[0]["residual"],
        db.list_nexus_states()[0]["revenue"],
        db.get_shopify_order("exact-order")["total_price"],
        db.list_shopify_customers()[0]["total_spent"],
    )
    check("compatibility reads return exact Decimal dollars",
          public_values == (Decimal("0.29"), Decimal("0.29"),
                            Decimal("12.34"), Decimal("0.29"),
                            Decimal("0.29"))
          and all(isinstance(value, Decimal) for value in public_values))

    plaid_client._persist_sync_records({
        "added": [{"transaction_id": "plaid-exact", "account_id": "acct",
                   "date": "2026-08-19", "amount": "0.29"}],
        "modified": [], "removed": [],
    })
    plaid_row = next(
        row for row in db.list_plaid_transactions_for_ledger()
        if row["transaction_id"] == "plaid-exact")
    normalized = check_bank._normalize_bank_transaction({
        "external_id": "bank-exact", "date": "2026-08-19",
        "amount": "-0.29", "counterparty": "Exact Bank",
    })
    check("Plaid and generic-bank boundaries preserve 0.29 exactly",
          plaid_row["amount_cents"] == -29
          and normalized["amount_cents"] == -29
          and normalized["amount"] == Decimal("-0.29"))
    # The cross-boundary sub-cent-rejection proof (Plaid, generic-bank,
    # and now setup_wizard's SimpleFIN-cutover converter) lives in
    # boundary_amount_rejection_checks() instead of here -- it has no
    # dependency on this function's tempdir/DB setup, so run() calls it
    # on its own, before this function, rather than folding it into the
    # middle of an unrelated ingestion flow.
    fresh.cleanup()


def run():
    migration_checks()
    rejection_check()
    boundary_amount_rejection_checks()
    ingestion_checks()
    if failures:
        print(f"FAIL: {failures} of {checks} money-cents checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} money-cents checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
