"""Offline checks for the additive Tax database migration."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-migration-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
import db  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    PLANNING_SOURCE_RETIREMENT,
    TAX_TABLES,
    run_migrations,
)


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def run():
    failures = 0
    first = run_migrations(report=False)
    failures += check("creates all Tax tables", all(
        first[name] == "created" for name in TAX_TABLES))
    second = run_migrations(report=False)
    failures += check("is idempotent on repeat", all(
        second[name] == "skipped" for name in TAX_TABLES))
    with sqlite3.connect(paths.db_path()) as conn:
        transaction_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(kl_transactions)")
        }
        settings_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(kl_tax_settings)")
        }
        units_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(kl_units_config)")
        }
    failures += check("transaction table has the requested columns", {
        "id", "wave_id", "amount_cents", "ai_category", "user_approved"
    }.issubset(transaction_columns))
    failures += check("settings table has the requested columns", {
        "filing_status", "estimated_other_income", "tax_year", "business_type"
    }.issubset(settings_columns))
    failures += check("units configuration table has the requested columns", {
        "id", "source", "manual_sp_cents", "manual_q", "manual_vc_cents",
        "target_income_cents", "date_range_days"
    }.issubset(units_columns))
    with sqlite3.connect(paths.db_path()) as conn:
        conn.execute("INSERT INTO kl_units_config (id) VALUES (1)")
        fresh_source = conn.execute(
            "SELECT source FROM kl_units_config WHERE id = 1").fetchone()[0]
        conn.execute("UPDATE kl_units_config SET source = 'auto' WHERE id = 1")
    no_shopify = run_migrations(report=False)
    with sqlite3.connect(paths.db_path()) as conn:
        manual_source = conn.execute(
            "SELECT source FROM kl_units_config WHERE id = 1").fetchone()[0]
        conn.execute(
            "CREATE TABLE shopify_orders (order_id TEXT PRIMARY KEY)"
        )
        conn.execute("INSERT INTO shopify_orders VALUES ('order-1')")
        conn.execute("UPDATE kl_units_config SET source = 'auto' WHERE id = 1")
    has_shopify = run_migrations(report=False)
    with sqlite3.connect(paths.db_path()) as conn:
        shopify_source = conn.execute(
            "SELECT source FROM kl_units_config WHERE id = 1").fetchone()[0]
    failures += check(
        "fresh and saved Planning sources cannot remain on retired Auto mode",
        fresh_source == "shopify"
        and no_shopify[PLANNING_SOURCE_RETIREMENT] == "created"
        and manual_source == "manual"
        and has_shopify[PLANNING_SOURCE_RETIREMENT] == "created"
        and shopify_source == "shopify",
    )
    startup_dir = tempfile.TemporaryDirectory(
        prefix="klearing-tax-startup-", ignore_cleanup_errors=True)
    os.environ["KLEARING_DATA_DIR"] = startup_dir.name
    db.init_db()
    with sqlite3.connect(paths.db_path()) as conn:
        startup_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    failures += check("db.init_db applies the Tax migration automatically",
                      set(TAX_TABLES).issubset(startup_tables))
    if failures:
        print(f"FAIL: {failures} Tax migration checks failed.")
        return 1
    print("PASS: Tax migration checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
