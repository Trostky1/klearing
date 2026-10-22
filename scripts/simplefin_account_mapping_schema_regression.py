"""Regression coverage for the bank_accounts mapping_state column.

Task 4 builds the actual "map this account or exclude it" screen; this task
is DB-layer only. ``mapping_state`` never affects posting -- posting is
still governed entirely by ``ledger_account_id IS NULL`` (see
``db.ensure_bank_ledger_account`` and ``ledger_sync.SkipReason
.UNMAPPED_BANK_ACCOUNT``, both untouched here). ``mapping_state`` only lets
the future mapping screen tell "no answer yet" (``''``) apart from
"deliberately excluded" (``'excluded'``).
"""

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-account-mapping-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import ledger  # noqa: E402
import paths  # noqa: E402
import migrations.run_migrations as run_migrations_module  # noqa: E402
from migrations.run_migrations import (  # noqa: E402
    BANK_ACCOUNT_MAPPING_STATE_FIELD,
    run_migrations,
)
from migrations.schema_versions import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    CURRENT_VERSION_FINGERPRINTS,
    StorageCompatibilityError,
    _stamp_version,
    current_schema_version,
    structural_fingerprint,
)


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    print(("  PASS  " if condition else "  FAIL  ") + label)
    failures += 0 if condition else 1


def account(row):
    """One bank_accounts fixture, source/account_id/name required by the
    table; other fields default the way a real adapter sync would leave
    an unmapped account (blank type/subtype, no mask)."""
    db.upsert_bank_account({
        "type": "", "subtype": "", "mask": None,
        "official_name": None, "synced_at": "2026-08-21T00:00:00Z",
        **row,
    })


def _revert_bank_accounts_to_pre_mapping_state_shape():
    """Simulate an install that predates this migration: no mapping_state
    column, no partial unique index. Used by the two isolated checks below
    that need to run the migration a first time from that starting point."""
    with db.get_connection() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_bank_accounts_ledger_unique")
        conn.execute("ALTER TABLE bank_accounts DROP COLUMN mapping_state")


def _in_isolated_data_dir(prefix, body):
    """Run ``body()`` against a brand-new KLEARING_DATA_DIR, then restore
    whatever directory was active before. Keeps these two fixtures (which
    deliberately mutate bank_accounts' shape mid-test) from disturbing the
    shared database the rest of this script's checks build up."""
    original = os.environ.get("KLEARING_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(
                prefix=prefix, ignore_cleanup_errors=True) as tmp:
            os.environ["KLEARING_DATA_DIR"] = tmp
            body()
    finally:
        if original is None:
            os.environ.pop("KLEARING_DATA_DIR", None)
        else:
            os.environ["KLEARING_DATA_DIR"] = original


def check_created_path_is_observable():
    """Issue 8: the idempotency check elsewhere in this file only ever
    observes 'skipped', because by the time it runs, db.init_db() already
    added the column. Prove the 'created' status genuinely fires the first
    time a database that predates this migration is brought forward."""
    db.init_db()
    _revert_bank_accounts_to_pre_mapping_state_shape()
    created = run_migrations(report=False)
    again = run_migrations(report=False)
    check("the mapping_state migration reports 'created' the first time it "
          "actually adds the column",
          created[BANK_ACCOUNT_MAPPING_STATE_FIELD] == "created")
    check("...and 'skipped' on every run after that",
          again[BANK_ACCOUNT_MAPPING_STATE_FIELD] == "skipped")


def check_duplicate_mapping_upgrade_guard():
    """Critical fix, driven via the plain run_migrations() reporting
    function: an install that reached "two bank accounts mapped to the
    same Ledger account" under the OLD rules (nothing forbade it) must get
    a readable refusal naming the collision, not a raw
    sqlite3.IntegrityError escaping mid-migration with the database stuck
    one version short and no explanation. On THIS call path, run_migrations()
    already wraps every migration in one connection-wide transaction, so a
    DML statement from an earlier (already-applied, no-op) migration may
    have opened an implicit transaction before this one runs -- that is why
    a real rollback is provable here even without this migration's own
    explicit BEGIN/COMMIT/ROLLBACK. See
    check_duplicate_mapping_upgrade_guard_real_path() below for proof the
    rollback is *also* real on the path that does NOT get that lucky
    implicit transaction for free."""
    db.init_db()
    dup_target = ledger.create_account(
        "Test Duplicate Mapping Target", "asset", parent_code="1000")
    account({"source": "plaid", "account_id": "legacy-dup-a",
              "name": "Legacy Dup A"})
    account({"source": "simplefin", "account_id": "legacy-dup-b",
              "name": "Legacy Dup B"})
    _revert_bank_accounts_to_pre_mapping_state_shape()
    with db.get_connection() as conn:
        # With the unique index gone, this state -- two different bank
        # identities pointing at the same Ledger account -- is reachable
        # again, exactly as it always was before this migration existed.
        conn.execute(
            "UPDATE bank_accounts SET ledger_account_id = ? "
            "WHERE (source, account_id) IN "
            "(('plaid', 'legacy-dup-a'), ('simplefin', 'legacy-dup-b'))",
            (dup_target["id"],),
        )

    raised = None
    try:
        run_migrations(report=False)
    except StorageCompatibilityError as exc:
        raised = exc
    except sqlite3.IntegrityError:
        raised = "raw-sqlite-integrity-error"

    check("a pre-existing duplicate mapping raises a readable "
          "StorageCompatibilityError instead of a raw sqlite3.IntegrityError "
          "(run_migrations() path)",
          isinstance(raised, StorageCompatibilityError))
    message = str(raised) if isinstance(raised, StorageCompatibilityError) else ""
    check("the refusal names the actual offending Ledger account id",
          str(dup_target["id"]) in message)
    check("the refusal names both colliding (source, account_id) pairs",
          "plaid:legacy-dup-a" in message and "simplefin:legacy-dup-b" in message)
    with db.get_connection() as conn:
        columns_after = {row[1] for row in
                         conn.execute("PRAGMA table_info(bank_accounts)")}
    check("the refused migration left no partially-added mapping_state "
          "column behind on the run_migrations() path",
          "mapping_state" not in columns_after)


def check_duplicate_mapping_upgrade_guard_real_path():
    """Same scenario, but driven through the REAL production path:
    db.init_db() -> db._schema_migration_plan() ->
    apply_bank_account_mapping_state(), which opens a fresh connection and
    issues only DDL (ALTER TABLE / CREATE INDEX) with no preceding DML. Under
    Python sqlite3's default isolation_level, DDL is never wrapped in an
    implicit transaction the way DML is -- so without an explicit
    BEGIN/COMMIT/ROLLBACK inside _ensure_bank_account_mapping_state itself,
    the ALTER TABLE ADD COLUMN would autocommit before the duplicate check
    even ran, leaving a stray column behind on refusal. This proves that gap
    is closed: the column is genuinely absent afterward, the database stays
    correctly stamped at its old version (not corrupted), and fixing the
    duplicate and retrying self-heals to a fully current, correctly
    classified database."""
    db.init_db()
    before_version = current_schema_version()
    dup_target = ledger.create_account(
        "Test Duplicate Mapping Target (real path)", "asset",
        parent_code="1000")
    account({"source": "plaid", "account_id": "real-dup-a",
              "name": "Real Dup A"})
    account({"source": "simplefin", "account_id": "real-dup-b",
              "name": "Real Dup B"})
    _revert_bank_accounts_to_pre_mapping_state_shape()
    db_path = paths.db_path()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE bank_accounts SET ledger_account_id = ? "
            "WHERE (source, account_id) IN "
            "(('plaid', 'real-dup-a'), ('simplefin', 'real-dup-b'))",
            (dup_target["id"],),
        )
        # Roll the schema_versions ledger back one step, so this database is
        # a faithful stand-in for a real install that has not yet received
        # the mapping_state migration -- exactly what db.init_db() would
        # walk into on next launch.
        conn.execute(
            "DELETE FROM schema_versions WHERE version = ?",
            (before_version,),
        )

    raised = None
    try:
        db.init_db()
    except StorageCompatibilityError as exc:
        raised = exc
    except sqlite3.IntegrityError:
        raised = "raw-sqlite-integrity-error"

    check("the real db.init_db()/_schema_migration_plan() path also raises "
          "a readable StorageCompatibilityError, not a raw "
          "sqlite3.IntegrityError",
          isinstance(raised, StorageCompatibilityError))
    message = str(raised) if isinstance(raised, StorageCompatibilityError) else ""
    check("the real-path refusal also names the offending Ledger account id",
          str(dup_target["id"]) in message)
    check("the real-path refusal also names both colliding pairs",
          "plaid:real-dup-a" in message and "simplefin:real-dup-b" in message)
    with closing(sqlite3.connect(db_path)) as conn:
        columns_after = {row[1] for row in
                         conn.execute("PRAGMA table_info(bank_accounts)")}
    check("on the REAL path, the refused migration left no partially-added "
          "mapping_state column behind either -- the explicit transaction "
          "inside _ensure_bank_account_mapping_state actually rolled back",
          "mapping_state" not in columns_after)
    check("the database was not silently advanced past its true version "
          "while refused",
          current_schema_version() == before_version - 1)

    # Fix the duplicate and confirm a retry self-heals cleanly.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE bank_accounts SET ledger_account_id = NULL "
            "WHERE source = 'simplefin' AND account_id = 'real-dup-b'"
        )
    db.init_db()
    check("fixing the duplicate and retrying self-heals to the current "
          "schema version",
          current_schema_version() == CURRENT_SCHEMA_VERSION)
    with closing(sqlite3.connect(db_path)) as conn:
        columns_final = {row[1] for row in
                         conn.execute("PRAGMA table_info(bank_accounts)")}
        indexes_final = [row[1] for row in
                         conn.execute("PRAGMA index_list(bank_accounts)")]
    check("the self-healed database has mapping_state and the unique index",
          "mapping_state" in columns_final
          and "idx_bank_accounts_ledger_unique" in indexes_final)


def check_migration_plan_misplacement_guard():
    """Issue 2 fix verification: _schema_migration_plan()'s full-sequence
    assertion must catch a step added to versioned_migration_steps()'s
    post_migrations tuple (the wrong home) even when a _VERSION_NAMES bump
    keeps the total step count matching CURRENT_SCHEMA_VERSION -- which is
    exactly the realistic mistake the two-positional-asserts version of
    this guard failed to catch, since a step inserted there leaves
    "wave-duplicate-post-history" and "bank-account-mapping-state" exactly
    where they already were (still last, still second-to-last)."""
    original_versioned_steps = run_migrations_module.versioned_migration_steps

    def _steps_with_a_misplaced_addition():
        # Simulates exactly the mistake the guard exists to catch: a new
        # migration appended to post_migrations (by returning it from
        # versioned_migration_steps(), which is what that tuple feeds)
        # instead of to _schema_migration_plan()'s own list.
        steps = list(original_versioned_steps())
        steps.append(("wrongly-placed-new-migration", lambda: None))
        return steps

    run_migrations_module.versioned_migration_steps = (
        _steps_with_a_misplaced_addition)
    try:
        misplacement_caught = False
        try:
            db._schema_migration_plan()
        except AssertionError:
            misplacement_caught = True
        check("a migration wrongly appended to post_migrations trips the "
              "full-sequence positional guard, even though it leaves the "
              "last two step names unchanged",
              misplacement_caught)
    finally:
        run_migrations_module.versioned_migration_steps = (
            original_versioned_steps)

    # And the "no false alarm" side: with nothing injected, the real,
    # correctly-ordered plan (which is the same shape as this repository's
    # own _EXPECTED_SCHEMA_MIGRATION_STEP_NAMES, updated in lockstep any
    # time a step is legitimately appended) must NOT trip the guard.
    no_false_alarm = True
    try:
        plan = db._schema_migration_plan()
    except AssertionError:
        no_false_alarm = False
        plan = []
    check("the real, unmodified schema migration plan does not false-alarm "
          "the positional guard",
          no_false_alarm)
    check("the real plan has one step per schema version, ending with this "
          "task's own two fixed steps in order",
          [name for name, _step in plan][-2:] ==
          ["wave-duplicate-post-history", "bank-account-mapping-state"])


def run():
    db.init_db()

    print("Fresh schema shape")
    with closing(sqlite3.connect(paths.db_path())) as conn:
        columns = {row[1]: row for row in
                   conn.execute("PRAGMA table_info(bank_accounts)")}
    check("mapping_state column exists on a fresh database",
          "mapping_state" in columns)
    check("mapping_state defaults to '' at the column level",
          columns["mapping_state"][4] == "''")
    check("mapping_state is NOT NULL",
          columns["mapping_state"][3] == 1)

    account({"source": "simplefin", "account_id": "sf-fresh",
              "name": "Fresh SimpleFIN Account"})
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-fresh'"
        ).fetchone()
    check("a fresh upsert_bank_account insert gets mapping_state = '' for free",
          row["mapping_state"] == "")
    check("a fresh upsert_bank_account insert leaves ledger_account_id NULL",
          row["ledger_account_id"] is None)

    print("Idempotent migration chain (already-migrated database)")
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    check("every migration reports 'skipped' on an already-migrated "
          "database (repeat 1)",
          all(status == "skipped" for status in first.values()))
    check("every migration reports 'skipped' on an already-migrated "
          "database (repeat 2)",
          all(status == "skipped" for status in second.values()))

    print("Schema-version fingerprint")
    with closing(sqlite3.connect(paths.db_path())) as conn:
        fingerprint = structural_fingerprint(conn)
    check("the freshly migrated database matches a known current fingerprint",
          fingerprint in CURRENT_VERSION_FINGERPRINTS)
    check("the database is classified at the new CURRENT_SCHEMA_VERSION",
          current_schema_version() == CURRENT_SCHEMA_VERSION)

    print("list_bank_accounts")
    account({"source": "simplefin", "account_id": "sf-2",
              "name": "Zeta SimpleFIN Account"})
    account({"source": "simplefin", "account_id": "sf-3",
              "name": "Alpha SimpleFIN Account"})
    account({"source": "simplefin", "account_id": "sf-lower",
              "name": "bravo lowercase simplefin account"})
    account({"source": "plaid", "account_id": "plaid-1",
              "name": "Plaid Only Account"})
    simplefin_rows = db.list_bank_accounts("simplefin")
    check("list_bank_accounts returns only the requested source",
          {row["source"] for row in simplefin_rows} == {"simplefin"}
          and len(simplefin_rows) == 4)
    check("list_bank_accounts excludes other sources",
          "plaid-1" not in {row["account_id"] for row in simplefin_rows})
    names = [row["name"] for row in simplefin_rows]
    # Deliberately compared against a case-insensitive sort, not Python's
    # binary sorted(): a lowercase-initial name ("bravo...") belongs
    # alphabetically between "Alpha" and "Fresh", not after every
    # uppercase-initial name the way BINARY collation would place it.
    # This is the case that would stay green even if the ORDER BY's
    # COLLATE NOCASE were silently reverted to a binary sort.
    check("list_bank_accounts orders case-insensitively, not just stably",
          names == sorted(names, key=str.lower))
    check("list_bank_accounts returns every column",
          set(simplefin_rows[0].keys()) >= {
              "source", "account_id", "name", "official_name", "type",
              "subtype", "mask", "ledger_account_id", "synced_at",
              "mapping_state",
          })

    print("Partial unique index tolerates unmapped (NULL) accounts")
    with db.get_connection() as conn:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM bank_accounts WHERE ledger_account_id IS NULL"
        ).fetchone()[0]
    check("many accounts can stay unmapped (NULL) at once without "
          "tripping the partial unique index",
          null_count >= 4)

    print("mapping_state CHECK constraint")
    check_rejected = False
    try:
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE bank_accounts SET mapping_state = 'bogus' "
                "WHERE source = 'simplefin' AND account_id = 'sf-fresh'"
            )
    except sqlite3.IntegrityError:
        check_rejected = True
    check("the CHECK constraint rejects any mapping_state besides "
          "'' or 'excluded'",
          check_rejected)
    with db.get_connection() as conn:
        untouched = conn.execute(
            "SELECT mapping_state FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-fresh'"
        ).fetchone()["mapping_state"]
    check("the rejected write left mapping_state at its prior value",
          untouched == "")

    print("exclude_bank_account")
    test_mapped_target = ledger.create_account(
        "Test Mapped Target", "asset", parent_code="1000")
    other_target = ledger.create_account(
        "Test Other Target", "asset", parent_code="1000")
    third_target = ledger.create_account(
        "Test Third Target", "asset", parent_code="1000")
    test_mapped_target_id = test_mapped_target["id"]
    other_target_id = other_target["id"]
    third_target_id = third_target["id"]

    db.set_bank_ledger_account("simplefin", "sf-2", test_mapped_target_id)
    excluded_existing = db.exclude_bank_account("simplefin", "sf-2")
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-2'"
        ).fetchone()
    check("exclude_bank_account returns True for an existing row",
          excluded_existing is True)
    check("exclude_bank_account sets mapping_state to 'excluded'",
          row["mapping_state"] == "excluded")
    check("exclude_bank_account clears a stale ledger_account_id to NULL",
          row["ledger_account_id"] is None)

    excluded_missing = db.exclude_bank_account("simplefin", "does-not-exist")
    check("exclude_bank_account returns False for a nonexistent row",
          excluded_missing is False)

    print("set_bank_ledger_account supersedes an exclusion")
    db.set_bank_ledger_account("simplefin", "sf-2", other_target_id)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-2'"
        ).fetchone()
    check("mapping a real ledger account resets mapping_state back to ''",
          row["mapping_state"] == "")
    check("mapping a real ledger account sets ledger_account_id",
          row["ledger_account_id"] == other_target_id)

    print("set_bank_ledger_account refuses a collision instead of raising")
    account({"source": "simplefin", "account_id": "sf-dup-a",
              "name": "Dup A"})
    account({"source": "simplefin", "account_id": "sf-dup-b",
              "name": "Dup B"})
    mapped_a = db.set_bank_ledger_account("simplefin", "sf-dup-a", third_target_id)
    collision_result = db.set_bank_ledger_account(
        "simplefin", "sf-dup-b", third_target_id)
    check("the first mapping to a fresh target succeeds",
          mapped_a is True)
    check("set_bank_ledger_account returns False on a collision rather "
          "than raising sqlite3.IntegrityError",
          collision_result is False)
    with db.get_connection() as conn:
        untouched = conn.execute(
            "SELECT ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-dup-b'"
        ).fetchone()["ledger_account_id"]
    check("the refused collision did not partially apply",
          untouched is None)

    print("taken_ledger_account_ids")
    taken = db.taken_ledger_account_ids()
    check("taken_ledger_account_ids reports every currently-mapped "
          "Ledger account across sources",
          taken.get(other_target_id) == ("simplefin", "sf-2")
          and taken.get(third_target_id) == ("simplefin", "sf-dup-a"))
    check("taken_ledger_account_ids omits a Ledger account nobody has "
          "claimed yet",
          third_target_id + 999999 not in taken)

    print("Partial unique index: same-source collision (raw write)")
    same_source_rejected = False
    try:
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE bank_accounts SET ledger_account_id = ? "
                "WHERE source = 'simplefin' AND account_id = 'sf-dup-b'",
                (third_target_id,),
            )
    except sqlite3.IntegrityError:
        same_source_rejected = True
    check("two SimpleFIN accounts cannot map to the same ledger account",
          same_source_rejected)

    print("Partial unique index: cross-source collision (raw write)")
    account({"source": "plaid", "account_id": "plaid-dup",
              "name": "Plaid Dup"})
    account({"source": "simplefin", "account_id": "sf-cross",
              "name": "SF Cross"})
    db.set_bank_ledger_account("plaid", "plaid-dup", test_mapped_target_id)
    cross_source_rejected = False
    try:
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE bank_accounts SET ledger_account_id = ? "
                "WHERE source = 'simplefin' AND account_id = 'sf-cross'",
                (test_mapped_target_id,),
            )
    except sqlite3.IntegrityError:
        cross_source_rejected = True
    check("a Plaid mapping and a SimpleFIN mapping cannot share a ledger account",
          cross_source_rejected)

    print("Re-sync survives an existing mapping/exclusion decision")
    account({"source": "simplefin", "account_id": "sf-2",
              "name": "Zeta SimpleFIN Account", "official_name": "Renamed"})
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id, official_name "
            "FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-2'"
        ).fetchone()
    check("a repeat sync updates ordinary metadata",
          row["official_name"] == "Renamed")
    check("a repeat sync does not reset an already-set mapping_state",
          row["mapping_state"] == "")
    check("a repeat sync does not reset an already-set ledger_account_id",
          row["ledger_account_id"] == other_target_id)

    account({"source": "simplefin", "account_id": "sf-dup-b",
              "name": "Dup B", "official_name": "Still unmapped"})
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-dup-b'"
        ).fetchone()
    check("a repeat sync of a never-mapped account leaves it unmapped, not excluded",
          row["mapping_state"] == "" and row["ledger_account_id"] is None)

    db.exclude_bank_account("simplefin", "sf-dup-b")
    account({"source": "simplefin", "account_id": "sf-dup-b",
              "name": "Dup B", "official_name": "Re-synced after exclusion"})
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT mapping_state, ledger_account_id FROM bank_accounts "
            "WHERE source = 'simplefin' AND account_id = 'sf-dup-b'"
        ).fetchone()
    check("a repeat sync does not silently un-exclude an excluded account",
          row["mapping_state"] == "excluded" and row["ledger_account_id"] is None)

    print("Isolated: 'created' status is genuinely observable (issue 8)")
    _in_isolated_data_dir(
        "klearing-mapping-created-", check_created_path_is_observable)

    print("Isolated: pre-existing duplicate mapping upgrade guard "
          "(critical fix, run_migrations() path)")
    _in_isolated_data_dir(
        "klearing-mapping-dup-guard-", check_duplicate_mapping_upgrade_guard)

    print("Isolated: pre-existing duplicate mapping upgrade guard "
          "(critical fix, REAL db.init_db() path -- proves the rollback "
          "claim true where it previously wasn't)")
    _in_isolated_data_dir(
        "klearing-mapping-dup-guard-real-",
        check_duplicate_mapping_upgrade_guard_real_path)

    print("Migration-plan misplacement guard (issue 2 fix)")
    _in_isolated_data_dir(
        "klearing-mapping-misplacement-",
        check_migration_plan_misplacement_guard)

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN account-mapping "
              "schema checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN account-mapping schema "
          "checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
