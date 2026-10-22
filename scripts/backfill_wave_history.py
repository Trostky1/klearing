"""One-time, guarded backfill of Klearing's approved Wave-only history.

The production decision recorded on 2026-08-19 carries forward only the 16
unambiguous rows.  The seven Purchases - Parts & Materials rows deliberately
remain Wave-only.  This script validates the whole population before writing,
uses SQLite's Online Backup API, and is safe to rerun after a complete backfill.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import paths


EXCLUDED_CATEGORY = "Purchases – Parts & Materials"
CATEGORY_ACCOUNT_CODES = {
    "Uncategorized Income": "4190",
    "Owner Investment / Drawings": "3000",
    "Uncategorized Expense": "6080",
    "Owner's Equity": "3000",
}
EXPECTED_CATEGORY_TOTALS = {
    "Uncategorized Income": (7, 797),
    "Owner Investment / Drawings": (5, 62_543),
    "Uncategorized Expense": (3, 49_524),
    "Owner's Equity": (1, 32),
    EXCLUDED_CATEGORY: (7, 17_593),
}
EXPECTED_DATE_RANGE = ("2026-06-26", "2026-07-27")
CHECKING_ACCOUNT_CODE = "1010"
CREATED_BY = "wave-history-backfill-2026-08-19"
_CENT = Decimal("0.01")


class BackfillRefused(RuntimeError):
    """The live data no longer matches the explicitly approved population."""


def _amount_cents(value):
    try:
        amount = Decimal(str(value))
        rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BackfillRefused(f"Invalid historical amount: {value!r}.") from exc
    if not amount.is_finite() or amount < 0 or rounded != amount:
        raise BackfillRefused(
            f"Historical amount is not exact non-negative cents: {value!r}.")
    return int(rounded * 100)


def _source_reference(row):
    external_id = (row["external_transaction_id"] or "").strip()
    if not external_id:
        raise BackfillRefused(
            f"Approved Wave row {row['id']} has no external transaction id.")
    return f"plaid:transaction:{external_id}"


def _approved_rows(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, source, vendor, amount, date, direction, "
        "suggested_category, external_transaction_id, wave_transaction_id "
        "FROM pending_items "
        "WHERE status = 'approved' AND wave_transaction_id IS NOT NULL "
        "ORDER BY date, id"
    ).fetchall()


def _validate_population(conn):
    rows = _approved_rows(conn)
    if len(rows) != 23:
        raise BackfillRefused(
            f"Expected exactly 23 approved Wave-only rows; found {len(rows)}.")
    dates = (rows[0]["date"], rows[-1]["date"])
    if dates != EXPECTED_DATE_RANGE:
        raise BackfillRefused(
            f"Expected date range {EXPECTED_DATE_RANGE}; found {dates}.")

    summary = {}
    seen_references = set()
    for row in rows:
        if row["source"] != "mercury":
            raise BackfillRefused(
                f"Row {row['id']} has unexpected source {row['source']!r}.")
        if not (row["wave_transaction_id"] or "").strip():
            raise BackfillRefused(f"Row {row['id']} has no Wave transaction id.")
        reference = _source_reference(row)
        if reference in seen_references:
            raise BackfillRefused(f"Duplicate source reference {reference}.")
        seen_references.add(reference)
        count, cents = summary.get(row["suggested_category"], (0, 0))
        summary[row["suggested_category"]] = (
            count + 1, cents + _amount_cents(row["amount"]))
    if summary != EXPECTED_CATEGORY_TOTALS:
        raise BackfillRefused(
            "Approved Wave population no longer matches the recorded decision: "
            f"{summary!r}.")

    included = [row for row in rows
                if row["suggested_category"] in CATEGORY_ACCOUNT_CODES]
    excluded = [row for row in rows
                if row["suggested_category"] == EXCLUDED_CATEGORY]
    if len(included) != 16 or len(excluded) != 7:
        raise BackfillRefused("Expected the approved 16/7 inclusion split.")
    return rows, included, excluded


def _reference_state(conn, rows):
    references = [_source_reference(row) for row in rows]
    placeholders = ",".join("?" for _ in references)
    entry_count = conn.execute(
        f"SELECT COUNT(*) FROM journal_entries "
        f"WHERE source_reference IN ({placeholders})", references).fetchone()[0]
    import_count = conn.execute(
        f"SELECT COUNT(*) FROM ledger_source_imports "
        f"WHERE source_reference IN ({placeholders})", references).fetchone()[0]
    return entry_count, import_count


def _accounts_by_code(conn):
    wanted = {CHECKING_ACCOUNT_CODE, *CATEGORY_ACCOUNT_CODES.values()}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT id, code FROM accounts WHERE code IN ({placeholders})",
        sorted(wanted),
    ).fetchall()
    result = {row["code"]: row["id"] for row in rows}
    missing = wanted - result.keys()
    if missing:
        raise BackfillRefused(
            "Required native accounts are missing: " + ", ".join(sorted(missing)))
    return result


def _expected_lines(row, accounts):
    cents = _amount_cents(row["amount"])
    category_id = accounts[CATEGORY_ACCOUNT_CODES[row["suggested_category"]]]
    checking_id = accounts[CHECKING_ACCOUNT_CODE]
    if row["direction"] == "expense":
        return ((category_id, cents, 0), (checking_id, 0, cents))
    if row["direction"] not in {"income", "transfer"}:
        raise BackfillRefused(
            f"Row {row['id']} has unexpected direction {row['direction']!r}.")
    return ((checking_id, cents, 0), (category_id, 0, cents))


def _verify_posted_rows(conn, included, excluded, accounts):
    included_state = _reference_state(conn, included)
    excluded_state = _reference_state(conn, excluded)
    if included_state != (16, 16):
        raise BackfillRefused(
            f"Expected 16 entries/imports after backfill; found {included_state}.")
    if excluded_state != (0, 0):
        raise BackfillRefused(
            "Excluded Parts & Materials rows acquired native entries/imports: "
            f"{excluded_state}.")

    for row in included:
        reference = _source_reference(row)
        entry = conn.execute(
            "SELECT id, date, memo, created_by FROM journal_entries "
            "WHERE source_reference = ?", (reference,)).fetchone()
        if entry is None or entry["date"] != row["date"]:
            raise BackfillRefused(f"Backfilled entry mismatch for {reference}.")
        lines = conn.execute(
            "SELECT account_id, debit_amount, credit_amount FROM journal_lines "
            "WHERE journal_entry_id = ? ORDER BY id", (entry["id"],)).fetchall()
        actual = tuple((line["account_id"], line["debit_amount"],
                        line["credit_amount"]) for line in lines)
        if actual != _expected_lines(row, accounts):
            raise BackfillRefused(f"Backfilled lines mismatch for {reference}.")


def _online_backup(conn, database_path, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_dir / f"klearing-pre-wave-backfill-{stamp}.db"
    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()

    check = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackfillRefused("The pre-backfill Online Backup failed validation.")
        if check.execute("SELECT COUNT(*) FROM pending_items").fetchone()[0] < 23:
            raise BackfillRefused("The pre-backfill Online Backup is incomplete.")
    finally:
        check.close()
    return backup_path


def run(database_path, backup_dir, apply=False):
    database_path = Path(database_path).resolve()
    backup_dir = Path(backup_dir).resolve()
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        rows, included, excluded = _validate_population(conn)
        included_state = _reference_state(conn, included)
        excluded_state = _reference_state(conn, excluded)
        if excluded_state != (0, 0):
            raise BackfillRefused(
                "Excluded rows already have native entries/imports; refusing.")
        if included_state not in {(0, 0), (16, 16)}:
            raise BackfillRefused(
                f"Partial backfill detected ({included_state}); refusing.")

        accounts = _accounts_by_code(conn)
        if included_state == (16, 16):
            _verify_posted_rows(conn, included, excluded, accounts)
            return {
                "status": "already-complete",
                "database": str(database_path),
                "backfilled": 16,
                "excluded": 7,
                "excluded_cents": 17_593,
                "backup": None,
            }
        if not apply:
            return {
                "status": "validated-dry-run",
                "database": str(database_path),
                "eligible": 16,
                "eligible_cents": 112_896,
                "excluded": 7,
                "excluded_cents": 17_593,
            }

        backup_path = _online_backup(conn, database_path, backup_dir)
        # Revalidate after the backup and immediately before obtaining the
        # write lock, so a concurrently changed population cannot slip in.
        _validate_population(conn)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if _reference_state(conn, included) != (0, 0):
                raise BackfillRefused(
                    "Source references changed after preflight; refusing.")
            created_at = datetime.now(timezone.utc).isoformat()
            for row in included:
                reference = _source_reference(row)
                cursor = conn.execute(
                    "INSERT INTO journal_entries "
                    "(date, memo, created_by, source_reference, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["date"], row["vendor"][:200], CREATED_BY,
                     reference, created_at),
                )
                entry_id = cursor.lastrowid
                conn.executemany(
                    "INSERT INTO journal_lines "
                    "(journal_entry_id, account_id, debit_amount, credit_amount) "
                    "VALUES (?, ?, ?, ?)",
                    [(entry_id, *line) for line in _expected_lines(row, accounts)],
                )
                conn.execute(
                    "INSERT INTO ledger_source_imports "
                    "(source_reference, journal_entry_id, imported_at) "
                    "VALUES (?, ?, ?)",
                    (reference, entry_id, created_at),
                )
        _verify_posted_rows(conn, included, excluded, accounts)
        return {
            "status": "backfilled",
            "database": str(database_path),
            "backfilled": 16,
            "backfilled_cents": 112_896,
            "excluded": 7,
            "excluded_cents": 17_593,
            "backup": str(backup_path),
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=paths.db_path())
    parser.add_argument(
        "--backup-dir", type=Path,
        default=REPO_ROOT.parent / "backups",
        help="Directory for the validated pre-write SQLite backup.")
    parser.add_argument(
        "--apply", action="store_true",
        help="Create the Online Backup snapshot and perform the backfill.")
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.backup_dir, args.apply), indent=2))


if __name__ == "__main__":
    main()
