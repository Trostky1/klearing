"""Offline smoke coverage for every app.py render function.

This suite imports the application against an isolated temporary data
directory, replaces external integrations with deterministic fixtures, and
checks the contract shared by every renderer: non-empty HTML with hostile
user strings escaped. It never starts a server or touches port 8642.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import UploadFile


# Set the data directory before importing app/db so importing the application
# cannot open or mutate the developer's real Klearing database.
_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-render-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402


_REAL_DB = {
    name: getattr(app.db, name)
    for name in (
        "init_db", "list_items", "list_items_in_range",
        "list_quote_followups", "list_invoice_reminders",
        "list_pending_payment_matches", "list_invoices",
        "list_recurring_instances", "list_recurring_templates",
        "list_inventory_reconciliation_queue",
        "list_nexus_states", "list_reconcile_history",
        "list_ga4_snapshots", "get_setting",
        "get_insight_widget_config",
        "get_meta", "get_agent_memory", "count_uncategorized_approved",
        "get_monthly_close", "get_vendor_memory", "get_wave_posted",
        "transaction_match_exists", "insert_pending_item",
    )
}
_REAL_DETECT_SUBSCRIPTIONS = app.recurring.detect_subscriptions
_REAL_GATHER_MONTH_DATA = app.accountant_export.gather_month_data
_REAL_PDF_AVAILABLE = app.accountant_export.PDF_AVAILABLE
_REAL_TAX_SETTINGS_GET = app.tax_settings.get


HOSTILE = "<script>alert(1)</script>"
SUMMARY = {
    "total_in": 0.0,
    "total_out": 0.0,
    "net": 0.0,
    "transfer_total": 0.0,
    "income": [],
    "expense": [],
    "transfer": [],
}
ITEM = {
    "id": 1,
    "vendor": HOSTILE,
    "amount": 10.0,
    "direction": "expense",
    "suggested_category": "<category>",
    "date": "2026-07-01",
    "notes": "<note>",
    "source": "manual",
    "external_transaction_id": "fixture-1",
    "external_id": "fixture-1",
}


def _install_fixtures():
    """Make every renderer deterministic and offline."""
    app.db.init_db = lambda: None
    app.db.list_items = lambda status=None: []
    app.db.list_items_in_range = lambda *args, **kwargs: []
    app.db.list_quote_followups = lambda status=None: []
    app.db.list_inventory_reconciliation_queue = lambda: []
    app.db.list_invoice_reminders = lambda status=None: []
    app.db.list_pending_payment_matches = lambda: []
    app.db.list_invoices = lambda include_voided=True: []
    app.db.list_recurring_instances = lambda status=None: []
    app.db.list_recurring_templates = lambda active_only=False: []
    app.db.list_nexus_states = lambda: []
    app.db.list_reconcile_history = lambda *args, **kwargs: []
    app.db.list_ga4_snapshots = lambda *args, **kwargs: []
    app.db.list_automation_jobs = lambda: []
    app.db.get_setting = lambda key: None
    app.db.get_insight_widget_config = lambda profile_id: None
    app.db.get_meta = lambda key: (
        "2020-01-01" if key == app.db.FIRST_SEEN_KEY else None
    )
    app.db.get_agent_memory = lambda key: (
        {"value": "Fixture insight", "updated_at": "2026-07-13T00:00:00"}
        if key == app.ollama_client.INSIGHT_KEY else None
    )
    app.db.count_uncategorized_approved = lambda: 1
    app.db.get_monthly_close = lambda period: None
    app.db.get_vendor_memory = lambda **kwargs: []
    app.db.get_wave_posted = lambda key: None
    app.tax_settings.get = lambda: dict(app.tax_settings.DEFAULTS)

    app.banks.active_adapter = lambda: None
    # The dedicated SimpleFIN Connections card imports
    # banks.get_adapter("simplefin") directly (by design -- it must show
    # SimpleFIN's own state independent of active_adapter() above), so
    # stubbing active_adapter() alone does not protect its is_configured()
    # call the way it protects the generic bank card's. Stubbed here the
    # same way every other integration below is, so this offline smoke
    # suite (db.init_db is a no-op below, so no "credentials" table
    # exists yet) never has to touch the real secrets_store/credentials
    # path -- matching stripe_client/gusto_client/etc.'s own stubs.
    app.banks.get_adapter("simplefin").is_configured = lambda: False
    app.business_registry.active_business = lambda: None
    app.business_registry.list_businesses = lambda: []
    app.cash_alert.get_config = lambda: {
        "cash_account_ids": [],
        "balance_threshold": None,
        "runway_days": None,
    }
    app.health.get_weights = lambda: {key: 1 for key in app.health.FACTORS}
    app.email_utils.is_configured = lambda: False
    app.gmail_reader.is_configured = lambda: False
    app.ga4_client.is_configured = lambda: False
    app.ga4_client.conversion_events = lambda: []
    app.stripe_client.is_configured = lambda: False
    app.gusto_client.is_configured = lambda: False
    app.klaviyo_client.is_configured = lambda: False
    app.slack_client.is_configured = lambda: False
    app.slack_client.daily_digest_enabled = lambda: False
    app.notion_client.is_configured = lambda: False
    app.shopify_client.is_configured = lambda: False
    app.recurring.detect_subscriptions = lambda: []

    app.update_check.available_update = lambda: {
        "version": HOSTILE,
        "url": "https://example.test/?q=<unsafe>",
    }
    app.net_series = lambda *args, **kwargs: ([], [])
    app.weekly_net_series = lambda *args, **kwargs: ([], [])
    app.monthly_income_series = lambda *args, **kwargs: ([], [])
    app._data_health_snapshot = lambda: (None, [])
    app.build_audit_trail = lambda: []
    app.accountant_export.gather_month_data = lambda period: {
        "start_date": "2026-06-01",
        "end_date": "2026-06-30",
        "approved": [],
        "pending": [],
        "total_in": 0,
        "total_out": 0,
    }
    app.accountant_export.notes_coverage = lambda period: (0, 0)
    app.accountant_export.PDF_AVAILABLE = False

    # Exercise the cloud-consent branch in Settings as part of the same
    # isolated fixture set.
    app.ai_providers.provider_name = lambda: "openai"
    app.ai_providers.is_cloud = lambda name=None: True
    app.ai_providers.has_api_key = lambda name: False
    app.app_lock.is_configured = lambda: False

    for name in (
        "dash_needs_review", "dash_vendor_flag", "dash_month",
        "dash_top_categories", "dash_cash", "dash_reconcile",
        "dash_traffic", "dash_subscriptions",
    ):
        if hasattr(app, name):
            setattr(app, name, lambda *args, **kwargs: "<div>fixture</div>")


RENDER_CASES = {
    "render_unlock_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_category_dropdown": ((HOSTILE, [], "form-1"), {}),
    "render_evidence_dropdown": (("", "form-1", "field-1"), {}),
    "render_update_banner": ((), {}),
    "render_duplicate_card": ((ITEM,), {}),
    # The three review queues merged into one page in the 2026-07-26
    # redesign; each still has a content builder, and render_review_page
    # composes them under the shared sub-nav.
    "_review_transactions_content": (([ITEM], []), {}),
    "render_review_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_breakdown_table": ((HOSTILE, []), {}),
    "render_summary_section": ((HOSTILE, SUMMARY), {}),
    "render_range_toggle": (("/summary", "8w"), {}),
    "render_summary_page": (
        ("2026-07-01", "2026-07-31", SUMMARY, SUMMARY),
        {"message": HOSTILE, "error": HOSTILE},
    ),
    "render_uncat_nudge": ((), {}),
    "render_daily_insight_banner": ((), {}),
    "render_home_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_uncategorized_page": (([ITEM],), {"message": HOSTILE, "error": HOSTILE}),
    "render_health_card_body": (({"score": None},), {}),
    "render_reconcile_page": ((), {"problem": HOSTILE, "message": HOSTILE, "error": HOSTILE}),
    "render_subscriptions_section": (([],), {}),
    "render_vendor_detail_page": ((HOSTILE, [], 0), {"message": HOSTILE, "error": HOSTILE}),
    "render_vendors_page": (([], 20), {"message": HOSTILE, "error": HOSTILE}),
    "_review_followups_content": (([],), {"error": HOSTILE}),
    "_review_invoices_content": (([],), {"error": HOSTILE}),
    "render_events_table": (({"current": {}, "prior": {}},), {}),
    "render_insights_page": ((None,), {"message": HOSTILE, "error": HOSTILE}),
    "render_logs_page": (("app",), {"message": HOSTILE, "error": HOSTILE}),
    "render_connections_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_automation_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_settings_page": (([],), {"message": HOSTILE, "error": HOSTILE}),
    "render_growth_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_shopify_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_tax_page": ((), {"message": HOSTILE, "error": HOSTILE}),
    # render_tools_hub deleted — it was six static cards with no data.
    "render_data_health": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_accountant_export_page": (("2026-06",), {"message": HOSTILE, "error": HOSTILE}),
    "render_unit_economics": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_nexus": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_contractors": ((), {"message": HOSTILE, "error": HOSTILE}),
    "render_audit": ((), {"message": HOSTILE, "error": HOSTILE}),
}


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
        return True
    print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
    return False


def _restore_real_db():
    for name, func in _REAL_DB.items():
        setattr(app.db, name, func)
    app.tax_settings.get = _REAL_TAX_SETTINGS_GET


def _data_health_import_smoke():
    """Exercise the real upload path against the isolated temp database."""
    _restore_real_db()
    original_snapshot = app._data_health_snapshot
    original_classify = app.check_bank.classify
    adapter = SimpleNamespace(id="mercury")
    account = {"id": "account-1", "name": "Checking", "balance": 100.0}
    row = {"account": account}
    app._data_health_snapshot = lambda: (adapter, [row])
    app.check_bank.classify = lambda vendor, direction: (direction, "Uncategorized")
    statement = b"date,amount,description\n2026-07-01,-42.00,Coffee Shop\n"
    try:
        app.db.init_db()
        first = asyncio.run(app.tools_data_health_import(
            account_id="account-1",
            statement=UploadFile(file=io.BytesIO(statement), filename="statement.csv"),
        ))
        items_after_first = app.db.list_items(status="pending")
        second = asyncio.run(app.tools_data_health_import(
            account_id="account-1",
            statement=UploadFile(file=io.BytesIO(statement), filename="statement.csv"),
        ))
        items_after_second = app.db.list_items(status="pending")
        return first, items_after_first, second, items_after_second
    finally:
        app._data_health_snapshot = original_snapshot
        app.check_bank.classify = original_classify


def _accountant_export_smoke():
    """Exercise real CSV/PDF builders against the isolated temp database."""
    _restore_real_db()
    original_detect = app.recurring.detect_subscriptions
    original_gather = app.accountant_export.gather_month_data
    original_pdf = app.accountant_export.PDF_AVAILABLE
    app.recurring.detect_subscriptions = _REAL_DETECT_SUBSCRIPTIONS
    app.accountant_export.gather_month_data = _REAL_GATHER_MONTH_DATA
    app.accountant_export.PDF_AVAILABLE = _REAL_PDF_AVAILABLE
    try:
        app.db.init_db()
        qb_csv = app.accountant_export.build_qb_csv("2026-06")
        all_csv = app.accountant_export.build_all_transactions_csv("2026-06")
        pdf = (app.accountant_export.build_close_pdf("2026-06")
               if app.accountant_export.PDF_AVAILABLE else None)
        return qb_csv, all_csv, pdf
    finally:
        app.recurring.detect_subscriptions = original_detect
        app.accountant_export.gather_month_data = original_gather
        app.accountant_export.PDF_AVAILABLE = original_pdf


def run():
    _install_fixtures()
    failures = 0

    print("Render coverage — all 37 app.py render functions")
    failures += not check("exactly 37 renderer cases", len(RENDER_CASES) == 37)
    for name, (args, kwargs) in RENDER_CASES.items():
        try:
            html = getattr(app, name)(*args, **kwargs)
            failures += not check(
                f"{name} returns non-empty HTML",
                isinstance(html, str) and bool(html.strip()),
            )
            failures += not check(
                f"{name} escapes hostile user HTML",
                HOSTILE not in html,
            )
        except Exception as exc:
            print(f"  FAIL  {name} raised {type(exc).__name__}: {exc}")
            failures += 1

    print("v0.4.x feature smoke checks")
    data_health = app.render_data_health()
    failures += not check("Data Health page renders", "No bank accounts connected" in data_health)
    export = app.render_accountant_export_page("2026-06")
    failures += not check("Accountant Export page renders", "monthly close package" in export)

    status = app.ai_status()
    try:
        decoded = json.loads(json.dumps(status))
    except (TypeError, ValueError):
        decoded = None
    failures += not check(
        "AI status route returns valid JSON",
        isinstance(decoded, dict) and decoded.get("provider") == "openai",
    )
    settings = app.render_settings_page([])
    failures += not check(
        "cloud consent dialog present for OpenAI",
        "This provider sends your financial data" in settings
        and "OpenAI" in settings,
    )

    print("Data intelligence smoke checks")
    try:
        first, first_items, second, second_items = _data_health_import_smoke()
        failures += not check(
            "Data Health statement import succeeds",
            getattr(first, "status_code", None) == 303,
        )
        failures += not check(
            "Data Health import inserts Pending item",
            len(first_items) == 1 and first_items[0]["vendor"] == "Coffee Shop",
        )
        failures += not check(
            "Data Health import deduplicates existing transaction",
            getattr(second, "status_code", None) == 303 and len(second_items) == 1,
        )
    except Exception as exc:
        print(f"  FAIL  Data Health import smoke raised {type(exc).__name__}: {exc}")
        failures += 3

    try:
        qb_csv, all_csv, pdf = _accountant_export_smoke()
        failures += not check(
            "Accountant Export CSV generation",
            qb_csv.startswith("Date,Description,Amount")
            and all_csv.startswith("Date,Description,Amount"),
        )
        failures += not check(
            "Accountant Export PDF generation",
            (pdf is not None and pdf.startswith(b"%PDF"))
            or (pdf is None and not _REAL_PDF_AVAILABLE),
        )
    except Exception as exc:
        print(f"  FAIL  Accountant Export smoke raised {type(exc).__name__}: {exc}")
        failures += 2

    failures += _check_card_reflow_labels()

    if failures:
        print(f"FAIL: {failures} regression checks failed.")
        return 1
    print("PASS: all renderer and v0.4.x smoke checks passed.")
    return 0


def _check_card_reflow_labels():
    """A card-reflowed cell must carry its own column's name.

    On a phone the header row becomes a sort bar rather than a set of
    column positions, so each value has to say what it is. The label is
    rendered from the same column list that builds thead (ui.table_head /
    ui.cell_label) so the two cannot drift — this asserts the result
    rather than trusting the mechanism.

    Only *record* tables opt in, where a row is one thing you inspect.
    Scanning tables keep horizontal scroll: stacking a column you are
    comparing down the page puts each number a screen apart. Contacts is
    checked here as the counter-example so the distinction stays real.
    """
    import re

    failed = 0
    views = [{
        "invoice": {"id": 1, "invoice_number": "INV-0001",
                    "contact_id": 1, "issue_date": "2026-08-01",
                    "due_date": "2026-08-31"},
        "totals": {"net": 50000, "tax": 4375, "gross": 54375},
        "balance": 54375, "tag": "tag", "label": "Sent", "status": "sent",
    }]
    app.db.get_contact = lambda contact_id: {"display_name": "Acme Corp"}

    html = app._invoice_table(views, "none")
    failed += not check("Invoices table opts into card reflow",
                        'class="table-hover card-reflow"' in html)

    head = re.search(r"<thead>(.*?)</thead>", html, re.S)
    headers = [re.sub(r"<[^>]+>", "", cell).strip()
               for cell in re.findall(r"<th[^>]*>(.*?)</th>",
                                      head.group(1) if head else "", re.S)]
    failed += not check(
        "Invoices headers are rendered from INVOICE_COLUMNS",
        headers == [column[0] for column in app.INVOICE_COLUMNS],
        f"{headers}")

    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>",
                      body.group(1) if body else "", re.S)
    labels = [re.findall(r'<td[^>]*\sdata-label="([^"]*)"', row)
              for row in rows]
    failed += not check(
        "every Invoices cell carries its own column's label",
        bool(labels) and all(row == headers for row in labels),
        f"{labels[:1]} vs {headers}")

    failed += not check(
        "the actions column of a reflowed table has an empty label",
        app.RECURRING_COLUMNS[-1][0] == "",
        "an actions column has no header to echo onto its cells")

    contacts = app._contacts_all_content()
    failed += not check(
        "Contacts keeps horizontal scroll rather than card reflow",
        "card-reflow" not in contacts,
        "Contacts is a scanning table — cards would put the revenue "
        "figures being compared a screen apart")

    # Pending amounts arrive as Decimal. summarize() adds them to a float
    # zero and raises, so the Review strip sums as Decimal on its own.
    from decimal import Decimal
    strip = app._review_summary_strip([
        {"direction": "income", "amount": Decimal("936.00")},
        {"direction": "expense", "amount": Decimal("18.40")},
        {"direction": "expense", "amount": Decimal("54.99")},
        {"direction": "transfer", "amount": Decimal("500.00")},
    ])
    failed += not check(
        "the Review strip sums Decimal amounts and leaves transfers out",
        "$936.00" in strip and "$73.39" in strip and "1 transfer" in strip,
        strip[:200])
    return failed


if __name__ == "__main__":
    raise SystemExit(run())
