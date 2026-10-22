"""Publish a local, readable copy of Klearing's canonical books.

Ledger writes only mark this publication dirty.  Building the files is an
explicit, live-user action because a full book can take a couple of seconds.
The marker lives in SQLite so a restart cannot accidentally make a stale
publication look current.
"""

import csv
import hashlib
import html
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import paths


DIRTY_KEY = "continuity_artifact_dirty"
PUBLISHED_KEY = "continuity_artifact_last_published"
ROOT_NAME = "My Klearing Book"
START_FILE = "00-START-HERE.html"
GENERATION_PREFIX = "Book records - updated "


class ContinuityArtifactError(RuntimeError):
    """A user-facing continuity-publication failure."""


def _utc_now():
    return datetime.now(timezone.utc)


def mark_dirty(conn=None, reason="canonical-ledger-changed"):
    """Persist a new dirty token, optionally inside the caller's transaction."""
    token = json.dumps({
        "marked_at": _utc_now().isoformat(),
        "reason": reason,
        "token": uuid.uuid4().hex,
    }, separators=(",", ":"))
    owns_connection = conn is None
    if owns_connection:
        conn = sqlite3.connect(paths.db_path())
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (DIRTY_KEY, token),
        )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()
    return token


def dirty_marker(conn=None):
    owns_connection = conn is None
    if owns_connection:
        conn = sqlite3.connect(paths.db_path())
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (DIRTY_KEY,)
        ).fetchone()
        return row[0] if row else None
    finally:
        if owns_connection:
            conn.close()


def _money(cents):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _csv_text(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def _snapshot():
    """Copy one consistent view into memory, then release the database."""
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        marker_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (DIRTY_KEY,)
        ).fetchone()
        accounts = [dict(row) for row in conn.execute(
            "SELECT a.id, a.code, a.name, a.type, a.parent_account_id, "
            "a.tax_category, COALESCE(SUM(jl.debit_amount), 0) debit_cents, "
            "COALESCE(SUM(jl.credit_amount), 0) credit_cents "
            "FROM accounts a LEFT JOIN journal_lines jl ON jl.account_id=a.id "
            "GROUP BY a.id ORDER BY CAST(a.code AS INTEGER), a.code"
        )]
        entries = [dict(row) for row in conn.execute(
            "SELECT id, date, memo, created_by, source_reference, created_at "
            "FROM journal_entries ORDER BY date, id"
        )]
        lines = [dict(row) for row in conn.execute(
            "SELECT jl.id, jl.journal_entry_id, jl.account_id, a.code account_code, "
            "a.name account_name, jl.debit_amount debit_cents, "
            "jl.credit_amount credit_cents "
            "FROM journal_lines jl JOIN accounts a ON a.id=jl.account_id "
            "ORDER BY jl.journal_entry_id, jl.id"
        )]
        conn.commit()
    finally:
        conn.close()
    return {
        "dirty_token": marker_row[0] if marker_row else None,
        "accounts": accounts,
        "entries": entries,
        "lines": lines,
    }


def _style():
    return """
body{font:16px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;color:#19211d;
max-width:1040px;margin:0 auto;padding:36px 28px;background:#fbfaf6}
h1,h2{line-height:1.18} .asof{background:#e8f2ed;border-left:5px solid #2e6b55;
padding:16px 18px;margin:22px 0}.note{color:#53615a}.cards{display:flex;gap:16px;
flex-wrap:wrap}.card{background:white;border:1px solid #d9ddd9;border-radius:10px;
padding:14px 18px;min-width:180px}table{width:100%;border-collapse:collapse;
background:white}th,td{padding:9px 10px;border-bottom:1px solid #e1e4e1;text-align:left}
th{background:#eef1ee}.money{text-align:right;font-variant-numeric:tabular-nums}
a{color:#155a44}.entry{background:white;border:1px solid #d9ddd9;border-radius:10px;
padding:14px 18px;margin:16px 0}.entry h3{margin:0 0 5px}.source{word-break:break-word}
"""


def _page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width'><title>{html.escape(title)}</title>"
            f"<style>{_style()}</style></head><body>{body}</body></html>")


def _write_text(root, relative, text):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _build_generation(build_dir, snapshot, generated_at):
    accounts = snapshot["accounts"]
    entries = snapshot["entries"]
    lines = snapshot["lines"]
    lines_by_entry = defaultdict(list)
    for line in lines:
        lines_by_entry[line["journal_entry_id"]].append(line)

    generated_label = generated_at.astimezone().strftime("%B %-d, %Y at %-I:%M %p %Z") if os.name != "nt" else generated_at.astimezone().strftime("%B %#d, %Y at %#I:%M %p %Z")
    generated_iso = generated_at.isoformat()
    latest_date = entries[-1]["date"] if entries else "no posted entries yet"

    account_rows = []
    total_debits = total_credits = 0
    for account in accounts:
        debit = int(account["debit_cents"])
        credit = int(account["credit_cents"])
        total_debits += debit
        total_credits += credit
        normal = debit - credit if account["type"] in ("asset", "expense") else credit - debit
        account_rows.append(
            f"<tr><td>{html.escape(account['code'])}</td><td>{html.escape(account['name'])}</td>"
            f"<td>{html.escape(account['type'].title())}</td><td class='money'>{_money(debit)}</td>"
            f"<td class='money'>{_money(credit)}</td><td class='money'>{_money(normal)}</td></tr>"
        )

    years = defaultdict(list)
    for entry in entries:
        years[(entry["date"] or "Unknown")[:4]].append(entry)
    year_links = []
    for year, year_entries in sorted(years.items(), reverse=True):
        safe_year = "".join(ch for ch in year if ch.isdigit()) or "unknown"
        year_links.append(f"<li><a href='years/{safe_year}.html'>{html.escape(year)}</a> — {len(year_entries):,} posted entries</li>")
        entry_blocks = []
        for entry in year_entries:
            line_rows = []
            for line in lines_by_entry[entry["id"]]:
                line_rows.append(
                    f"<tr><td>{html.escape(line['account_code'])} — {html.escape(line['account_name'])}</td>"
                    f"<td class='money'>{_money(line['debit_cents']) if line['debit_cents'] else '—'}</td>"
                    f"<td class='money'>{_money(line['credit_cents']) if line['credit_cents'] else '—'}</td></tr>"
                )
            source = html.escape(entry["source_reference"] or "None recorded")
            entry_blocks.append(
                f"<section class='entry'><h3>{html.escape(entry['date'])} — {html.escape(entry['memo'])}</h3>"
                f"<div class='note source'>Source reference: {source}</div>"
                "<table><thead><tr><th>Account</th><th class='money'>Debit</th>"
                f"<th class='money'>Credit</th></tr></thead><tbody>{''.join(line_rows)}</tbody></table></section>"
            )
        year_body = (
            f"<h1>{html.escape(year)} posted journal</h1>"
            f"<div class='asof'><strong>Book record last updated:</strong> {html.escape(generated_label)}<br>"
            f"Reflects posted entries through <strong>{html.escape(latest_date)}</strong>.</div>"
            "<p><a href='../index.html'>← Back to the book overview</a></p>" + "".join(entry_blocks)
        )
        _write_text(build_dir, f"years/{safe_year}.html", _page(f"{year} Klearing book", year_body))

    overview = (
        "<h1>Your Klearing Book</h1>"
        f"<div class='asof'><strong>Book record last updated:</strong> {html.escape(generated_label)}<br>"
        f"This readable record reflects posted entries through <strong>{html.escape(latest_date)}</strong>.</div>"
        "<p>This folder is a readable, offline copy of the canonical books recorded in Klearing: "
        "the chart of accounts, posted journal, balances, source references, and accountant-ready CSV files. "
        "Pending or denied items, drafts, credentials, connection details, CRM data, analytics, and app settings are intentionally not part of the books.</p>"
        "<p class='note'>If Klearing recorded newer activity after the time above, open <strong>My Klearing Book</strong> "
        "inside the app to publish a current copy. The timestamp is the authority on how current these files are.</p>"
        f"<div class='cards'><div class='card'><strong>{len(entries):,}</strong><br>posted entries</div>"
        f"<div class='card'><strong>{len(lines):,}</strong><br>journal lines</div>"
        f"<div class='card'><strong>{len(accounts):,}</strong><br>accounts</div></div>"
        "<h2>Balances by account</h2><table><thead><tr><th>Code</th><th>Account</th><th>Type</th>"
        "<th class='money'>Debits</th><th class='money'>Credits</th><th class='money'>Normal balance</th>"
        f"</tr></thead><tbody>{''.join(account_rows)}</tbody></table>"
        f"<p><strong>Control total:</strong> {_money(total_debits)} debits and {_money(total_credits)} credits.</p>"
        f"<h2>Yearly records</h2><ul>{''.join(year_links) or '<li>No posted entries yet.</li>'}</ul>"
        "<h2>Files for an accountant</h2><ul>"
        "<li><a href='accountant/accounts.csv'>Chart of accounts and balances (CSV)</a></li>"
        "<li><a href='accountant/journal_entries.csv'>Journal entry headers (CSV)</a></li>"
        "<li><a href='accountant/journal_lines.csv'>Journal entry lines in integer cents (CSV)</a></li></ul>"
        "<h2>What possession of this folder means</h2>"
        "<p>These files were generated locally so you can keep and open your books without Klearing. "
        "Seeing the folder proves that you possess this copy. It does not, by itself, prove whether any software has transmitted data. "
        "Klearing does not transmit this continuity artifact anywhere.</p>"
        "<p class='note'><a href='ABOUT-THIS-BOOK.txt'>Read the plain-language file guide</a></p>"
    )
    _write_text(build_dir, "index.html", _page("Your Klearing Book", overview))
    _write_text(build_dir, "ABOUT-THIS-BOOK.txt", (
        "ABOUT THIS KLEARING BOOK\n\n"
        f"Book record last updated: {generated_label}\n"
        f"Generated timestamp: {generated_iso}\n"
        f"Reflects posted entries through: {latest_date}\n\n"
        "Start with index.html. It explains the balances and links to a readable journal for each year.\n"
        "The accountant folder contains ordinary CSV files that spreadsheet and accounting software can open.\n\n"
        "This copy includes only the canonical books. It deliberately excludes pending or denied items, CRM data, drafts, analytics, credentials, AI memory, connection caches, and operational settings.\n\n"
        "These files were generated locally. Possessing the folder proves possession of this copy; it cannot prove absence of transmission by software. Klearing does not transmit this artifact anywhere.\n"
    ))
    _write_text(build_dir, "accountant/accounts.csv", _csv_text(
        ["account_id", "code", "name", "type", "parent_account_id", "tax_category", "debit_cents", "credit_cents", "normal_balance_cents"],
        [(a["id"], a["code"], a["name"], a["type"], a["parent_account_id"], a["tax_category"], a["debit_cents"], a["credit_cents"],
          int(a["debit_cents"]) - int(a["credit_cents"]) if a["type"] in ("asset", "expense") else int(a["credit_cents"]) - int(a["debit_cents"])) for a in accounts],
    ))
    _write_text(build_dir, "accountant/journal_entries.csv", _csv_text(
        ["journal_entry_id", "date", "memo", "created_by", "source_reference", "created_at"],
        [(e["id"], e["date"], e["memo"], e["created_by"], e["source_reference"], e["created_at"]) for e in entries],
    ))
    _write_text(build_dir, "accountant/journal_lines.csv", _csv_text(
        ["journal_line_id", "journal_entry_id", "account_id", "account_code", "account_name", "debit_cents", "credit_cents"],
        [(line["id"], line["journal_entry_id"], line["account_id"], line["account_code"], line["account_name"], line["debit_cents"], line["credit_cents"]) for line in lines],
    ))

    files = []
    for file_path in sorted(path for path in build_dir.rglob("*") if path.is_file()):
        relative = file_path.relative_to(build_dir).as_posix()
        payload = file_path.read_bytes()
        files.append({"path": relative, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    manifest = {
        "format": "klearing-continuity-v1",
        "generated_at": generated_iso,
        "posted_entries_through": latest_date,
        "files": files,
    }
    _write_text(build_dir, "manifest.json", json.dumps(manifest, indent=2) + "\n")
    _validate_generation(build_dir)
    return manifest


def _validate_generation(generation_dir):
    try:
        manifest = json.loads((generation_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != "klearing-continuity-v1":
            raise ValueError("unknown manifest format")
        for record in manifest["files"]:
            payload = (generation_dir / record["path"]).read_bytes()
            if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise ValueError(f"hash mismatch: {record['path']}")
        overview = (generation_dir / "index.html").read_text(encoding="utf-8")
        if "Book record last updated:" not in overview or "layperson" in overview.lower():
            raise ValueError("readable timestamped overview missing")
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ContinuityArtifactError(f"The new book record could not be verified: {exc}") from exc


def _home_page(generation_name, generated_at):
    label = generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    href = quote(generation_name) + "/index.html"
    body = (
        "<h1>Your Klearing Book</h1>"
        f"<div class='asof'><strong>Book record last updated:</strong> {html.escape(label)}</div>"
        "<p>This start page points to the most recently completed and verified local copy of your books.</p>"
        f"<p><a href='{href}'><strong>Open the readable book →</strong></a></p>"
        "<p class='note'>If you know Klearing recorded activity after the time shown above, open My Klearing Book from inside the app to refresh it.</p>"
    )
    return _page("Your Klearing Book", body)


def _clear_if_unchanged(token, publication):
    conn = sqlite3.connect(paths.db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        if token is None:
            current = conn.execute(
                "SELECT 1 FROM meta WHERE key = ?", (DIRTY_KEY,)
            ).fetchone()
            still_clean = current is None
        else:
            cursor = conn.execute(
                "DELETE FROM meta WHERE key = ? AND value = ?", (DIRTY_KEY, token)
            )
            still_clean = cursor.rowcount == 1
        if still_clean:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (PUBLISHED_KEY, json.dumps(publication, separators=(",", ":"))),
            )
        conn.commit()
        return still_clean
    finally:
        conn.close()


def publish_if_needed():
    """Return the start-file path, regenerating only when absent or dirty."""
    import db
    db.init_db()
    root = paths.data_dir() / ROOT_NAME
    home = root / START_FILE
    marker = dirty_marker()
    if marker is None and home.is_file():
        return {"path": home, "regenerated": False, "generated_at": None}

    root.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        snapshot = _snapshot()
        generated_at = _utc_now()
        stamp = generated_at.strftime("%Y-%m-%d %H%M%S.%f UTC")
        final_name = GENERATION_PREFIX + stamp
        build_dir = root / (".building-" + uuid.uuid4().hex)
        final_dir = root / final_name
        try:
            build_dir.mkdir()
            manifest = _build_generation(build_dir, snapshot, generated_at)
            os.replace(build_dir, final_dir)
            home_temp = root / (START_FILE + ".new")
            home_temp.write_text(_home_page(final_name, generated_at), encoding="utf-8", newline="\n")
            os.replace(home_temp, home)
            publication = {"generated_at": manifest["generated_at"], "generation": final_name}
            if _clear_if_unchanged(snapshot["dirty_token"], publication):
                _remove_old_generations(root, final_dir)
                return {"path": home, "regenerated": True, "generated_at": manifest["generated_at"], "generation": final_dir}
        except Exception as exc:
            if build_dir.exists():
                shutil.rmtree(build_dir, ignore_errors=True)
            if isinstance(exc, ContinuityArtifactError):
                raise
            raise ContinuityArtifactError(f"Klearing could not publish the readable book: {exc}") from exc
    raise ContinuityArtifactError("The books kept changing while the readable copy was being built. Please try again.")


def _remove_old_generations(root, keep):
    for candidate in root.iterdir():
        if candidate.is_dir() and candidate.name.startswith(GENERATION_PREFIX) and candidate != keep:
            shutil.rmtree(candidate, ignore_errors=True)


def reveal_folder():
    """Open the publication folder after an explicit user request."""
    result = publish_if_needed()
    folder = result["path"].parent
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return result
