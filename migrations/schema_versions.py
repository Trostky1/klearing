"""Versioned storage boundary and automatic pre-migration recovery copies.

Unstamped databases are classified only by a recognized structural
fingerprint.  Once stamped, ``schema_versions`` is the single source of truth:
each successful migration appends its version, app version, name, and time.
"""

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paths
from config import APP_VERSION


_VERSION_NAMES = (
    "fresh/version-ledger-only",
    "legacy core schema",
    "tax tables",
    "native ledger tables",
    "ledger source sync",
    "Shopify inventory costs",
    "Shopify payouts",
    "bank-account ledger routing",
    "contacts",
    "invoicing",
    "invoice matching",
    "contact pipeline",
    "recurring invoices",
    "inventory quantity events",
    "Shopify analytics history",
    "Insights widget configuration",
    "account tax categories",
    "planning source retirement",
    "bank rewards account",
    "inventory accounting fields",
    "canonical bank sources",
    "integer-cent money storage",
    "Wave duplicate-post history backfill",
    "bank account mapping state",
)
SUPPORTED_VERSION_MATRIX = dict(enumerate(_VERSION_NAMES))
CURRENT_SCHEMA_VERSION = max(SUPPORTED_VERSION_MATRIX)
MIN_SUPPORTED_SCHEMA_VERSION = min(SUPPORTED_VERSION_MATRIX)
SCHEMA_VERSION_TABLE = "schema_versions"

# Filled from known real/fresh historical heads.  The fingerprint includes the
# exact application table set, every column's name/type/nullability/key role,
# foreign keys, and named indexes.  sqlite's internal autoindexes are omitted.
LEGACY_VERSION_FINGERPRINTS = {
    "7885a1d3f36beab2e1a206c8d2563e3c9e804e06645f5b3bd9f69f1db960ab46":
        "legacy-real-money-production-head-v20",
}
CURRENT_VERSION_FINGERPRINTS = {
    "ea289a22de5703ff4556bc1835b980b38d62e64d6fd03642b368d4c63bac8f27":
        "current-fresh-integer-cents-head",
    "f47214f94cda8914c89827acc61d8d280697e353f3d2ffa73bc1c7dd5c8403ea":
        "current-upgraded-integer-cents-head",
}

_LOCK = threading.RLock()


class StorageCompatibilityError(RuntimeError):
    """Opening this database would require an unsafe schema assumption."""


class NewerSchemaVersionError(StorageCompatibilityError):
    """This app is older than the database schema."""


class UnrecognizedLegacySchemaError(StorageCompatibilityError):
    """An unstamped database does not match a known historical head."""


class MigrationBackupError(StorageCompatibilityError):
    """A recovery snapshot could not be created and validated."""


@dataclass(frozen=True)
class StorageInitialization:
    version: int
    migrated: bool
    classified_as: str
    backup_path: Path | None


def _application_tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name <> ? ORDER BY name",
        (SCHEMA_VERSION_TABLE,),
    )]


def _structural_description(conn):
    structure = []
    for table in _application_tables(conn):
        columns = sorted(
            (row[1], (row[2] or "").upper(), int(row[3]), int(row[5]))
            for row in conn.execute(f'PRAGMA table_info("{table}")'))
        foreign_keys = sorted(tuple(row[2:8]) for row in conn.execute(
            f'PRAGMA foreign_key_list("{table}")'))
        indexes = []
        for index in conn.execute(f'PRAGMA index_list("{table}")'):
            name = index[1]
            if name.startswith("sqlite_autoindex_"):
                continue
            indexed_columns = tuple(
                row[2] for row in conn.execute(f'PRAGMA index_info("{name}")'))
            indexes.append((name, int(index[2]), int(index[4]), indexed_columns))
        structure.append((table, columns, foreign_keys, sorted(indexes)))
    return structure


def structural_fingerprint(conn):
    """Stable structural digest used only to classify pre-version databases."""
    structure = _structural_description(conn)
    payload = json.dumps(structure, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _has_version_table(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone() is not None


def _read_stamped_version(conn):
    if not _has_version_table(conn):
        return None
    expected = [
        ("version", "INTEGER", 0, 1),
        ("migration_name", "TEXT", 1, 0),
        ("app_version", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ]
    actual = [(row[1], (row[2] or "").upper(), int(row[3]), int(row[5]))
              for row in conn.execute(f"PRAGMA table_info({SCHEMA_VERSION_TABLE})")]
    if actual != expected:
        raise StorageCompatibilityError(
            "The schema-version ledger is malformed; no data was changed.")
    rows = conn.execute(
        f"SELECT version FROM {SCHEMA_VERSION_TABLE} ORDER BY version"
    ).fetchall()
    if not rows:
        raise StorageCompatibilityError(
            "The schema-version ledger is empty; no data was changed.")
    versions = [row[0] for row in rows]
    if any(not isinstance(version, int) or version < 0 for version in versions):
        raise StorageCompatibilityError(
            "The schema-version ledger contains an invalid version; "
            "no data was changed.")
    if versions != sorted(set(versions)):
        raise StorageCompatibilityError(
            "The schema-version ledger contains duplicate versions; "
            "no data was changed.")
    return versions[-1]


def _classify_unstamped(conn):
    tables = _application_tables(conn)
    if not tables:
        return 0, "fresh-empty"
    fingerprint = structural_fingerprint(conn)
    if fingerprint in LEGACY_VERSION_FINGERPRINTS:
        # The real production head already contains migrations 1-20. Its only
        # remaining schema migration is integer cents (21), followed by the
        # versioned Wave guard backfill (22).
        return 20, LEGACY_VERSION_FINGERPRINTS[fingerprint]
    if fingerprint in CURRENT_VERSION_FINGERPRINTS:
        return CURRENT_SCHEMA_VERSION, CURRENT_VERSION_FINGERPRINTS[fingerprint]
    raise UnrecognizedLegacySchemaError(
        "This unstamped database does not match a recognized Klearing schema "
        f"head (fingerprint {fingerprint[:12]}); no data was changed.")


def _backup_path(from_version, to_version):
    backup_dir = paths.data_dir() / "migration-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return backup_dir / (
        f"klearing-pre-schema-v{from_version}-to-v{to_version}-{stamp}.db")


def _create_validated_online_backup(database_path, from_version, to_version):
    destination_path = _backup_path(from_version, to_version)
    try:
        source = sqlite3.connect(database_path)
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        check = sqlite3.connect(
            f"file:{destination_path.as_posix()}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationBackupError(
                    "The automatic pre-migration backup failed validation; "
                    "no migration was run.")
            # Force a schema read as a restore/open smoke check.
            check.execute(
                "SELECT name FROM sqlite_master ORDER BY name").fetchall()
        finally:
            check.close()
    except Exception as exc:
        try:
            destination_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, MigrationBackupError):
            raise
        raise MigrationBackupError(
            "Klearing could not create a valid automatic pre-migration "
            f"backup ({exc}); no migration was run.") from exc
    return destination_path


def _stamp_version(database_path, version, migration_name):
    applied_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(database_path)) as conn, conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
            "version INTEGER PRIMARY KEY, "
            "migration_name TEXT NOT NULL, "
            "app_version TEXT NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
        conn.execute(
            f"INSERT INTO {SCHEMA_VERSION_TABLE} "
            "(version, migration_name, app_version, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (version, migration_name, str(APP_VERSION), applied_at),
        )


def _assert_supported(version):
    if version > CURRENT_SCHEMA_VERSION:
        raise NewerSchemaVersionError(
            f"Database schema v{version} is newer than this app supports "
            f"(through v{CURRENT_SCHEMA_VERSION}); no data was changed.")
    if version not in SUPPORTED_VERSION_MATRIX:
        raise StorageCompatibilityError(
            f"Database schema v{version} is not supported by this app; "
            "no data was changed.")


def ensure_current_schema(migration_plan):
    """Classify, back up, migrate, and stamp the configured database.

    ``migration_plan`` is an ordered sequence of ``(name, callable)`` pairs;
    each callable advances exactly one version and is stamped only after it
    succeeds. A changed or failed step therefore cannot claim a later head.
    """
    with _LOCK:
        if len(migration_plan) != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Migration plan has {len(migration_plan)} steps; schema "
                f"matrix requires {CURRENT_SCHEMA_VERSION}.")
        database_path = Path(paths.db_path())
        existed_with_content = database_path.exists() and database_path.stat().st_size > 0
        if existed_with_content:
            try:
                with closing(sqlite3.connect(
                        f"file:{database_path.as_posix()}?mode=ro", uri=True)) as conn:
                    stamped_version = _read_stamped_version(conn)
                    if stamped_version is None:
                        source_version, classification = _classify_unstamped(conn)
                    else:
                        source_version = stamped_version
                        classification = f"stamped-v{source_version}"
            except StorageCompatibilityError:
                raise
            except sqlite3.DatabaseError as exc:
                raise UnrecognizedLegacySchemaError(
                    f"The database is not a readable recognized Klearing "
                    f"database ({exc}); no data was changed.") from exc
        else:
            source_version, classification = 0, "fresh-empty"
            stamped_version = None

        _assert_supported(source_version)
        if stamped_version == CURRENT_SCHEMA_VERSION:
            return StorageInitialization(
                CURRENT_SCHEMA_VERSION, False, classification, None)

        backup_path = None
        if existed_with_content:
            backup_path = _create_validated_online_backup(
                database_path, source_version, CURRENT_SCHEMA_VERSION)

        migrated = source_version < CURRENT_SCHEMA_VERSION
        if stamped_version is None:
            initial_name = (
                "bootstrap-fresh-v0" if source_version == 0 else
                f"bootstrap-{classification}")
            _stamp_version(database_path, source_version, initial_name)

        if migrated:
            for target_version in range(source_version + 1,
                                        CURRENT_SCHEMA_VERSION + 1):
                migration_name, migrate = migration_plan[target_version - 1]
                migrate()
                if target_version == CURRENT_SCHEMA_VERSION:
                    with closing(sqlite3.connect(
                            f"file:{database_path.as_posix()}?mode=ro",
                            uri=True)) as conn:
                        fingerprint = structural_fingerprint(conn)
                    if fingerprint not in CURRENT_VERSION_FINGERPRINTS:
                        raise StorageCompatibilityError(
                            "Migration finished with an unrecognized schema "
                            f"shape; restore {backup_path or 'the '
                            'pre-migration backup'}. The database was not "
                            "stamped current.")
                _stamp_version(database_path, target_version, migration_name)

        return StorageInitialization(
            CURRENT_SCHEMA_VERSION, migrated, classification, backup_path)


def current_schema_version(database_path=None):
    """Read the ledger without initializing or modifying the database."""
    database_path = Path(database_path or paths.db_path())
    with closing(sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro", uri=True)) as conn:
        return _read_stamped_version(conn)
