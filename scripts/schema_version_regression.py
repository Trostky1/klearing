"""Regression fixtures for versioned storage and migration recovery."""

import hashlib
import gc
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db
import main
import paths
from config import GITHUB_RELEASES_URL
from migrations.schema_versions import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_VERSION_FINGERPRINTS,
    NewerSchemaVersionError,
    StorageCompatibilityError,
    UnrecognizedLegacySchemaError,
    current_schema_version,
    ensure_current_schema,
    _stamp_version,
    structural_fingerprint,
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def _columns(conn, table):
    return {row[1]: row[2].upper()
            for row in conn.execute(f"PRAGMA table_info({table})")}


def _make_current_fixture(data_dir):
    os.environ["KLEARING_DATA_DIR"] = str(data_dir)
    db.init_db()
    check(current_schema_version() == CURRENT_SCHEMA_VERSION,
          "fresh fixture was not stamped current")


def _replace_with_legacy_money(conn, table, cents_column, legacy_column,
                               nullable=False):
    definition = "REAL" if nullable else "REAL NOT NULL DEFAULT 0"
    conn.execute(
        f"ALTER TABLE {table} ADD COLUMN {legacy_column} {definition}")
    conn.execute(
        f"UPDATE {table} SET {legacy_column} = {cents_column} / 100.0")
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {cents_column}")


def _turn_current_into_legacy_fixture(database_path):
    with closing(sqlite3.connect(database_path)) as conn, conn:
        conn.execute("DROP TABLE schema_versions")
        # Production's historical head required suggested_category, while a
        # fresh current database permits NULL. Rebuild this one table so the
        # fixture represents the real historical production schema exactly.
        conn.executescript("""
            CREATE TABLE pending_items_legacy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                vendor TEXT NOT NULL,
                amount REAL NOT NULL,
                date TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'expense',
                suggested_category TEXT NOT NULL,
                external_transaction_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                wave_transaction_id TEXT,
                business_id TEXT,
                notes TEXT,
                evidence_type TEXT,
                account_id TEXT,
                category_confidence TEXT,
                category_source TEXT,
                UNIQUE(source, external_transaction_id)
            );
            INSERT INTO pending_items_legacy (
                id, source, vendor, amount, date, direction,
                suggested_category, external_transaction_id, status,
                wave_transaction_id, business_id, notes, evidence_type,
                account_id, category_confidence, category_source)
            SELECT id, source, vendor, amount_cents / 100.0, date, direction,
                COALESCE(suggested_category, 'Uncategorized Expense'),
                external_transaction_id, status, wave_transaction_id,
                business_id, notes, evidence_type, account_id,
                category_confidence, category_source
            FROM pending_items;
            DROP TABLE pending_items;
            ALTER TABLE pending_items_legacy RENAME TO pending_items;
        """)
        replacements = {
            "reconciliation_history": (
                ("wave_balance_cents", "wave_balance", False),
                ("bank_balance_cents", "bank_balance", False),
                ("residual_cents", "residual", False),
            ),
            "sales_tax_nexus": (
                ("revenue_cents", "revenue", False),
                ("threshold_cents", "threshold", True),
            ),
            "shopify_orders": (
                ("total_price_cents", "total_price", False),
                ("subtotal_price_cents", "subtotal_price", False),
                ("total_discounts_cents", "total_discounts", False),
                ("total_tax_cents", "total_tax", False),
                ("refund_total_cents", "refund_total", False),
            ),
            "shopify_customers": (
                ("total_spent_cents", "total_spent", False),
            ),
        }
        for table, columns in replacements.items():
            for cents_column, legacy_column, nullable in columns:
                _replace_with_legacy_money(
                    conn, table, cents_column, legacy_column, nullable)
        conn.execute(
            "INSERT INTO pending_items "
            "(source, vendor, amount, date, direction, suggested_category, "
            "status) VALUES ('fixture', 'Historical fixture', 12.34, "
            "'2026-01-01', 'expense', 'Uncategorized Expense', 'denied')")
        # The real production head this fingerprint pins predates the
        # bank-account mapping-state migration (and its partial unique
        # index) by several schema versions. Building this fixture from a
        # fully-migrated current database means it otherwise carries that
        # column/index forward untouched, which the true historical head
        # never had. Strip both back out so the fixture matches the real
        # v20 bank_accounts shape exactly.
        conn.execute("DROP INDEX IF EXISTS idx_bank_accounts_ledger_unique")
        conn.execute("ALTER TABLE bank_accounts DROP COLUMN mapping_state")


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main_regression():
    original_data_dir = os.environ.get("KLEARING_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="klearing-schema-versions-") as root:
            root = Path(root)

            # Historical head fixture: v0 / fresh empty.
            fresh = root / "v0-empty"
            _make_current_fixture(fresh)
            with closing(sqlite3.connect(fresh / "klearing.db")) as conn:
                ledger = conn.execute(
                    "SELECT version, migration_name FROM schema_versions"
                ).fetchall()
            check([row[0] for row in ledger] ==
                  list(range(CURRENT_SCHEMA_VERSION + 1)),
                  "fresh migration ledger is wrong")
            check(ledger[1][1] == "legacy-core-schema",
                  "first schema migration name is wrong")
            check(not (fresh / "migration-backups").exists(),
                  "brand-new empty storage should not create a recovery copy")

            # Historical head fixture: v1 / current but predating its stamp.
            current_unstamped = root / "v1-current-unstamped"
            _make_current_fixture(current_unstamped)
            with closing(sqlite3.connect(
                    current_unstamped / "klearing.db")) as conn, conn:
                conn.execute("DROP TABLE schema_versions")
            db.init_db()
            backups = list((current_unstamped / "migration-backups").glob("*.db"))
            check(len(backups) == 1, "current unstamped fixture was not backed up")
            with closing(sqlite3.connect(backups[0])) as backup:
                check(backup.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                      "bootstrap backup is not restorable")
                check("schema_versions" not in {
                    row[0] for row in backup.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")},
                      "backup happened after the version stamp")

            # Historical head fixture: v0 / real-money production schema.
            legacy = root / "v0-legacy-real"
            _make_current_fixture(legacy)
            legacy_db = legacy / "klearing.db"
            _turn_current_into_legacy_fixture(legacy_db)
            with closing(sqlite3.connect(legacy_db)) as conn:
                legacy_fingerprint = structural_fingerprint(conn)
            check(legacy_fingerprint in LEGACY_VERSION_FINGERPRINTS,
                  f"legacy fixture fingerprint drifted: {legacy_fingerprint}")
            db.init_db()
            legacy_backups = list((legacy / "migration-backups").glob("*.db"))
            check(len(legacy_backups) == 1,
                  "legacy migration did not create exactly one backup")
            with closing(sqlite3.connect(legacy_backups[0])) as backup:
                check(backup.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                      "legacy backup failed quick_check")
                check("amount" in _columns(backup, "pending_items") and
                      "amount_cents" not in _columns(backup, "pending_items"),
                      "legacy backup was taken after money migration")
            with closing(sqlite3.connect(legacy_db)) as migrated:
                check("amount_cents" in _columns(migrated, "pending_items") and
                      "amount" not in _columns(migrated, "pending_items"),
                      "legacy fixture did not migrate to integer cents")
                stored = migrated.execute(
                    "SELECT amount_cents FROM pending_items "
                    "WHERE vendor='Historical fixture'").fetchone()[0]
                check(stored == 1234, "historical fixture amount changed")
            check(current_schema_version() == CURRENT_SCHEMA_VERSION,
                  "legacy fixture was not stamped current")

            # CI fixtures for every stamped historical head. Each fixture is
            # built to its exact version, then opened by the current app and
            # required to traverse every remaining ordered migration.
            for head in range(CURRENT_SCHEMA_VERSION + 1):
                fixture = root / f"historical-head-v{head}"
                os.environ["KLEARING_DATA_DIR"] = str(fixture)
                database_path = paths.db_path()
                _stamp_version(database_path, 0, "fixture-v0")
                plan = db._schema_migration_plan()
                for target in range(1, head + 1):
                    name, migrate = plan[target - 1]
                    migrate()
                    _stamp_version(database_path, target, name)
                db.init_db()
                check(current_schema_version() == CURRENT_SCHEMA_VERSION,
                      f"historical fixture v{head} did not reach current")
                with closing(sqlite3.connect(database_path)) as migrated:
                    versions = [row[0] for row in migrated.execute(
                        "SELECT version FROM schema_versions ORDER BY version")]
                check(versions == list(range(CURRENT_SCHEMA_VERSION + 1)),
                      f"historical fixture v{head} ledger has a gap")

            # A final migration whose result has the wrong structure must not
            # stamp itself current, even though its own SQLite work committed.
            failed = root / "failed-final-shape"
            os.environ["KLEARING_DATA_DIR"] = str(failed)
            failed_db = paths.db_path()
            _stamp_version(failed_db, 0, "fixture-v0")
            failed_plan = db._schema_migration_plan()
            for target in range(1, CURRENT_SCHEMA_VERSION):
                name, migrate = failed_plan[target - 1]
                migrate()
                _stamp_version(failed_db, target, name)

            def wrong_final_shape():
                with closing(sqlite3.connect(failed_db)) as conn, conn:
                    conn.execute("CREATE TABLE unexpected_migration_output (id)")

            failed_plan[-1] = ("bad-final-test", wrong_final_shape)
            try:
                ensure_current_schema(failed_plan)
                raise AssertionError("bad final migration shape was accepted")
            except StorageCompatibilityError:
                pass
            check(current_schema_version() == CURRENT_SCHEMA_VERSION - 1,
                  "failed migration incorrectly stamped itself current")

            # Unknown/mixed unstamped storage is refused byte-for-byte.
            unknown = root / "unknown-mixed"
            unknown.mkdir()
            unknown_db = unknown / "klearing.db"
            with closing(sqlite3.connect(unknown_db)) as conn, conn:
                conn.execute("CREATE TABLE not_klearing (value TEXT)")
                conn.execute("INSERT INTO not_klearing VALUES ('untouched')")
            before = _file_hash(unknown_db)
            os.environ["KLEARING_DATA_DIR"] = str(unknown)
            try:
                db.init_db()
                raise AssertionError("unknown unstamped schema was accepted")
            except UnrecognizedLegacySchemaError:
                pass
            check(_file_hash(unknown_db) == before,
                  "unknown unstamped refusal changed the database")
            check(not (unknown / "migration-backups").exists(),
                  "unknown schema should be refused before backup/write")

            # Simulated older app / newer schema refuses and leaves a durable
            # scheduled-run diagnostic containing the real update destination.
            newer = root / "newer-schema"
            _make_current_fixture(newer)
            newer_db = newer / "klearing.db"
            with closing(sqlite3.connect(newer_db)) as conn, conn:
                conn.execute(
                    "INSERT INTO schema_versions "
                    "(version, migration_name, app_version, applied_at) "
                    "VALUES (?, 'future-test', '999', '2099-01-01T00:00:00Z')",
                    (CURRENT_SCHEMA_VERSION + 1,),
                )
            before = _file_hash(newer_db)
            try:
                db.init_db()
                raise AssertionError("newer schema was accepted")
            except NewerSchemaVersionError as exc:
                copy = main._storage_refusal_copy(exc)
                check(GITHUB_RELEASES_URL in copy,
                      "refusal copy dead-ends without an update location")
                check("No data was changed" in copy,
                      "refusal copy omits the no-write reassurance")
            check(_file_hash(newer_db) == before,
                  "newer-schema refusal changed the database")
            check(not (newer / "migration-backups").exists(),
                  "newer schema should refuse before creating a backup")
            result = main.main(["--daily-check"])
            check(result == 2, "scheduled refusal did not exit nonzero")
            log_text = (newer / "daily.log").read_text(encoding="utf-8")
            check("NewerSchemaVersionError" in log_text,
                  "scheduled refusal was not persisted to daily.log")

            # Some legacy helpers use ``with sqlite3.connect(...)`` (which
            # commits but does not close); collect their now-unreachable local
            # connections before Windows removes the fixture directory.
            gc.collect()

        print("PASS: schema heads, strict refusal, version stamps, and "
              "Online Backup recovery are verified.")
        return 0
    finally:
        if original_data_dir is None:
            os.environ.pop("KLEARING_DATA_DIR", None)
        else:
            os.environ["KLEARING_DATA_DIR"] = original_data_dir


if __name__ == "__main__":
    raise SystemExit(main_regression())
