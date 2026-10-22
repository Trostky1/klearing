"""Static and render smoke coverage for the four-tab Tax workspace."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-tax-frontend-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def run():
    failures = 0
    archive_page = app.render_tax_page(tab="archive")
    statement_page = app.render_tax_page(tab="statements")
    projection_page = app.render_tax_page(tab="projections")
    planning_page = app.render_tax_page(tab="planning")
    tax_nav = app._tax_nav("archive")

    failures += check(
        "Tax shell exposes four wired tabs",
        all(f"tab={tab}" in tax_nav
            for tab in ("archive", "statements", "projections", "planning"))
        and tax_nav.count('aria-current="page"') == 1)
    failures += check(
        "Legacy archive is read-only, clearly labeled, and exportable",
        all(marker in archive_page for marker in (
            'data-view="archive"', 'id="tax-archive-counts"',
            'id="tax-legacy-csv-export"', 'Read only'))
        and '/api/tax/exports/legacy-transactions.csv' in archive_page
        and 'tax-sync' not in archive_page
        and 'tax-bulk-dialog' not in archive_page)
    failures += check(
        "Statements tab includes period, result grids, and all statement exports",
        all(marker in statement_page for marker in (
            'id="tax-statement-period"', 'id="tax-statement-results"',
            'id="tax-income-export"', 'id="tax-schedule-export"',
            'id="tax-statement-csv"'))
        and "Ledger activity CSV" in statement_page
        and statement_page.count('aria-disabled="true"') >= 3)
    failures += check(
        "Projections tab includes deduction inputs, results, quarters, and PDF export",
        all(marker in projection_page for marker in (
            'id="tax-section-179"', 'id="tax-home-office"',
            'id="tax-projection-results"', 'id="tax-quarterly-rows"',
            'id="tax-projection-export"', 'id="tax-projection-blocked-title"',
            'id="tax-projection-blocked-action"')))
    failures += check(
        "Planning tab remains available",
        'id="tax-planning"' in planning_page
        and '/static/tax/planning.js' in planning_page)

    workspace = (ROOT / "frontend" / "tax" / "workspace.js").read_text(encoding="utf-8")
    failures += check(
        "Workspace script wires the archive and canonical financial APIs",
        all(route in workspace for route in (
            "/api/tax/legacy-transactions", "/api/tax/statements/income",
            "/api/tax/statements/balance-sheet", "/api/tax/projection",
            "/api/tax/exports/income-statement.pdf",
            "/api/tax/exports/schedule-c.pdf",
            "/api/tax/exports/transactions.csv",
            "/api/tax/exports/projection.pdf"))
        and "/api/tax/sync-transactions" not in workspace
        and "/api/tax/transactions" not in workspace)
    failures += check(
        "Dynamic financial and transaction data uses DOM-safe text rendering",
        "innerHTML" not in workspace and ".textContent" in workspace)
    failures += check(
        "category-sensitive export links stay disabled until the statement is complete",
        "setExportLink" in workspace
        and "updateExports(statement.is_tax_complete)" in workspace
        and 'removeAttribute("href")' in workspace)
    failures += check(
        "Projections suppress money and their PDF for both categorization and stale-year blocks",
        "showProjectionBlock" in workspace
        and "tax_categorization_required" in workspace
        and "tax_year_stale" in workspace
        and 'output.hidden = true;' in workspace
        and 'exportLink.setAttribute("aria-disabled", "true")' in workspace)
    failures += check(
        "Packaged app includes the complete Tax frontend directory",
        '"frontend", "tax"' in (ROOT / "build" / "klearing.spec").read_text(
            encoding="utf-8"))
    route_paths = {route.path for route in app.app.routes if hasattr(route, "path")}
    failures += check(
        "Tax workspace script has a registered static route",
        "/static/tax/workspace.js" in route_paths)
    planning = (ROOT / "frontend" / "tax" / "planning.js").read_text(
        encoding="utf-8")
    failures += check(
        "Planning exposes only Shopify/manual modes and explains Ledger costs",
        'data-source="auto"' not in planning_page
        and all(f'data-source="{source}"' in planning_page
                for source in ("shopify", "manual"))
        and "period-matched costs from categorized Ledger accounts" in planning_page
        and "data.cost_basis" in planning)

    if failures:
        print(f"FAIL: {failures} Tax frontend checks failed.")
        return 1
    print("PASS: Tax frontend checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
