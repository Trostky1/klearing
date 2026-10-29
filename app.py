"""
Klearing — local approval queue for small-business finances.

Run (dev):  python main.py
Then open http://localhost:8642

Bank transactions (via the active bank adapter) wait as cards on the
Pending tab. Approving records them in the native double-entry ledger;
denying dismisses. Nothing is ever posted or emailed
without an explicit click.

First run goes through Setup (setup_wizard.py). After that, every
page sits behind the app password (app_lock.py) — a UI lock only;
credentials are protected separately by the OS credential store.
"""

import calendar
import csv
import io
import json
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import List
from urllib.parse import quote

from fastapi import File, FastAPI, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response,
                               StreamingResponse)

import accountant_export
from backend.routers import tax as tax_router
from backend.services import (contractor_handoff, financial_insights,
                              tax_projection, tax_settings)
import app_lock
import banks
import business_registry
import briefing_signals
import cash_alert
import charts
import check_bank
import crm
import continuity_artifact
import db
import ai_providers
import email_utils
import error_alerts
import ga4_client
import gmail_reader
import gusto_client
import inventory
import insight_metrics
import insight_widget
import health
import invoice_ledger
import invoice_reconcile
import klaviyo_client
import ledger
import ledger_sync
import local_host
import ollama_client
import paths
import plaid_client
import recurring
import recurring_invoices
import scheduler
import secrets_store
import setup_wizard
import slack_client
import stripe_client
import tax_categories
import ui
import update_check
import notion_client
import shopify_client
import shopify_analytics
from config import APP_NAME, APP_VERSION
from src import categorize as categorizer
from ui import confirm_attrs, confirm_onsubmit, money, render_shell

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

app = FastAPI(title=APP_NAME)
app.include_router(setup_wizard.router)
app.include_router(tax_router.router)


def _plaid_error_response(exc):
    status = 400 if isinstance(exc, plaid_client.PlaidConfigError) else 502
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)


@app.post("/plaid/link-token")
def plaid_link_token():
    """Phase 1 Plaid Link token; no redirect_uri or webhook is sent.

    OAuth-institution redirect handling and webhooks are intentionally
    deferred to Phase 2.
    """
    try:
        return JSONResponse({"ok": True, "link_token": plaid_client.create_link_token()})
    except (plaid_client.PlaidConfigError, plaid_client.PlaidAPIError) as exc:
        return _plaid_error_response(exc)


@app.post("/plaid/exchange")
async def plaid_exchange(request: Request):
    """Exchange a Link public token and encrypt the Item credentials."""
    try:
        payload = await request.json()
        item_id = plaid_client.exchange_public_token(payload.get("public_token"))
        return JSONResponse({"ok": True, "connection_id": item_id})
    except (plaid_client.PlaidConfigError, plaid_client.PlaidAPIError) as exc:
        return _plaid_error_response(exc)
    except (AttributeError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Expected a JSON public_token."},
                            status_code=400)


@app.post("/plaid/sync/{connection_id}")
def plaid_sync(connection_id: str):
    """Return the next Plaid transaction page and save its cursor."""
    try:
        return JSONResponse({"ok": True, **plaid_client.sync(connection_id)})
    except (plaid_client.PlaidConfigError, plaid_client.PlaidAPIError) as exc:
        return _plaid_error_response(exc)

# A pending item whose vendor starts with this prefix is a dev-only
# fixture (inserted via the "Insert test item" button) — approving it
# skips the real ledger write, so the confirm-dialog and category
# flows can be tested without touching real money or financial data.
# The dev-insert route forces this prefix onto any custom vendor name.
TEST_VENDOR_PREFIX = "TEST ITEM"
TEST_VENDOR_NAME = "TEST ITEM (safe)"
TEST_TRANSACTION_AUDIT_ID = "TEST-NO-REAL-POST"

# Category-side accounts come from the native Chart of Accounts. This used
# to be a cached Wave API fetch that returned None whenever Wave was
# unreachable, which is why callers still tolerate an empty list. A local
# SQLite read needs no cache, no TTL, and no business key, and it cannot
# fail the way a network call could -- so the cache and the None path are
# both gone rather than preserved out of habit.
INVENTORY_ACCOUNT_CODE = "1200"


def get_categories():
    """Category-side Ledger accounts for the Review dropdown.

    Revenue and expense accounts are the categories proper. The Inventory
    asset account joins them so a material purchase can be posted straight
    to inventory instead of expensed; every other asset account (cash,
    receivables) is a source of money, never a category for it.
    """
    return [account for account in ledger.list_accounts()
            if account["type"] in ("revenue", "expense", "equity")
            or account["code"] == INVENTORY_ACCOUNT_CODE]


def resolve_submitted_category(value):
    """Resolve a submitted category to (ledger account or None, display name).

    The Review dropdown submits an account CODE (see
    render_category_dropdown). Two other things still arrive as plain
    names and must pass through untouched: the categorizer's suggestion,
    used when nothing was picked, and the free-text fallback input. Codes
    are numeric and names are not, so the two cannot collide.

    The name is what the stored category and vendor memory use. The account
    is what the Ledger post needs, and it is None
    exactly when the submission was a bare name -- no account is guessed
    from a name, because choosing where money lands on a string
    resemblance is the one thing this path must never do.

    Both the single and the bulk approve paths call this at the same
    point, so a category means exactly the same thing either way.
    """
    submitted = (value or "").strip()
    if not submitted:
        return None, ""
    account = ledger.account_by_code(submitted)
    return (account, account["name"]) if account else (None, submitted)


# ---------------------------------------------------------------- gatekeeper


def _session_ok(request):
    return app_lock.session_valid(
        request.cookies.get(app_lock.SESSION_COOKIE)
    )


# Setup routes a fresh install may hit before the password exists.
# The exemption evaporates the moment a password is set — otherwise
# anyone on the machine could re-run step 1 to REPLACE the password
# mid-setup and walk in, which is exactly what the lock exists to stop.
PRE_PASSWORD_PATHS = {"/setup", "/setup/step/1", "/setup/password"}
PROCESS_HEALTH_PATH = "/__klearing_process_health"
_legacy_business_cleanup_done = False


@app.get(PROCESS_HEALTH_PATH)
def process_health():
    """Unauthenticated local identity probe for the supervising shell only."""
    return {"app": "klearing", "version": APP_VERSION, "role": "backend"}


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    global _legacy_business_cleanup_done
    path = request.url.path
    if path == PROCESS_HEALTH_PATH:
        return await call_next(request)
    if path == "/favicon.ico":
        icon = paths.resource_path("assets/klearing.ico")
        if icon.exists():
            return FileResponse(icon, media_type="image/x-icon")
        return Response(status_code=204)
    if path == "/unlock" or path == "/lock":
        return await call_next(request)

    db.init_db()
    if not _legacy_business_cleanup_done:
        business_registry.migrate_legacy_state()
        _legacy_business_cleanup_done = True
    if not setup_wizard.is_complete():
        if not path.startswith("/setup"):
            return RedirectResponse("/setup", status_code=303)
        pre_password_ok = (path in PRE_PASSWORD_PATHS
                           and not app_lock.is_configured())
        if (app_lock.is_configured() and not _session_ok(request)
                and not pre_password_ok):
            return RedirectResponse("/unlock", status_code=303)
        return await call_next(request)

    if app_lock.is_configured() and not _session_ok(request):
        return RedirectResponse("/unlock", status_code=303)
    return await call_next(request)


# Register after the route-level gatekeeper decorator: FastAPI prepends each
# middleware registration, so this makes the exact Host check the outermost
# HTTP boundary and prevents redirects/lock pages from bypassing it.
app.add_middleware(local_host.LocalHostMiddleware)


def render_unlock_page(message=None, error=None):
    content = f"""
    <div class="section measure">
      <div class="section-head"><h2>Unlock with your app password</h2></div>
      <div class="hint mb-4">This keeps your
        finances private from anyone else using this computer.</div>
      <form method="post" action="/unlock">
        <div class="cred-field">
          <label class="q" for="pw">App password</label>
          <input type="password" id="pw" name="password" required
                 autocomplete="current-password" autofocus>
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Unlock</button>
        </div>
      </form>
    </div>"""
    return render_shell("", "<span class='tag'>Locked</span>", content,
                        message=message, error=error, show_nav=False)


@app.get("/unlock", response_class=HTMLResponse)
def unlock_page(msg: str = None):
    if not app_lock.is_configured():
        return RedirectResponse("/", status_code=303)
    return render_unlock_page(message=msg)


@app.post("/unlock")
def unlock(password: str = Form("")):
    wait = app_lock.locked_out()
    if wait:
        return HTMLResponse(render_unlock_page(
            error=f"Too many wrong attempts — try again in {wait} seconds."))
    if not app_lock.verify(password):
        app_lock.note_failed_attempt()
        return HTMLResponse(render_unlock_page(error="Wrong password."))
    app_lock.note_successful_attempt()
    destination = "/" if setup_wizard.is_complete() else "/setup"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(app_lock.SESSION_COOKIE, app_lock.issue_session(),
                        httponly=True, samesite="lax")
    return response


@app.post("/lock")
def lock(request: Request):
    app_lock.revoke_session(request.cookies.get(app_lock.SESSION_COOKIE))
    return RedirectResponse("/unlock", status_code=303)


@app.post("/business/switch")
def business_switch(request: Request, business_id: str = Form("")):
    if not business_registry.set_active_business(business_id):
        return _redirect(err="That business isn't configured.")
    name = business_registry.business_name_by_id(business_id) or "business"
    # Stay on the page the switch happened on.
    from urllib.parse import urlparse
    path = urlparse(request.headers.get("referer") or "").path
    target = path if path.startswith("/") else "/"
    return RedirectResponse(
        f"{target}?msg={quote(f'Now working in {name}.')}", status_code=303,
    )


# ---------------------------------------------------------------- Pending


def signed_money(item):
    sign = "-" if item["direction"] == "expense" else "+"
    return f"{sign}{money(item['amount'])}"


def render_category_dropdown(selected_name, categories, form_id,
                             suggestion_name=None, bulk_name=None):
    """
    A <select> of the native Chart of Accounts, grouped by type, defaulting
    to the suggested category. Associated with the approve form via the
    form= attribute so it submits with Approve. Falls back to a static
    editable text input if the account list is somehow empty.

    Option values are account CODES, not names. Codes are unique in the
    schema and names are not, so a code is the only identifier that
    resolves to exactly one account -- which is what lets the approval
    path post to a Ledger account without matching on a display string.
    Everything user-visible is still the name.
    """
    selected_name = selected_name or ""
    suggestion_name = suggestion_name or ""
    # data-bulk-name lets table.js carry this row's value into a bulk
    # submit; the form= association already spoken for by the row's own
    # approve form means it cannot simply be re-parented (see
    # _review_transactions_content).
    bulk = f' data-bulk-name="{bulk_name}"' if bulk_name else ""
    if not categories:
        return (f'<input type="text" name="category"{bulk} '
                f'class="category-select" form="{form_id}" '
                f'value="{escape(selected_name)}" required '
                'placeholder="Enter a category">')

    groups = {"expense": [], "revenue": [], "equity": [], "asset": []}
    code_by_name = {}
    for account in categories:
        groups.setdefault(account["type"], []).append(account)
        code_by_name[account["name"]] = account["code"]

    found = selected_name in code_by_name
    html = (f'<select name="category"{bulk} class="category-select" '
            f'form="{form_id}">')
    if suggestion_name:
        # The categorizer suggests a name; carry its code so selecting the
        # suggestion submits the same value any other option would.
        suggested_value = code_by_name.get(suggestion_name, suggestion_name)
        html += (f'<option value="{escape(suggested_value)}">'
                 f'{escape(suggestion_name)} (suggested)</option>')
        html += '<option value="" selected>Select a category</option>'
    elif not selected_name:
        html += '<option value="" selected>Select a category</option>'
    if not found and selected_name:
        # A previously stored category that no longer matches an account --
        # keep it visible and selected rather than silently swapping it.
        html += (f'<option value="{escape(selected_name)}" selected>'
                 f'{escape(selected_name)}</option>')
    # "Inventory (asset)" is its own group, kept distinct from the materials
    # expense category, so a material buy can be posted to inventory
    # instead of expensed.
    for label, key in (("Expenses", "expense"), ("Income", "revenue"),
                       ("Equity", "equity"), ("Inventory (asset)", "asset")):
        if not groups[key]:
            continue
        html += f'<optgroup label="{label}">'
        for account in sorted(groups[key], key=lambda row: row["name"]):
            sel = " selected" if account["name"] == selected_name else ""
            html += (f'<option value="{escape(account["code"])}"{sel}>'
                     f'{escape(account["name"])}</option>')
        html += "</optgroup>"
    html += "</select>"
    return html


def _evidence_options(selected_type):
    """<option> tags for the evidence-on-file tags, shared by every
    evidence <select> in the app (Pending's approve form, the approved-
    item edit row on Vendor Detail) so the choice list can't drift."""
    options = '<option value="">No evidence tagged</option>'
    for value, label in accountant_export.EVIDENCE_TYPES:
        sel = " selected" if value == selected_type else ""
        options += f'<option value="{value}"{sel}>{escape(label)}</option>'
    return options


def render_evidence_dropdown(selected_type, form_id, field_id,
                             bulk_name=None):
    """A <select> of evidence tags tied to an EXTERNAL form via form=
    — used on Pending, where the dropdown sits outside the approve
    form it submits with (same pattern as render_category_dropdown)."""
    bulk = f' data-bulk-name="{bulk_name}"' if bulk_name else ""
    return (f'<select id="{field_id}" name="evidence_type"{bulk} '
            f'form="{form_id}">'
            f"{_evidence_options(selected_type)}</select>")


def render_update_banner():
    update = update_check.available_update()
    if not update:
        return ""
    link = (f' <a href="{escape(update["url"])}" target="_blank" '
            f'rel="noopener">See what changed and download it</a>.'
            if update.get("url") else "")
    return (f'<div class="banner">{APP_NAME} {escape(update["version"])} '
            f"is available (you're on v{escape(update_check.APP_VERSION)})."
            f"{link} Updating is a fresh download by design — this app "
            f"never updates itself.</div>")


def render_duplicate_card(item, dup_category=None):
    """The warning that replaces the normal approve card when the
    duplicate-post guard fires for this item."""
    existing = db.get_wave_posted(item["external_transaction_id"]) or {}
    posted_at = (existing.get("posted_at") or "")[:10]
    posted_by = existing.get("posted_by") or "another tool"
    when = f" on {escape(posted_at)}" if posted_at else " earlier"
    category = dup_category or item.get("suggested_category") or ""
    skip_confirm = confirm_attrs(
        "It won't be recorded.",
        title="Skip this transaction?", confirm_label="Skip",
        busy_label="Skipping…")
    post_confirm = confirm_attrs(
        "This creates a duplicate in your books. Only do this if you are "
        "sure the first one was removed.",
        title="Record a second copy?", confirm_label="Record duplicate",
        danger=True)
    return f"""
    <div class="card is-warn">
      <div class="card-main">
        <div class="vendor">{escape(item["vendor"])}
          <span class="jump-tag">already posted</span></div>
        <div class="dash-note note-warn">
          This transaction was already recorded{when} by
          {escape(posted_by)}. Recording it again would create a duplicate
          in your books. Record anyway?</div>
      </div>
      <div class="amount {item["direction"]}">{signed_money(item)}</div>
      <div class="actions">
        <form method="post" action="/skip/{item["id"]}" {skip_confirm}>
          <button class="approve" type="submit">Skip (recommended)</button>
        </form>
        <form method="post" action="/approve/{item["id"]}" {post_confirm}>
          <input type="hidden" name="force" value="1">
          <input type="hidden" name="category" value="{escape(category)}">
          <button class="deny" type="submit">Post anyway (I know what I'm doing)</button>
        </form>
      </div>
    </div>"""


REVIEW_ROW_CAP = 150


def _category_state(item, show_auto_suggestions):
    """How settled this item's category is: (state, name, tag_html).

    state is 'confirmed' (a high-confidence rule pre-selected it),
    'suggested' (a guess the user must accept) or 'missing'. The row
    shows this as information; the drawer is where it is changed.
    """
    suggestion = (item.get("suggested_category") or "").strip()
    confidence = item.get("category_confidence")
    if not show_auto_suggestions:
        suggestion, confidence = "", None
    if suggestion and confidence == "high":
        return ("confirmed", suggestion,
                f'<span class="tag ok">{escape(suggestion)}</span>')
    if suggestion and confidence == "medium":
        return ("suggested", suggestion,
                f'<span class="tag warn">{escape(suggestion)}?</span>')
    return ("missing", "",
            '<span class="tag off">Needs a category</span>')


def _review_detail_panel(item, categories, show_auto_suggestions):
    """Everything needed to decide one transaction.

    These are the same controls the row used to carry, not copies of
    them: the form= attribute associates by element id, so they submit
    to the row's approve form no matter where in the document they sit.
    table.js moves this node into the drawer on open and back on close,
    which keeps one source of truth for the field values and means bulk
    submission keeps reading exactly what the user last chose.
    """
    item_id = item["id"]
    form_id = f"approve-{item_id}"
    state, suggestion, _ = _category_state(item, show_auto_suggestions)
    selected = suggestion if state == "confirmed" else ""
    suggestion_name = suggestion if state == "suggested" else None

    dropdown = render_category_dropdown(
        selected, categories, form_id, suggestion_name=suggestion_name,
        bulk_name=f"category_{item_id}")

    hint = ""
    if state == "suggested":
        hint = (f'<div class="hint">Klearing guessed '
                f'<strong>{escape(suggestion)}</strong> but is not sure. '
                "Confirm it or pick another.</div>")
    elif state == "confirmed":
        hint = ('<div class="hint">Filled in from a rule you confirmed '
                "before. Change it if this one is different.</div>")

    transfer_note = ""
    if item["direction"] == "transfer":
        transfer_note = ('<div class="flag flag-info">Marked as money moved '
                         "between your own accounts, not income or spending."
                         "</div>")

    approve_confirm = confirm_attrs(
        f"{money(item['amount'])} will be recorded in your native books "
        "with the category shown. This cannot be undone from "
        "Klearing.",
        title="Record this transaction?", confirm_label="Record it")
    deny_confirm = confirm_attrs(
        "It will be dismissed from this queue and never recorded. Nothing "
        "is sent to your accounting.",
        title="Dismiss this transaction?", confirm_label="Dismiss",
        busy_label="Dismissing…", danger=True)

    return f"""
    <div class="review-detail is-hidden" id="detail-{item_id}"
         data-vendor="{escape(item["vendor"], quote=True)}"
         data-meta="{escape(item["date"], quote=True)}, {escape(item["source"], quote=True)}"
         data-amount="{escape(signed_money(item), quote=True)}"
         data-direction="{escape(item["direction"], quote=True)}">
      <div class="drawer-body">
        <div class="drawer-amount {item["direction"]}">{signed_money(item)}</div>
        {transfer_note}
        <div class="drawer-field">
          <label for="cat-{item_id}">Category</label>
          {dropdown}
          {hint}
        </div>
        <div class="drawer-field">
          <label for="notes-{item_id}">Note <span class="hint">optional</span></label>
          <input type="text" id="notes-{item_id}" name="notes" form="{form_id}"
                 data-bulk-name="notes_{item_id}" class="notes-input"
                 maxlength="500" placeholder="What was this for?">
        </div>
        <div class="drawer-field">
          <label for="evidence-{item_id}">Receipt or document
            <span class="hint">optional</span></label>
          {render_evidence_dropdown(item.get("evidence_type"), form_id,
                                    f"evidence-{item_id}",
                                    bulk_name=f"evidence_{item_id}")}
        </div>
      </div>
      <div class="drawer-foot">
        <form method="post" action="/approve/{item_id}" id="{form_id}"
              class="inline-form grow" {approve_confirm}>
          <input type="hidden" name="review_return" value="{item_id}">
          <button class="approve" type="submit">Approve</button>
        </form>
        <div class="deny-slot">
          <form method="post" action="/deny/{item_id}" class="inline-form"
                {deny_confirm}>
            <button type="submit">Dismiss</button>
          </form>
        </div>
      </div>
    </div>"""


def _review_summary_strip(items):
    """How much is waiting, and which way the money moves.

    Transfers stay out of money in and money out, as on the Summary page.
    Sums as Decimal: pending amounts arrive as Decimal, and summarize()
    mixes them with a float zero.
    """
    def total(direction):
        return sum((Decimal(str(item["amount"])) for item in items
                    if item["direction"] == direction), Decimal("0"))

    totals = {"total_in": total("income"), "total_out": total("expense")}
    transfers = sum(1 for item in items if item["direction"] == "transfer")
    note = (f"{transfers} transfer{'s' if transfers != 1 else ''} not counted "
            "in money in or out" if transfers else None)
    return f"""
    <div class="stat-grid mb-4">
      {ui.stat("Waiting", str(len(items)), sub=note)}
      {ui.stat("Money in", money(totals["total_in"]), tone="in")}
      {ui.stat("Money out", money(totals["total_out"]), tone="out")}
    </div>"""


def _review_transactions_content(items, categories=None, dup_item_id=None,
                                 dup_category=None,
                                 show_auto_suggestions=True,
                                 open_item_id=None):
    """The transaction queue: a scannable table over a focused drawer.

    The table stays the overview and the expert surface — sorting,
    selection and bulk actions all live here. Each row carries one
    primary action, Review, rather than six simultaneous controls
    (category, notes, evidence, approve, deny), which was the audit's
    misclick risk and its cognitive-load finding.

    Bulk is untouched by that move: the drawer's fields still carry
    data-bulk-name, and the checkbox points at its detail panel so
    table.js can find them wherever the panel currently sits.
    """
    if not items:
        return ui.empty_state(
            "Nothing waiting for review",
            "New transactions from your bank appear here after a check. "
            "Nothing is ever recorded without your say-so.",
            "/settings?tab=connections", "Check your connections")

    shown = items[:REVIEW_ROW_CAP]
    rows, panels = "", ""
    for item in shown:
        item_id = item["id"]

        if dup_item_id is not None and item_id == dup_item_id:
            rows += _review_duplicate_row(item, dup_category)
            continue

        transfer_tag = ('<span class="tag">Transfer</span>'
                        if item["direction"] == "transfer" else "")
        waited = days_since(item["date"])
        waiting_html = ""
        if waited is not None and waited >= 7:
            unit = "day" if waited == 1 else "days"
            waiting_html = (f'<span class="waiting warn">waiting {waited} '
                            f"{unit}</span>")
        _, _, cat_tag = _category_state(item, show_auto_suggestions)
        safe_vendor = escape(item["vendor"], quote=True)

        rows += f"""
        <tr id="row-{item_id}">
          <td class="select-cell">
            <input type="checkbox" data-row name="ids" data-name="ids"
                   value="{item_id}" data-detail="#detail-{item_id}"
                   aria-label="Select {safe_vendor}">
          </td>
          <td class="vendor-name" data-sort-value="{safe_vendor}">
            {escape(item["vendor"])}{transfer_tag}
            <div class="meta"><span>{escape(item["date"])}</span>
              <span>{escape(item["source"])}</span> {waiting_html}</div>
          </td>
          <td class="review-cat">{cat_tag}</td>
          <td class="num {item["direction"]}"
              data-sort-value="{item["amount"]}">{signed_money(item)}</td>
          <td>
            <button type="button" class="check" data-review="#detail-{item_id}"
                    aria-haspopup="dialog"
                    aria-label="Review {safe_vendor}">Review</button>
          </td>
        </tr>"""
        panels += _review_detail_panel(item, categories, show_auto_suggestions)

    overflow = ""
    if len(items) > REVIEW_ROW_CAP:
        overflow = (f'<div class="none-note">Showing the {REVIEW_ROW_CAP} '
                    f"oldest of {len(items)}. Clear some and the rest "
                    f"appear.</div>")

    bulk_approve = confirm_attrs(
        "Each one is recorded with the category shown on its row. This "
        "cannot be undone from Klearing.",
        title="Record the selected transactions?", confirm_label="Record all")
    bulk_deny = confirm_attrs(
        "They will be dismissed from this queue and never recorded.",
        title="Dismiss the selected transactions?",
        confirm_label="Dismiss all", danger=True)

    open_attr = f' data-open-detail="#detail-{open_item_id}"' if open_item_id else ""

    return f"""
    {_review_summary_strip(items)}
    <div class="sub">Check each transaction, then record it or dismiss it.
      Nothing is recorded without your approval.</div>
    <table class="table-hover" data-select="#review-bulk"
           data-review-table{open_attr}>
      <thead>
        <tr>
          <th class="select-cell">
            <input type="checkbox" data-all aria-label="Select all shown">
          </th>
          <th data-sort="text">From</th>
          <th data-sort="text">Category</th>
          <th class="num" data-sort="num">Amount</th>
          <th><span class="sr-only">Actions</span></th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    {overflow}
    <p class="hint"><kbd>j</kbd> <kbd>k</kbd> move, <kbd>Enter</kbd> opens,
      <kbd>Esc</kbd> closes.</p>
    <div class="review-panels is-hidden">{panels}</div>
    <div class="bulk-bar" id="review-bulk" role="status">
      <span class="count">0 selected</span>
      <span class="sep"></span>
      <form method="post" action="/review/approve" class="inline-form"
            {bulk_approve}>
        <button class="approve" type="submit">Approve</button>
      </form>
      <form method="post" action="/review/deny" class="inline-form"
            {bulk_deny}>
        <button class="deny" type="submit">Dismiss</button>
      </form>
      <button type="button" class="check" data-clear>Clear</button>
    </div>"""


def _review_drawer(aria_label):
    """The shared review-drawer chrome.

    Hoisted out of _review_transactions_content so any review queue can use
    it, not just transactions. The drawer itself holds no queue-specific
    markup: table.js fills the header from the opened panel's data-vendor /
    data-meta attributes and moves the panel into [data-drawer-slot], so a
    new row type only needs a [data-review="#detail-<id>"] trigger and a
    panel inside that tab's .review-panels container.

    Each tab renders alone, so a per-tab .review-panels stays unambiguous —
    it is only this chrome that is genuinely shared.
    """
    return f"""
    <div class="drawer-scrim" data-drawer-scrim></div>
    <aside class="drawer" id="review-drawer" role="dialog" aria-modal="true"
           aria-label="{escape(aria_label, quote=True)}" hidden>
      <div class="drawer-head">
        <div>
          <div class="who" data-drawer-vendor></div>
          <div class="meta" data-drawer-meta></div>
        </div>
        <button type="button" class="drawer-close" data-drawer-close>
          Close</button>
      </div>
      <div data-drawer-slot></div>
    </aside>"""


def _inventory_reconciliation_content(events):
    """Legacy quantity-only acquisitions, using the shared Review drawer."""
    if not events:
        return ui.empty_state(
            "No inventory acquisitions waiting",
            "Every acquisition is either Ledger-backed or has been explicitly "
            "acknowledged as unresolvable.",
            "/inventory?tab=stock", "Open Inventory")

    accounts = [account for account in ledger.list_accounts()
                if account["type"] in {"asset", "liability"}
                and account["code"] != "1200"]
    options = "".join(
        f'<option value="{escape(account["code"], quote=True)}">'
        f'{escape(account["code"])} {escape(account["name"])}</option>'
        for account in accounts)
    titles = db.shopify_variant_titles()
    rows, panels = "", ""
    for event in events[:REVIEW_ROW_CAP]:
        event_id = event["id"]
        title = titles.get(event["variant_id"]) or event["variant_id"]
        total = Decimal(int(event["quantity"]) * int(event["unit_cost_cents"])) / 100
        amount = ui.money(total)
        safe_title = escape(title, quote=True)
        detail_id = f"detail-inventory-{event_id}"
        post_confirm = confirm_attrs(
            f"This posts {amount} to Inventory and credits the selected funding "
            "account. The acquisition becomes eligible for dated FIFO costing.",
            title="Post this acquisition?", confirm_label="Post acquisition")
        unresolved_confirm = confirm_attrs(
            "This excludes the acquisition from COGS coverage permanently. "
            "Use this only when the funding cannot be established.",
            title="Acknowledge as unresolvable?",
            confirm_label="Acknowledge", danger=True)
        rows += f"""
        <tr>
          <td class="vendor-name" data-sort-value="{safe_title}">
            {escape(title)}<div class="meta">{escape(event["effective_date"])}
              &middot; legacy quantity-only acquisition</div></td>
          <td class="num" data-sort-value="{event["quantity"]}">{event["quantity"]}</td>
          <td class="num">{ui.money(Decimal(int(event["unit_cost_cents"])) / 100)}</td>
          <td class="num">{amount}</td>
          <td><button type="button" class="check" data-review="#{detail_id}"
                aria-haspopup="dialog" aria-label="Review {safe_title}">
                Review</button></td>
        </tr>"""
        panels += f"""
        <div class="review-detail is-hidden" id="{detail_id}"
             data-vendor="{safe_title}"
             data-meta="{escape(event["effective_date"], quote=True)} &middot; inventory acquisition"
             data-amount="{escape(amount, quote=True)}" data-direction="expense">
          <div class="drawer-body">
            <div class="drawer-amount expense">{amount}</div>
            <div class="drawer-field"><strong>{escape(title)}</strong>
              <div class="hint">{event["quantity"]} units at
                {ui.money(Decimal(int(event["unit_cost_cents"])) / 100)} each.
                This row predates Ledger-backed acquisition posting.</div></div>
            <div class="drawer-field">
              <label for="funding-{event_id}">Funding account</label>
              <select id="funding-{event_id}" name="funding_account_code"
                      form="inventory-post-{event_id}" required>
                <option value="">Choose bank or payable</option>{options}
              </select>
              <div class="hint">The selected account is credited for the
                historical acquisition. Do not guess.</div>
            </div>
            <div class="drawer-field">
              <label for="resolution-note-{event_id}">Note <span class="hint">required only if unresolvable</span></label>
              <input type="text" id="resolution-note-{event_id}"
                     name="note" form="inventory-unresolvable-{event_id}"
                     maxlength="500" placeholder="Why the funding cannot be established">
            </div>
          </div>
          <div class="drawer-foot">
            <form method="post" action="/review/inventory/{event_id}/post"
                  id="inventory-post-{event_id}" class="inline-form grow" {post_confirm}>
              <button class="approve" type="submit">Post acquisition</button>
            </form>
            <form method="post" action="/review/inventory/{event_id}/unresolvable"
                  id="inventory-unresolvable-{event_id}" class="inline-form" {unresolved_confirm}>
              <button class="deny" type="submit">Acknowledge unresolvable</button>
            </form>
          </div>
        </div>"""
    overflow = (f'<div class="none-note">Showing the {REVIEW_ROW_CAP} oldest '
                f'of {len(events)}.</div>' if len(events) > REVIEW_ROW_CAP else "")
    return f"""
    <div class="sub">These quantity-only acquisitions need one funding-account
      decision before they can affect Inventory or COGS. Post the real account,
      or acknowledge the row as unresolvable so it is excluded permanently.</div>
    <table class="table-hover" data-review-table>
      <thead><tr><th data-sort="text">Item</th>
        <th class="num" data-sort="num">Units</th>
        <th class="num" data-sort="num">Unit cost</th>
        <th class="num" data-sort="num">Total</th>
        <th><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>{overflow}
    <div class="review-panels is-hidden">{panels}</div>"""


# The dialog's accessible name, per queue. Generic-but-accurate beats one
# shared label: a screen-reader user hearing "Review transaction" on the
# invoice queue is being told something false.
REVIEW_DRAWER_LABELS = {
    "transactions": "Review transaction",
    "payments": "Review payment",
    "recurring": "Review recurring invoice",
    "followups": "Review follow-up",
    "invoices": "Review invoice reminder",
    "inventory": "Review inventory acquisition",
}


def _payment_match_row(match):
    """A detected-but-unapplied payment, as a review row + drawer panel.

    Uses the hoisted review drawer exactly like the transactions queue: a
    Review trigger points at a #detail-<id> panel that carries the header
    text in data-vendor/data-meta and holds the apply/dismiss forms. The
    id keeps the #detail- prefix so table.js's row-highlight finds it.
    """
    invoice = db.get_invoice(match["invoice_id"]) or {}
    contact = db.get_contact(invoice.get("contact_id")) or {}
    number = invoice.get("invoice_number") or f"draft {match['invoice_id']}"
    who = contact.get("display_name") or "Unknown customer"
    gross = ui.money(Decimal(int(match["amount_cents"])) / 100)
    source = "Online (Stripe)" if match["source"] == "stripe" else "Bank deposit"
    element_id = f"pm-{match['id']}"
    meta = (f"{escape(match['paid_on'])} &middot; {escape(source)} &middot; "
            f"invoice {escape(number)}")

    fee_note = ""
    if match["fee_cents"]:
        fee = ui.money(Decimal(int(match["fee_cents"])) / 100)
        net = ui.money(Decimal(int(match["amount_cents"]) - int(match["fee_cents"]))
                       / 100)
        fee_note = (f'<div class="hint">Processor fee {fee} is recorded '
                    f"separately, so {net} lands in cash and the fee is an "
                    "expense.</div>")

    apply_confirm = confirm_attrs(
        f"This records {gross} against invoice {number} and posts it to your "
        "Ledger.", title="Apply this payment?", confirm_label="Apply payment",
        busy_label="Applying…")
    dismiss_confirm = confirm_attrs(
        "This is not a payment for that invoice. It will be dropped from this "
        "queue; a bank deposit then posts as ordinary income on the next "
        "sync.", title="Not this payment?", confirm_label="Not this one",
        danger=True)

    row = f"""
    <tr id="row-{element_id}">
      <td>{escape(who)}</td>
      <td>{escape(source)} &middot; invoice {escape(number)}</td>
      <td class="num income">{gross}</td>
      <td>
        <button type="button" class="check" data-review="#detail-{element_id}"
                aria-haspopup="dialog"
                aria-label="Review payment for {escape(number, quote=True)}">
          Review</button>
      </td>
    </tr>"""

    panel = f"""
    <div class="review-detail is-hidden" id="detail-{element_id}"
         data-vendor="{escape(who, quote=True)}"
         data-meta="{meta}">
      <div class="drawer-body">
        <div class="drawer-amount income">{gross}</div>
        <p class="hint">{escape(match.get('detail') or '')}</p>
        {fee_note}
      </div>
      <div class="drawer-foot">
        <form method="post" action="/review/payment/{match['id']}/apply"
              class="inline-form grow" {apply_confirm}>
          <button class="approve" type="submit">Apply payment</button>
        </form>
        <div class="deny-slot">
          <form method="post" action="/review/payment/{match['id']}/dismiss"
                class="inline-form" {dismiss_confirm}>
            <button type="submit">Not this one</button>
          </form>
        </div>
      </div>
    </div>"""
    return row, panel


def _overdue_row(view):
    """An overdue invoice, as a review row proposing a reminder or a
    write-off. Derived from invoice state, not a stored queue item, so it
    carries no badge (see review_counts)."""
    invoice = view["invoice"]
    contact = db.get_contact(invoice["contact_id"]) or {}
    number = invoice["invoice_number"] or f"draft {invoice['id']}"
    who = contact.get("display_name") or "Unknown customer"
    outstanding = ui.money(Decimal(view["balance"]) / 100)
    days = view["days_overdue"]
    element_id = f"od-{invoice['id']}"
    meta = (f"{days} day{'s' if days != 1 else ''} overdue &middot; "
            f"due {escape(invoice['due_date'])} &middot; invoice {escape(number)}")

    write_off_confirm = confirm_attrs(
        f"This writes {outstanding} off as uncollectible and posts it to your "
        "Ledger.", title="Write this invoice off?", confirm_label="Write off",
        danger=True)

    row = f"""
    <tr id="row-{element_id}">
      <td>{escape(who)}</td>
      <td>{escape(number)} &middot; {days} day{'s' if days != 1 else ''} overdue</td>
      <td class="num">{outstanding}</td>
      <td>
        <button type="button" class="check" data-review="#detail-{element_id}"
                aria-haspopup="dialog"
                aria-label="Review overdue invoice {escape(number, quote=True)}">
          Review</button>
      </td>
    </tr>"""

    email_note = ("" if invoice["payment_link_url"] else
                  '<p class="hint">No online payment link on this invoice yet '
                  "— the reminder still sends; add a link from the invoice "
                  "page to let them pay in a click.</p>")
    panel = f"""
    <div class="review-detail is-hidden" id="detail-{element_id}"
         data-vendor="{escape(who, quote=True)}"
         data-meta="{meta}">
      <div class="drawer-body">
        <div class="drawer-amount">{outstanding} outstanding</div>
        {email_note}
      </div>
      <div class="drawer-foot">
        <form method="post" action="/invoices/{invoice['id']}/email"
              class="inline-form grow">
          <button class="approve" type="submit">Send reminder</button>
        </form>
        <div class="deny-slot">
          <form method="post" action="/invoices/{invoice['id']}/write-off"
                class="inline-form" {write_off_confirm}>
            <button type="submit">Write off</button>
          </form>
        </div>
      </div>
    </div>"""
    return row, panel


def _review_payments_content(matches):
    """The Payments queue: detected payments to apply, and overdue invoices
    to chase — both through the shared review drawer."""
    today = date.today().isoformat()
    overdue = [v for v in (_invoice_view(inv, today)
                           for inv in db.list_invoices(include_voided=False))
               if v["status"] == "overdue"]

    if not matches and not overdue:
        return ui.empty_state(
            "No payments waiting",
            "Detected payments to apply and overdue invoices to chase show "
            "up here. Connect Stripe or a bank to catch payments "
            "automatically.")

    sections = []
    panels = ""

    if matches:
        rows = ""
        for match in matches:
            row, panel = _payment_match_row(match)
            rows += row
            panels += panel
        sections.append(f"""
        <div class="section">
          <div class="section-head"><h2>Detected payments</h2></div>
          <div class="sub">Klearing spotted these but posts nothing until you
            say so.</div>
          <table class="table-hover">
            <thead><tr><th data-sort="text">Customer</th>
              <th data-sort="text">Source</th>
              <th class="num" data-sort="num">Amount</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>""")

    if overdue:
        rows = ""
        for view in sorted(overdue, key=lambda v: -v["days_overdue"]):
            row, panel = _overdue_row(view)
            rows += row
            panels += panel
        sections.append(f"""
        <div class="section">
          <div class="section-head"><h2>Overdue invoices</h2></div>
          <table class="table-hover">
            <thead><tr><th data-sort="text">Customer</th>
              <th data-sort="text">Invoice</th>
              <th class="num" data-sort="num">Outstanding</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>""")

    return ("".join(sections)
            + f'<div class="review-panels is-hidden">{panels}</div>')


def _review_duplicate_row(item, dup_category):
    """A suspected duplicate, deliberately held out of the normal flow."""
    existing = db.get_wave_posted(item["external_transaction_id"]) or {}
    posted_at = (existing.get("posted_at") or "")[:10]
    posted_by = existing.get("posted_by") or "another tool"
    when = f" on {escape(posted_at)}" if posted_at else " earlier"
    category = dup_category or item.get("suggested_category") or ""
    skip_confirm = confirm_attrs(
        "It won't be recorded.",
        title="Skip this transaction?", confirm_label="Skip",
        busy_label="Skipping…")
    post_confirm = confirm_attrs(
        "This creates a duplicate in your books. Only do this if you are "
        "sure the first one was removed.",
        title="Record a second copy?", confirm_label="Record duplicate",
        danger=True)
    return f"""
    <tr class="is-duplicate">
      <td class="select-cell"></td>
      <td class="vendor-name">{escape(item["vendor"])}
        <div class="meta"><span>{escape(item["date"])}</span>
          <span class="dash-flag">Already posted by
            {escape(posted_by)}{when}</span></div>
      </td>
      <td colspan="2"><span class="tag warn">Possible duplicate</span></td>
      <td class="num {item["direction"]}">{signed_money(item)}</td>
      <td>
        <div class="row-inline">
          <form method="post" action="/skip/{item["id"]}" class="inline-form"
                {skip_confirm}>
            <button class="check" type="submit">Skip</button>
          </form>
          <form method="post" action="/approve/{item["id"]}"
                class="inline-form" {post_confirm}>
            <input type="hidden" name="category"
                   value="{escape(category, quote=True)}">
            <button class="deny" type="submit">Post anyway</button>
          </form>
        </div>
      </td>
    </tr>"""


# The tab keys are URL values (/review?tab=invoices) and appear in saved
# links, the command palette and several in-copy hrefs, so they stay put.
# Only the labels changed: "Invoices" here chases invoices that live in
# *Wave*, which is a different thing from the invoices Klearing now writes
# itself at /invoices. Two unrelated things sharing one word in the same app
# is a support question waiting to happen.
REVIEW_TABS = (
    ("transactions", "Transactions"),
    ("payments", "Payments"),
    ("recurring", "Recurring"),
    ("followups", "Follow-ups"),
    ("invoices", "Invoice reminders"),
    ("inventory", "Inventory acquisitions"),
)


def review_counts():
    """How much is waiting across the review queues, for the nav badge.

    The Payments count is pending payment MATCHES only -- deposits and
    online payments detected but not yet applied. Overdue invoices also
    appear on that tab, but they are a standing state, not a queue that
    clears, so they must not drive a badge that then never returns to zero
    (see render_sidebar's badge docstring).
    """
    return {
        "transactions": len(db.list_items(status="pending")),
        "payments": len(db.list_pending_payment_matches()),
        "recurring": len(db.list_recurring_instances(status="pending_review")),
        "followups": len(db.list_quote_followups(status="pending_review")),
        "invoices": len(db.list_invoice_reminders(status="pending_review")),
        "inventory": len(db.list_inventory_reconciliation_queue()),
    }


def render_review_page(tab="transactions", message=None, error=None,
                       dup_item_id=None, dup_category=None,
                       open_item_id=None):
    """The three review queues under one roof.

    Pending, Follow-ups and Invoices were three sidebar entries serving
    one habit: clear what is waiting. Splitting them meant the only way
    to know how much was outstanding was to visit three pages, two of
    which are usually empty.
    """
    db.init_db()
    items = db.list_items(status="pending")
    followups = db.list_quote_followups(status="pending_review")
    invoices = db.list_invoice_reminders(status="pending_review")
    matches = db.list_pending_payment_matches()
    recurring = db.list_recurring_instances(status="pending_review")
    inventory_events = db.list_inventory_reconciliation_queue()
    counts = {"transactions": len(items), "payments": len(matches),
              "recurring": len(recurring), "followups": len(followups),
              "invoices": len(invoices), "inventory": len(inventory_events)}
    active = tab if tab in counts else "transactions"

    # Follow-ups and Invoices render their own inline notice when email
    # is unconfigured, so the shell must not repeat it above them.
    shell_error = error
    if (error == EMAIL_NOT_CONFIGURED
            and active in ("followups", "invoices")):
        shell_error = None
    if active == "transactions":
        categories = get_categories() if items else None
        show_auto = db.get_setting("auto_categorize_enabled") != "0"
        body = _review_transactions_content(
            items, categories=categories, dup_item_id=dup_item_id,
            dup_category=dup_category, show_auto_suggestions=show_auto,
            open_item_id=open_item_id)
    elif active == "payments":
        body = _review_payments_content(matches)
    elif active == "recurring":
        body = _review_recurring_content(recurring)
    elif active == "followups":
        body = _review_followups_content(followups, error)
    elif active == "inventory":
        body = _inventory_reconciliation_content(inventory_events)
    else:
        body = _review_invoices_content(invoices, error)

    nav = ui.sub_nav(
        [(key, label, f"/review?tab={key}", counts[key] or None)
         for key, label in REVIEW_TABS],
        active, "Review queues")

    lock_form = ""
    if app_lock.is_configured():
        lock_form = """
        <form method="post" action="/lock" class="inline-form">
          <button class="deny" type="submit">Lock</button>
        </form>"""
    header_extra = f"""
      <div class="header-actions">
        <form method="post" action="/check" class="inline-form">
          <button class="check" type="submit">Check for new transactions</button>
        </form>{lock_form}
      </div>"""

    drawer = _review_drawer(REVIEW_DRAWER_LABELS.get(active, "Review item"))
    content = f"{render_update_banner()}{nav}{body}{drawer}"
    return render_shell("Review", header_extra, content, message, shell_error,
                        wide=True,
                        nav_badges={"Review": sum(counts.values())})


# ---------------------------------------------------------------- Summary


def summarize(items):
    """
    Totals and per-category rollups for a list of items. Transfers
    (owner contributions in/out) are broken out on their own and
    excluded from total_in/total_out/net, so they never inflate
    real revenue or expense numbers.
    """
    def rollup(rows):
        agg = {}
        for row in rows:
            category = row.get("suggested_category") or "No suggestion"
            entry = agg.setdefault(category, {"count": 0, "total": 0.0})
            entry["count"] += 1
            entry["total"] += row["amount"]
        return sorted(agg.items(), key=lambda kv: -kv[1]["total"])

    income = [i for i in items if i["direction"] == "income"]
    expense = [i for i in items if i["direction"] == "expense"]
    transfer = [i for i in items if i["direction"] == "transfer"]
    total_in = sum(i["amount"] for i in income)
    total_out = sum(i["amount"] for i in expense)
    return {
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "transfer_total": sum(i["amount"] for i in transfer),
        "income": rollup(income),
        "expense": rollup(expense),
        "transfer": rollup(transfer),
    }


def _delta_pill_money(current, prior, kind):
    """Absolute-dollar change pill for the category tables. kind:
    'income' colors by direction (up green, down red); 'neutral' stays
    neutral both ways — same judgment as the totals row's T2b deltas
    (spending more isn't inherently good or bad). Dollar change, not
    percent: a category that didn't exist last period reads as its
    honest full amount, not a meaningless percent-of-zero."""
    change = current - prior
    if abs(change) < 0.005:
        return '<span class="delta neutral">no change</span>'
    arrow = "&#9650;" if change > 0 else "&#9660;"
    sign = "+" if change > 0 else "-"
    if kind == "income":
        css = "up" if change > 0 else "down"
    else:
        css = "neutral"
    return (f'<span class="delta {css}">{arrow} '
            f'{sign}{money(abs(change))}</span>')


def render_breakdown_table(title, rows, prior_rows=None, delta_kind=None,
                           link_params=None):
    """One category table. With prior_rows (the prior period's rollup,
    same shape), a "vs prior" dollar-delta column is added; without it
    the column is absent entirely — never an empty header.

    link_params, when given ({"direction", "status", "start", "end"}),
    makes each category name a link to /summary/category — the same
    drill-down-to-transactions shape as the Vendors page's per-vendor
    link. Synthetic rows (Gusto payroll — live from Gusto, not real
    pending_items rows) never link, since there's nothing to drill into."""
    if not rows:
        return f"<h3>{escape(title)}</h3><div class='none-note'>None in this range.</div>"

    with_delta = prior_rows is not None
    prior_totals = ({category: entry["total"]
                     for category, entry in prior_rows} if with_delta else {})
    body = ""
    for category, entry in rows:
        delta_cell = ""
        if with_delta:
            pill = _delta_pill_money(entry["total"],
                                     prior_totals.get(category, Decimal(0)),
                                     delta_kind)
            delta_cell = f'<td class="num">{pill}</td>'
        is_synthetic = bool(entry.get("gusto"))
        row_cls = ""
        if link_params and not is_synthetic:
            label = (f'<a href="/summary/category?category={quote(category)}'
                     f'&direction={link_params["direction"]}'
                     f'&status={link_params["status"]}'
                     f'&start={quote(link_params["start"])}'
                     f'&end={quote(link_params["end"])}">'
                     f'{escape(category)}</a>')
            row_cls = "row-link"
        else:
            label = escape(category)
        if is_synthetic:
            label += ' <span class="tag">from Gusto</span>'
        body += f"""
        <tr class="{row_cls}">
          <td>{label}</td>
          <td class="num">{entry["count"]}</td>
          <td class="num">{money(entry["total"])}</td>
          {delta_cell}
        </tr>"""

    delta_head = '<th class="num" data-sort="num">vs prior</th>' if with_delta else ""
    return f"""
    <h3>{escape(title)}</h3>
    <table class="table-hover">
      <thead><tr><th data-sort="text">Category</th><th class="num" data-sort="num">Count</th><th class="num" data-sort="num">Total</th>{delta_head}</tr></thead>
      <tbody>{body}</tbody>
    </table>"""


def summary_has_rows(summary):
    return bool(summary["income"] or summary["expense"] or summary["transfer"])


def net_commentary(summary, current_month):
    """One plain-language line about the section's net, or None for an
    empty section (no sentence is forced onto an empty state). Threshold
    rules only — deterministic, no AI."""
    if not summary_has_rows(summary):
        return None
    when = "this month" if current_month else "in this range"
    if summary["net"] < 0:
        return (f"You spent more than you made {when} — normal pre-launch, "
                f"worth watching if it continues.")
    return f"You're net positive {when}."


def render_summary_section(heading, summary, unposted=False, commentary=None,
                           expense_chart=False, prior_summary=None,
                           prior_label="", gusto_payroll=None,
                           gusto_note="", start_date=None, end_date=None,
                           status=None):
    tag = '<span class="tag">not yet posted</span>' if unposted else ""
    net = summary["net"]
    net_sign = "+" if net >= 0 else "-"
    commentary_html = (f'<div class="commentary">{escape(commentary)}</div>'
                       if commentary else "")

    # T2b: change vs the immediately-preceding period, under each figure.
    # Money in uses good=up (more revenue is good). Money out is neutral —
    # spending more isn't inherently good or bad. Net shows the absolute
    # dollar change, not a percentage: net is signed and can cross zero,
    # where a percent-change against a negative or near-zero base is
    # misleading, but the sign of the dollar change is always meaningful.
    in_cmp = out_cmp = net_cmp = compare_note = ""
    if prior_summary is not None:
        in_cmp = (f'<div class="delta-line">'
                  f'{_delta_pill(summary["total_in"], prior_summary["total_in"])}'
                  f'</div>')
        out_cmp = (f'<div class="delta-line">'
                   f'{_delta_pill(summary["total_out"], prior_summary["total_out"], "neutral")}'
                   f'</div>')
        net_change = summary["net"] - prior_summary["net"]
        net_kind = "up" if net_change >= 0 else "down"
        net_arrow = "&#9650;" if net_change >= 0 else "&#9660;"
        net_cmp = (f'<div class="delta-line"><span class="delta {net_kind}">'
                   f'{net_arrow} {money(abs(net_change))}</span></div>')
        compare_note = (f'<div class="snapshot-meta">Change vs the previous '
                        f'period ({escape(prior_label)}).</div>')

    # T2a: top expense categories as a bar chart above the detailed tables
    # — the same data the "Expenses by category" table holds, shown as a
    # glance-able proportion view (reuses horizontal_bar_chart, same as
    # Home and Vendors). Only when there are expenses to chart.
    expense_chart_html = ""
    if expense_chart and summary["expense"]:
        expense_chart_html = charts.horizontal_bar_chart(
            [{"label": category, "value": entry["total"],
              "value_label": money(entry["total"])}
             for category, entry in summary["expense"][:8]],
            aria="Top expense categories this period", bar_h=22, gap=8)

    def _link_params(direction):
        if start_date is None or end_date is None or status is None:
            return None
        return {"direction": direction, "status": status,
                "start": start_date, "end": end_date}

    return f"""
    <div class="section{' unposted' if unposted else ''}">
      <div class="section-head"><h2>{escape(heading)}</h2>{tag}</div>
      <div class="totals">
        <div><div class="label">Money in</div><div class="value in">+{money(summary["total_in"])}</div>{in_cmp}</div>
        <div><div class="label">Money out</div><div class="value out">-{money(summary["total_out"])}</div>{out_cmp}</div>
        <div><div class="label">Net</div><div class="value">{net_sign}{money(abs(net))}</div>{net_cmp}</div>
        <div><div class="label">Transfers</div><div class="value transfer">{money(summary["transfer_total"])}</div></div>
      </div>
      {compare_note}
      {commentary_html}
      {expense_chart_html}
      {render_breakdown_table(
          "Expenses by category",
          summary["expense"] + ([("Payroll", {
              "count": gusto_payroll["count"],
              "total": gusto_payroll["total"], "gusto": True})]
              if gusto_payroll else []),
          prior_rows=prior_summary["expense"] if prior_summary else None,
          delta_kind="neutral", link_params=_link_params("expense"))}
      {gusto_note}
      {render_breakdown_table(
          "Income by category", summary["income"],
          prior_rows=prior_summary["income"] if prior_summary else None,
          delta_kind="income", link_params=_link_params("income"))}
      {render_breakdown_table(
          "Transfers by category", summary["transfer"],
          prior_rows=prior_summary["transfer"] if prior_summary else None,
          delta_kind="neutral", link_params=_link_params("transfer"))}
    </div>"""


SUMMARY_SPARK_WEEKS = 10


def weekly_net_series(n_weeks=SUMMARY_SPARK_WEEKS):
    """Net (money in minus money out, transfers excluded) for each of the
    last n completed Mon-Sun weeks, active business. Reuses the Summary
    net calc (summarize) across trailing weeks for the sparkline — no new
    calculation, just more windows. Returns (values, labels) oldest
    first, with weeks before any activity trimmed so the line isn't a
    misleading run of leading zeros."""
    import weekly_digest
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    approved = [i for i in db.list_items(status="approved")
                if active_id is None or i.get("business_id") in (None, active_id)]
    if not approved:
        return [], []
    earliest = min(i["date"] for i in approved)
    this_start, _te, _ps, _pe = weekly_digest.week_bounds(date.today())
    values, labels = [], []
    for k in range(n_weeks - 1, -1, -1):
        wk_start = this_start - timedelta(days=7 * k)
        wk_end = wk_start + timedelta(days=6)
        if wk_end.isoformat() < earliest:
            continue  # before any activity — don't pad with zeros
        window = [i for i in approved
                  if wk_start.isoformat() <= i["date"] <= wk_end.isoformat()]
        values.append(round(summarize(window)["net"], 2))
        labels.append(wk_start.strftime("%b %d"))
    return values, labels


def _month_windows(months):
    """(label, start_iso, end_iso) for the last `months` calendar months,
    oldest first, ending with the current month. Shared by the monthly
    net series and the Growth tab's revenue-by-category trend."""
    today = date.today()
    seq = []
    year, month = today.year, today.month
    for k in range(months):
        mm, yy = month - k, year
        while mm <= 0:
            mm += 12
            yy -= 1
        seq.append((yy, mm))
    seq.reverse()
    windows = []
    for yy, mm in seq:
        last = calendar.monthrange(yy, mm)[1]
        windows.append((date(yy, mm, 1).strftime("%b"),
                        date(yy, mm, 1).isoformat(),
                        date(yy, mm, last).isoformat()))
    return windows


def _active_approved():
    """Approved items for the active business (or all if none active)."""
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    return [i for i in db.list_items(status="approved")
            if active_id is None or i.get("business_id") in (None, active_id)], active_id


def monthly_net_series(months=12):
    """Net per calendar month over the trailing months, active business —
    the 12-month view of the Summary sparkline. Reuses summarize(); trims
    months before any activity."""
    approved, _active_id = _active_approved()
    if not approved:
        return [], []
    earliest = min(i["date"] for i in approved)
    values, labels = [], []
    for label, start, end in _month_windows(months):
        if end < earliest:
            continue
        window = [i for i in approved if start <= i["date"] <= end]
        values.append(round(summarize(window)["net"], 2))
        labels.append(label)
    return values, labels


# Trend-window toggle used by Summary and Insights sparklines.
TREND_RANGES = [("8w", "8 weeks"), ("12w", "12 weeks"), ("12mo", "12 months")]
TREND_SNAPSHOT_LIMIT = {"8w": 60, "12w": 90, "12mo": 370}


def net_series(window="8w"):
    """(values, labels) for the chosen trend window — weekly for 8w/12w,
    monthly for 12mo."""
    if window == "12mo":
        return monthly_net_series(12)
    return weekly_net_series(12 if window == "12w" else 8)


def render_range_toggle(base_path, current, extra_qs=""):
    """A small 8w / 12w / 12mo toggle, preserving any other query string.
    Marked up as tabs (tablist/tab/aria-selected) so screen readers hear
    which window is active, not just a row of identical links."""
    links = ""
    for key, label in TREND_RANGES:
        active = key == current
        cls = "btn-link primary" if active else "btn-link"
        selected = "true" if active else "false"
        sep = "&" if extra_qs else ""
        links += (f'<a class="{cls}" role="tab" aria-selected="{selected}" '
                  f'href="{base_path}?{extra_qs}{sep}'
                  f'range={key}">{label}</a>')
    return (f'<div class="range-toggle" role="tablist" '
            f'aria-label="Trend window">{links}</div>')


def render_summary_page(start_date, end_date, approved_summary, pending_summary,
                        trend_range="8w", prior_summary=None, prior_label="",
                        gusto_payroll=None, gusto_note="",
                        message=None, error=None):
    has_any_rows = any(
        summary[key]
        for summary in (approved_summary, pending_summary)
        for key in ("income", "expense", "transfer")
    )
    empty_note = "" if has_any_rows else """
    <div class="empty mb-4">
      <p><strong>No activity in this date range.</strong></p>
      <p>Try widening the range, or approve some transactions on the
        <a href="/review?tab=transactions">Pending tab</a>.</p>
    </div>"""

    # "this month" wording is only accurate on the default current-month
    # view; a custom range gets "in this range" instead.
    month_start, month_end = default_range()
    current_month = (start_date == month_start and end_date == month_end)
    approved_commentary = net_commentary(approved_summary, current_month)

    # Net over the trailing window (8w / 12w / 12mo toggle), above the
    # current-period numbers.
    spark_values, spark_labels = net_series(trend_range)
    spark_last = None
    if spark_values:
        last = spark_values[-1]
        spark_last = ("+" if last >= 0 else "-") + money(abs(last))
    net_spark = charts.sparkline(
        spark_values, labels=spark_labels, last_label=spark_last,
        zero_baseline=True, grid=True,
        aria="Net position over the trailing window")
    range_toggle = render_range_toggle(
        "/summary", trend_range,
        extra_qs=f"start={quote(start_date)}&end={quote(end_date)}")
    spark_section = f"""
    <div class="section">
      <div class="section-head"><h2>Net over time</h2></div>
      {range_toggle}
      {net_spark}
      <div class="snapshot-meta">A trailing window from today — this trend
        doesn't change with the date range above.</div>
    </div>"""

    export_query = f"start={quote(start_date)}&end={quote(end_date)}"
    content = f"""
    <div class="sub">Approved totals are recorded in your books. Pending totals are still waiting for review.</div>
    <form class="range-form" method="get" action="/summary">
      <label>From <input type="date" name="start" value="{start_date}"></label>
      <label>To <input type="date" name="end" value="{end_date}"></label>
      <button class="check" type="submit">View</button>
    </form>
    <div class="export-actions">
      <a class="btn-link" href="/export/csv?{export_query}">Download CSV</a>
      <a class="btn-link" href="/export/accountant?{export_query}">Export everything for my accountant</a>
    </div>
    {empty_note}
    {spark_section}
    {render_summary_section("Approved", approved_summary,
                            commentary=approved_commentary,
                            expense_chart=True,
                            prior_summary=prior_summary,
                            prior_label=prior_label,
                            gusto_payroll=gusto_payroll,
                            gusto_note=gusto_note,
                            start_date=start_date, end_date=end_date,
                            status="approved")}
    {render_summary_section("Pending", pending_summary, unposted=True,
                            start_date=start_date, end_date=end_date,
                            status="pending")}"""

    return render_shell("Summary", "", content, message, error)


def default_range():
    """First to last day of the current month."""
    today = date.today()
    first = today.replace(day=1)
    last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return first.isoformat(), last.isoformat()


def clean_date(value, fallback):
    """Return value if it's a valid YYYY-MM-DD date, else the fallback."""
    if value:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return fallback


# --------------------------------------------------------------- Exports


def approved_for_export(start_date, end_date):
    """Approved transactions in [start, end] for the active business —
    the same set the Summary 'Approved' section shows."""
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    return db.list_items_in_range("approved", start_date, end_date,
                                  business_id=active_id)


def signed_amount(item):
    """Amount as a signed number for exports: expenses negative, money
    in positive. Encodes direction in one column an accountant can sum."""
    amount = item["amount"]
    return -amount if item["direction"] == "expense" else amount


def build_transactions_csv(items):
    """Approved transactions as CSV text: date, vendor, amount (signed),
    category, notes."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Vendor", "Amount", "Category", "Notes"])
    for item in items:
        writer.writerow([
            item["date"],
            item["vendor"],
            f"{signed_amount(item):.2f}",
            item["suggested_category"],
            (item.get("notes") or ""),
        ])
    return buffer.getvalue()


def build_summary_txt(start_date, end_date, items, generated_at):
    """A P&L-style plain-text summary: totals and per-category rollups
    for the range, for the accountant bundle."""
    summary = summarize(items)
    active = business_registry.active_business() or {}
    net = summary["net"]
    net_sign = "+" if net >= 0 else "-"
    lines = [
        f"{APP_NAME} — Financial Summary",
        f"Business:  {active.get('name', 'n/a')}",
        f"Range:     {start_date} to {end_date}",
        f"Generated: {generated_at}",
        f"Transactions: {len(items)} approved",
        "",
        "TOTALS",
        "------",
        f"  Money in:   +{money(summary['total_in'])}",
        f"  Money out:  -{money(summary['total_out'])}",
        f"  Net:        {net_sign}{money(abs(net))}",
        f"  Transfers:  {money(summary['transfer_total'])}",
        "",
        "EXPENSES BY CATEGORY",
        "--------------------",
    ]
    if summary["expense"]:
        for category, entry in summary["expense"]:
            lines.append(f"  {category}: {money(entry['total'])} "
                         f"({entry['count']})")
    else:
        lines.append("  None in this range.")
    lines += ["", "INCOME BY CATEGORY", "------------------"]
    if summary["income"]:
        for category, entry in summary["income"]:
            lines.append(f"  {category}: {money(entry['total'])} "
                         f"({entry['count']})")
    else:
        lines.append("  None in this range.")
    if summary["transfer"]:
        lines += ["", "TRANSFERS BY CATEGORY", "---------------------"]
        for category, entry in summary["transfer"]:
            lines.append(f"  {category}: {money(entry['total'])} "
                         f"({entry['count']})")
    lines += [
        "",
        "(Transfers are owner contributions, excluded from the income "
        "and expense totals above.)",
        "Amounts in transactions.csv are signed: expenses negative, "
        "money in positive.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- Home


def _dash_card(href, kicker, body):
    return f"""
    <a class="dash-card" href="{href}">
      <div class="dash-kicker"><span>{escape(kicker)}</span>
        <span class="go">View &rarr;</span></div>
      {body}
    </a>"""


def _delta_pill(current, prior, kind=None):
    """A small change-indicator pill for dash cards: ▲/▼ + percentage,
    colored by direction (or forced with kind='up'/'down'/'watch').
    Falls back to plain text when there's no prior to compare against."""
    text = pct_change_text(current, prior)
    if not text.startswith(("+", "-")):
        return f"({escape(text)})"
    if kind is None:
        kind = "up" if text.startswith("+") else "down"
    arrow = "&#9650;" if text.startswith("+") else "&#9660;"
    return (f'<span class="delta {kind}">{arrow} '
            f'{escape(text.lstrip("+-"))}</span>')


def dash_needs_review():
    """Everything waiting for a human, across all three queues, with
    the single most-urgent item previewed. 'Most urgent' = the largest
    dollar amount in the bank queue (money is what attention should
    follow; a stale $3 charge can wait), oldest as the tie-break."""
    pending = db.list_items(status="pending")
    followups = db.list_quote_followups(status="pending_review")
    invoices = db.list_invoice_reminders(status="pending_review")
    total = len(pending) + len(followups) + len(invoices)

    if total == 0:
        body = """
        <div class="dash-value mixed">All clear</div>
        <div class="dash-sub">Nothing waiting for review.</div>"""
        return _dash_card("/review?tab=transactions", "Needs review", body)

    parts = []
    if pending:
        parts.append(f"{len(pending)} transaction(s)")
    if followups:
        parts.append(f"{len(followups)} follow-up(s)")
    if invoices:
        parts.append(f"{len(invoices)} invoice reminder(s)")

    preview = ""
    if pending:
        urgent = sorted(pending, key=lambda i: (-i["amount"], i["date"]))[0]
        preview = (f'<div class="dash-sub">Largest: '
                   f'{escape(urgent["vendor"])} '
                   f"{escape(signed_money(urgent))} &middot; "
                   f'{escape(urgent["date"])}</div>')

    body = f"""
    <div class="dash-value mixed">{total} waiting</div>
    <div class="dash-sub">{escape(" · ".join(parts))}</div>
    {preview}"""
    return _dash_card("/review?tab=transactions", "Needs review", body)


def dash_month():
    """This month at a glance — same numbers the Summary tab shows for
    the current month (approved only, active business)."""
    start, end = default_range()
    active = business_registry.active_business()
    approved = db.list_items_in_range(
        "approved", start, end,
        business_id=active["id"] if active else None)
    if not approved:
        body = """
        <div class="dash-note">Nothing approved yet this month. Numbers
          appear here as you approve transactions.</div>"""
        return _dash_card("/summary", "This month", body)

    summary = summarize(approved)
    net = summary["net"]
    net_sign = "+" if net >= 0 else "-"

    # A tiny weekly-net column chart under the headline numbers — the
    # same series the Summary sparkline plots, at glance size.
    spark_values, spark_labels = weekly_net_series(8)
    signed_labels = [
        f"Week of {label}: {'+' if v >= 0 else '-'}{money(abs(v))}"
        for label, v in zip(spark_labels, spark_values)
    ]
    mini = charts.mini_bars(spark_values, labels=signed_labels,
                            aria="Weekly net, last 8 weeks")
    mini_caption = ('<div class="dash-sub">Weekly net, last 8 weeks</div>'
                    if mini else "")
    body = f"""
    <div class="dash-row">
      <div><div class="dash-mini-label">In</div>
        <div class="dash-value in">+{money(summary["total_in"])}</div></div>
      <div><div class="dash-mini-label">Out</div>
        <div class="dash-value out">-{money(summary["total_out"])}</div></div>
      <div><div class="dash-mini-label">Net</div>
        <div class="dash-value">{net_sign}{money(abs(net))}</div></div>
    </div>
    {mini}{mini_caption}"""
    return _dash_card("/summary", "This month", body)


def dash_top_categories():
    """Top expense categories this month — the "where did the money go"
    view Summary already computes (summarize()'s expense rollup),
    surfaced on Home so it doesn't take a trip to Summary to see."""
    start, end = default_range()
    active = business_registry.active_business()
    approved = db.list_items_in_range(
        "approved", start, end,
        business_id=active["id"] if active else None)
    expense_rows = summarize(approved)["expense"] if approved else []
    if not expense_rows:
        body = """
        <div class="dash-note">No approved expenses yet this month.</div>"""
        return _dash_card("/summary", "Top categories", body)

    chart = charts.horizontal_bar_chart(
        [{"label": category, "value": entry["total"],
          "value_label": money(entry["total"])}
         for category, entry in expense_rows[:4]],
        aria="Top expense categories this month", bar_h=22, gap=8)
    return _dash_card("/summary", "Top categories", chart)


def dash_vendor_flag():
    """Vendor spend-jump status. Mirrors Reconcile's pattern: a calm
    positive state when nothing's flagged (not just an absent card,
    which reads as "nothing to see" rather than "checked, all clear"),
    and a lighter not-enough-history note when there's no expense
    history to check yet."""
    rows, _threshold = vendor_stats()
    if not rows:
        body = """
        <div class="dash-note">Not enough expense history yet to check for
          spend jumps.</div>"""
        return _dash_card("/vendors", "Vendor spend jump", body)
    flagged = [r for r in rows if r["jump_pct"] is not None]
    if not flagged:
        body = """
        <div class="dash-ok">No vendor spend jumps this month.</div>"""
        return _dash_card("/vendors", "Vendor spend jump", body)
    top = max(flagged, key=lambda r: r["jump_pct"])
    body = f"""
    <div class="dash-value dash-flag">+{top["jump_pct"]:.0f}%</div>
    <div class="dash-sub">{escape(top["vendor"])}
      <span class="delta watch">&#9650; vs last month</span>
      &middot; {money(top["this_month"])} so far</div>"""
    return _dash_card("/vendors", "Vendor spend jump", body)


def dash_traffic():
    """This week's sessions + the top conversion event, from the
    stored GA4 snapshot (never a live GA4 call)."""
    if not ga4_client.is_configured():
        body = """
        <div class="dash-note">GA4 not connected — optional. Connect it
          under Settings &rarr; Connections for website numbers here.</div>"""
        return _dash_card("/insights", "Website traffic", body)

    row = db.get_latest_ga4_snapshot()
    snapshot = json.loads(row["snapshot_json"]) if row else None
    week = (snapshot or {}).get("windows", {}).get("7")
    if not week or "events" not in week:
        body = """
        <div class="dash-note">First snapshot arrives with the next
          daily check.</div>"""
        return _dash_card("/insights", "Website traffic", body)

    current = week["traffic"]["current"]["sessions"]
    prior = week["traffic"]["prior"]["sessions"]
    events = week["events"]["current"]
    top_event = max(events.items(), key=lambda kv: kv[1]) if events else None
    event_line = ""
    if top_event:
        event_line = (f'<div class="dash-sub">{escape(top_event[0])}: '
                      f"{top_event[1]:,} this week</div>")

    # Sessions history at glance size: the 7-day session count from each
    # of the recent daily snapshots (a rolling window per bar, labeled as
    # such — not a per-day count).
    history = db.list_ga4_snapshots(limit=8)
    mini_values, mini_labels = [], []
    for row in history:
        try:
            snap = json.loads(row["snapshot_json"])
            sessions = snap["windows"]["7"]["traffic"]["current"]["sessions"]
        except (ValueError, KeyError, TypeError):
            continue
        mini_values.append(sessions)
        mini_labels.append(f'{row["date"]}: {sessions:,} sessions (7-day)')
    mini = charts.mini_bars(mini_values, labels=mini_labels,
                            aria="7-day sessions across recent snapshots")

    body = f"""
    <div class="dash-value mixed">{current:,} sessions</div>
    <div class="dash-sub">this week {_delta_pill(current, prior)} vs last</div>
    {mini}
    {event_line}"""
    return _dash_card("/insights", "Website traffic", body)


def dash_subscriptions():
    active = business_registry.active_business()
    count = len(recurring.detect_subscriptions(
        business_id=active["id"] if active else None))
    if count:
        body = f"""
        <div class="dash-value">{count}</div>
        <div class="dash-sub">recurring charge(s) detected in approved
          history</div>"""
    else:
        body = """
        <div class="dash-note">None detected yet — takes a few months
          of approved history.</div>"""
    return _dash_card("/vendors", "Subscriptions", body)


def dash_shopify():
    if not shopify_client.is_configured():
        return ""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    orders = db.list_shopify_orders(today, (date.today() + timedelta(days=1)).isoformat())
    prior = db.list_shopify_orders(yesterday, today)
    net = sum(row["total_price"] - row["refund_total"] for row in orders)
    prior_net = sum(row["total_price"] - row["refund_total"] for row in prior)
    products = _shopify_products(orders)
    top = max(products.values(), key=lambda item: item["revenue"])["title"] if products else "No orders today"
    last_week = [sum(r["total_price"] - r["refund_total"] for r in db.list_shopify_orders((date.today() - timedelta(days=n)).isoformat(), (date.today() - timedelta(days=n-1)).isoformat())) for n in range(7, 0, -1)]
    body = f'<div class="dash-value in">{money(net)}</div><div class="dash-sub">{len(orders)} order(s) {_delta_pill(net, prior_net)} vs yesterday</div><div class="dash-sub">Top product: {escape(top[:30])}</div>{charts.mini_bars(last_week, aria="Shopify net revenue, last 7 days")}'
    return _dash_card("/shopify", "Shopify", body)


# First-month cleanup nudge: shown once, 30 days after the onboarding
# anchor (db.FIRST_SEEN_KEY), if any approved transactions are still in a
# Wave catch-all category. Dismissing it sets the flag for good — this
# is a one-time onboarding prompt, never a recurring nag.
UNCAT_NUDGE_DISMISSED_KEY = "uncat_nudge_dismissed"
NUDGE_AFTER_DAYS = 30


def render_uncat_nudge():
    if db.get_meta(UNCAT_NUDGE_DISMISSED_KEY) == "1":
        return ""
    first_seen = db.get_meta(db.FIRST_SEEN_KEY)
    if not first_seen:
        return ""
    try:
        anchor = date.fromisoformat(first_seen)
    except ValueError:
        return ""
    if date.today() < anchor + timedelta(days=NUDGE_AFTER_DAYS):
        return ""
    count = db.count_uncategorized_approved()
    if count <= 0:
        return ""
    plural = "s" if count != 1 else ""
    return f"""
    <div class="nudge">
      <div class="nudge-text">You have {count} uncategorized transaction{plural}
        from your first month — want to clean them up? Takes about 5 minutes.</div>
      <div class="nudge-actions">
        <a class="btn-link primary" href="/uncategorized">Review them</a>
        <form method="post" action="/uncat-nudge/dismiss" class="inline-form">
          <button class="deny" type="submit">Dismiss</button>
        </form>
      </div>
    </div>"""


def render_daily_insight_banner():
    """The assistant's daily observation, written by the daily check —
    dismissable, and dismissal sticks until a NEWER insight arrives
    (keyed to the insight's timestamp, not a boolean)."""
    memory = db.get_agent_memory(ollama_client.INSIGHT_KEY)
    if not memory or not memory["value"].strip():
        return ""
    stamp = memory["updated_at"]
    if db.get_setting("daily_insight_dismissed") == stamp:
        return ""
    return f"""
    <div class="banner ai-insight">
      <span class="ai-insight-kicker">Today's insight</span>
      {escape(memory["value"])}
      <form method="post" action="/insight/dismiss" class="inline-form ml-2">
        <input type="hidden" name="stamp" value="{escape(stamp)}">
        <button class="btn-link" type="submit"
                class="tag-lg">Dismiss</button>
      </form>
    </div>"""


@app.post("/insight/dismiss")
def insight_dismiss(stamp: str = Form("")):
    db.set_setting("daily_insight_dismissed", (stamp or "").strip()[:64])
    return RedirectResponse("/", status_code=303)


def _briefing_points():
    """The facts worth stating in sentences, most urgent first.

    Every line is read from something the app already stores. Nothing is
    generated and nothing is estimated — a briefing that guesses is
    worse than a table, because a table never claims to have understood
    anything. Anything needing a network call (bank balance, runway)
    stays out and stays in the async cards below, so opening the app
    never waits.

    Returns (urgent, html) pairs. Urgent lines colour their marker and
    are counted in the lead sentence.
    """
    points = []

    pending = db.list_items(status="pending")
    if pending:
        stale = sum(1 for item in pending
                    if (days_since(item["date"]) or 0) >= 7)
        line = (f'<strong>{len(pending)}</strong> '
                f'{_plural(len(pending), "transaction")} waiting in '
                f'<a href="/review?tab=transactions">Review</a>')
        if stale:
            line += (f", <strong>{stale}</strong> of them over a week old")
        points.append((bool(stale), line + "."))

    followups = db.list_quote_followups(status="pending_review")
    invoices = db.list_invoice_reminders(status="pending_review")
    if followups:
        points.append((False,
                       f'<strong>{len(followups)}</strong> drafted '
                       f'{_plural(len(followups), "follow-up")} ready to send '
                       f'in <a href="/review?tab=followups">Review</a>.'))
    if invoices:
        points.append((True,
                       f'<strong>{len(invoices)}</strong> overdue '
                       f'{_plural(len(invoices), "invoice")} waiting on a '
                       f'reminder in <a href="/review?tab=invoices">Review</a>.'))

    # Reconciliation, from a native-era stored snapshot rather than a live call.
    # The history comes back oldest-first, so the newest is the tail.
    native_since = db.get_setting(NATIVE_RECONCILE_SINCE_KEY)
    history = ([row for row in db.list_reconcile_history()
                if row["date"] >= native_since] if native_since else [])
    if history:
        latest = history[-1]
        if latest.get("flagged"):
            points.append((True,
                           f'The books and bank differ by '
                           f'<strong>{money(abs(latest["residual"]))}</strong> '
                           f'that pending items do not explain '
                           f'(<a href="/reconcile">Reconcile</a>).'))

    # Ledger exceptions — records deliberately not posted.
    try:
        skips = db.list_ledger_source_skips()
    except Exception:
        skips = []
    if skips:
        points.append((True,
                       f'<strong>{len(skips)}</strong> source '
                       f'{_plural(len(skips), "record")} could not be posted '
                       f'to the <a href="/ledger?tab=sync">ledger</a>.'))

    uncategorized = db.count_uncategorized_approved()
    if uncategorized:
        points.append((False,
                       f'<strong>{uncategorized}</strong> approved '
                       f'{_plural(uncategorized, "transaction")} sat in a '
                       f'catch-all category '
                       f'(<a href="/uncategorized">tidy them up</a>).'))

    # This month's shape, if there is any.
    start, end = default_range()
    approved = db.list_items_in_range("approved", start, end)
    if approved:
        totals = summarize(approved)
        points.append((False,
                       f'This month: <strong>{money(totals["total_in"])}</strong> '
                       f'in, <strong>{money(totals["total_out"])}</strong> out, '
                       # _ledger_money, not money(), so a negative reads
                       # -$40.00 rather than $-40.00. The app-wide money()
                       # still does the latter; see the deferred list.
                       f'net <strong>{_ledger_money(totals["net"])}</strong> '
                       f'(<a href="/summary">Summary</a>).'))

    # The assistant's daily note, folded in rather than stacked above as
    # its own banner.
    memory = db.get_agent_memory(ollama_client.INSIGHT_KEY)
    if memory and memory["value"].strip():
        points.append((False, escape(memory["value"].strip())))

    return points


def _briefing():
    """The Home lead: one sentence, then the facts behind it."""
    points = _briefing_points()
    urgent = sum(1 for is_urgent, _ in points if is_urgent)

    if not points:
        lead = ("Nothing needs you. <span class=\"quiet\">The books are "
                "clear.</span>")
    elif urgent:
        lead = (f"{urgent} {_plural(urgent, 'thing')} "
                f"{_plural(urgent, 'needs', 'need')} you.")
    else:
        lead = ("Nothing urgent. <span class=\"quiet\">Here is where things "
                "stand.</span>")

    items = "".join(
        f'<li{" class=\"urgent\"" if is_urgent else ""}>'
        f'<span class="marker" aria-hidden="true">&#9679;</span>'
        f"<span>{html}</span></li>"
        for is_urgent, html in points)
    body = f'<ul class="briefing-points">{items}</ul>' if points else ""
    return f'<div class="briefing"><p class="briefing-lead">{lead}</p>{body}</div>'


def _today_exceptions():
    """Bookkeeping-blocking exceptions only.

    Deliberately narrower than the briefing's fact list: traffic dips and
    marketing anomalies are things to know, not work to clear, and Today
    is about work. Each entry is (label, href).
    """
    found = []
    native_since = db.get_setting(NATIVE_RECONCILE_SINCE_KEY)
    history = ([row for row in db.list_reconcile_history()
                if row["date"] >= native_since] if native_since else [])
    if history and history[-1].get("flagged"):
        found.append(("bank and books disagree", "/reconcile"))
    try:
        skips = db.list_ledger_source_skips()
    except Exception:
        skips = []
    if skips:
        found.append((f"{len(skips)} record{'' if len(skips) == 1 else 's'} "
                      "could not post", "/ledger?tab=sync"))
    invoices = db.list_invoice_reminders(status="pending_review")
    if invoices:
        found.append((f"{len(invoices)} overdue invoice"
                      f"{'' if len(invoices) == 1 else 's'}",
                      "/review?tab=invoices"))
    uncategorized = db.count_uncategorized_approved()
    if uncategorized:
        found.append((f"{uncategorized} uncategorized", "/uncategorized"))
    return found


def _today_region():
    """The one dominant region on Home.

    Three concerns side by side — work waiting, cash health, exceptions —
    each with exactly one call to action. Home previously gave nine cards
    equal weight, which meant none of them was the next action.

    The cash card fills in after paint (it needs the bank), so it renders
    a loading state here and is populated by the same /dashboard/cash
    endpoint the old card used. Nothing about that contract changes.
    """
    pending = db.list_items(status="pending")
    followups = db.list_quote_followups(status="pending_review")
    review_count = len(pending) + len(followups)
    exceptions = _today_exceptions()

    # Nothing waiting and nothing wrong: say so and stop asking for
    # attention. The audit called out the missing "you're done" close.
    if not review_count and not exceptions:
        return f"""
    <section class="today" aria-label="Today">
      <div class="today-clear">
        <span class="mark" aria-hidden="true">&#10003;</span>
        <div>
          <div class="headline">You're all caught up.</div>
          <div class="detail">Nothing to review and nothing needs fixing.
            New transactions appear here after the next check.</div>
        </div>
      </div>
    </section>"""

    if review_count:
        noun = "item" if review_count == 1 else "items"
        review_card = f"""
      <div class="today-card is-primary">
        <span class="kicker">To review</span>
        <span class="figure">{review_count} {noun}</span>
        <span class="detail">Nothing posts to your accounting until you
          approve it.</span>
        <div class="cta"><a class="btn-link primary"
          href="/review?tab=transactions">Start reviewing</a></div>
      </div>"""
    else:
        review_card = """
      <div class="today-card">
        <span class="kicker">To review</span>
        <span class="figure calm">Nothing waiting</span>
        <span class="detail">The queue is clear.</span>
        <div class="cta"><a class="btn-link"
          href="/review?tab=transactions">Open Review</a></div>
      </div>"""

    # Live values arrive from /dashboard/cash; the ids match what the
    # Home script already fills, plus a retry the old card never had.
    cash_card = """
      <div class="today-card">
        <span class="kicker">Cash</span>
        <span class="figure" id="today-cash-value">&hellip;</span>
        <span class="detail" id="today-cash-sub">Checking your bank&hellip;</span>
        <span class="freshness" id="today-cash-fresh"></span>
        <div class="cta" id="today-cash-cta"></div>
      </div>"""

    if exceptions:
        items = "".join(
            f'<li><a href="{escape(href, quote=True)}">{escape(label)}</a></li>'
            for label, href in exceptions[:3])
        more = (f'<span class="detail">and {len(exceptions) - 3} more</span>'
                if len(exceptions) > 3 else "")
        noun = "thing" if len(exceptions) == 1 else "things"
        exceptions_card = f"""
      <div class="today-card">
        <span class="kicker">Needs attention</span>
        <span class="figure">{len(exceptions)} {noun}</span>
        <ul class="briefing-points">{items}</ul>{more}
      </div>"""
    else:
        exceptions_card = """
      <div class="today-card">
        <span class="kicker">Needs attention</span>
        <span class="figure calm">Nothing</span>
        <span class="detail">No mismatches, failed postings, or overdue
          invoices.</span>
      </div>"""

    return f"""
    <section class="today" aria-label="Today">
      <div class="today-grid">{review_card}{cash_card}{exceptions_card}</div>
    </section>"""


_SIGNAL_NUMBER_RE = re.compile(
    r"(?<![\w])(?:-\$|\$)?\d[\d,]*(?:\.\d+)?%?"
)


def _signal_sentence_html(sentence):
    """Escape a signal sentence while keeping its numbers in data type."""
    parts = []
    cursor = 0
    for match in _SIGNAL_NUMBER_RE.finditer(sentence):
        parts.append(escape(sentence[cursor:match.start()]))
        parts.append(f'<span class="mono">{escape(match.group(0))}</span>')
        cursor = match.end()
    parts.append(escape(sentence[cursor:]))
    return "".join(parts)


def _source_signal_row(signal):
    severity = signal["severity"]
    label = {
        "urgent": "Urgent",
        "notable": "Worth knowing",
        "informational": "For context",
    }[severity]
    return f"""
      <li>
        <a class="source-signal {severity}"
           href="{escape(signal['deep_link'], quote=True)}"
           target="_blank" rel="noopener noreferrer">
          <span class="source-signal-tier">{label}</span>
          <span class="source-signal-copy">{_signal_sentence_html(signal['plain_language_sentence'])}</span>
          <span class="source-signal-open">Open source &nearr;</span>
        </a>
      </li>"""


def _connection_briefing():
    """Cross-integration signals, separate from bookkeeping exceptions."""
    signals = sorted(
        briefing_signals.get_home_briefing_signals(),
        key=lambda signal: briefing_signals.SEVERITY_ORDER[signal["severity"]],
    )
    connected = []
    if shopify_client.is_configured():
        connected.append("Shopify")
    if plaid_client.is_configured():
        connected.append("Plaid")
    scope = " + ".join(connected) if connected else "No sources connected"

    visible = [signal for signal in signals
               if signal["severity"] != "informational"]
    informational = [signal for signal in signals
                     if signal["severity"] == "informational"]

    if visible:
        visible_html = ('<ul class="source-signal-list">'
                        + "".join(_source_signal_row(signal)
                                  for signal in visible)
                        + "</ul>")
    else:
        visible_html = ""

    if informational:
        count = len(informational)
        noun = "update" if count == 1 else "updates"
        informational_html = f"""
      <details class="source-signal-context">
        <summary><span><span class="mono">{count}</span> background {noun}</span>
          <span class="source-signal-toggle" aria-hidden="true"></span></summary>
        <ul class="source-signal-list">{
            ''.join(_source_signal_row(signal) for signal in informational)
        }</ul>
      </details>"""
    else:
        informational_html = ""

    if not signals:
        if connected:
            empty = "Nothing new from your connected sources today."
            detail = "Shopify and Plaid are quiet."
            body = f"""
      <div class="source-briefing-empty">
        <span class="source-briefing-mark" aria-hidden="true">&#10003;</span>
        <div><strong>{empty}</strong><span>{detail}</span></div>
      </div>"""
        else:
            empty = "Connect Shopify or Plaid to see updates here."
            detail = "Connection data stays on this device."
            body = f"""
      <div class="source-briefing-empty">
        <div><strong>{empty}</strong><span>{detail}</span>
          <a class="btn-link" href="/settings?tab=connections">Open Connections</a>
        </div>
      </div>"""
    else:
        body = visible_html + informational_html

    return f"""
    <section class="source-briefing" aria-labelledby="source-briefing-title">
      <div class="source-briefing-head">
        <div>
          <span class="source-briefing-kicker">Across your dashboards</span>
          <h2 id="source-briefing-title">From your connections</h2>
        </div>
        <span class="source-briefing-scope">{escape(scope)}</span>
      </div>
      {body}
    </section>"""


def _insight_value_text(metric):
    """Human-readable current value, with the contract's unit preserved."""
    value = metric["value"]
    if value is None:
        return "\u2014"
    unit = metric["unit"]
    if unit == "currency_cents":
        return _ledger_money(Decimal(str(value)) / Decimal("100"))

    number = Decimal(str(value))
    text = f"{number:,.2f}".rstrip("0").rstrip(".")
    if unit == "percent":
        return f"{text}%"
    if unit == "days":
        return f"{text} {_plural(1 if number == 1 else 2, 'day')}"
    return text


def _insight_delta_html(metric):
    """Neutral comparison copy: direction is not inherently good or bad."""
    delta = metric["delta"]
    if delta is None:
        return '<span class="insight-metric-delta">No prior comparison</span>'
    if delta == 0:
        return '<span class="insight-metric-delta">No change from previous period</span>'

    direction = "up" if delta > 0 else "down"
    marker = "&#8593;" if delta > 0 else "&#8595;"
    magnitude = dict(metric)
    magnitude["value"] = abs(delta)
    value = _insight_value_text(magnitude)
    if metric["unit"] == "percent":
        value = value.removesuffix("%") + " pts"
    return f"""
        <span class="insight-metric-delta {direction}">
          <span aria-hidden="true">{marker}</span>
          <span class="mono">{escape(value)}</span> from previous period
        </span>"""


def _insight_metric_link(metric):
    source = metric["source"].title()
    value = _insight_value_text(metric)
    available = metric["value"] is not None
    comparison = (_insight_delta_html(metric) if available else
                  '<span class="insight-metric-delta">Not enough stored data yet</span>')
    aria_value = value if available else "not enough stored data yet"
    aria_label = f"{metric['label']}: {aria_value}. Open source in a new window."
    return f"""
      <a class="insight-metric" href="{escape(metric['deep_link'], quote=True)}"
         target="_blank" rel="noopener noreferrer"
         aria-label="{escape(aria_label, quote=True)}">
        <span class="insight-metric-source">{escape(source)}</span>
        <span class="insight-metric-label">{escape(metric["label"])}</span>
        <span class="insight-metric-value mono">{escape(value)}</span>
        {comparison}
        <span class="insight-metric-open">Open source &nearr;</span>
      </a>"""


def _home_insights_widget():
    """The configured subset of the shared registry, with no new reads."""
    try:
        pinned_ids = insight_widget.get_pinned_metric_ids()
        if not pinned_ids:
            return ""
        available = {metric["id"]: metric
                     for metric in insight_metrics.get_insight_metrics()}
    except Exception:
        body = """
        <div class="insight-metrics-empty">
          <strong>Insights could not load from stored data.</strong>
          <span>Reload Home to try again.</span>
          <a href="/">Reload Home</a>
        </div>"""
    else:
        metrics = [available[metric_id] for metric_id in pinned_ids
                   if metric_id in available]
        if metrics:
            body = ('<div class="insight-metric-grid">'
                    + "".join(_insight_metric_link(metric)
                              for metric in metrics)
                    + "</div>")
        else:
            body = """
        <div class="insight-metrics-empty">
          <strong>Your pinned metrics are not ready yet.</strong>
          <span>They will appear after their connected sources have stored data.</span>
          <a href="/settings?tab=connections">Check Connections</a>
        </div>"""

    return f"""
    <section class="home-insights" aria-labelledby="home-insights-title">
      <div class="insight-metrics-head">
        <div>
          <h2 id="home-insights-title">Insights</h2>
          <p>Current figures from the source data stored on this device.</p>
        </div>
        <a class="insight-metrics-action"
           href="/settings#home-insights-widget">Customize</a>
      </div>
      {body}
    </section>"""


AGING_LABELS = (
    ("current", "Not due yet"),
    ("1-30", "1 to 30 days late"),
    ("31-60", "31 to 60 days late"),
    ("61-90", "61 to 90 days late"),
    ("90+", "Over 90 days late"),
)


def _home_owed_panel():
    """Money customers owe you, by how late it is.

    Reads the same per-invoice balances (integer cents) and aging buckets
    as the A/R aging report, so the two always agree. Left out until at
    least one invoice exists, because there is nothing to age before then.
    """
    today = date.today().isoformat()
    views = [_invoice_view(invoice, today) for invoice in db.list_invoices()]
    if not views:
        return ""
    totals = {bucket: 0 for bucket, _ in AGING_LABELS}
    for view in views:
        if view["status"] in ("sent", "partial", "overdue"):
            totals[view["bucket"]] += view["balance"]
    cell = lambda cents: ui.money(Decimal(cents) / 100)
    rows = "".join(
        f'<tr><td>{label}</td><td class="num amount">{cell(totals[bucket])}</td></tr>'
        for bucket, label in AGING_LABELS)
    grand = sum(totals.values())
    return f"""
    <div class="section">
      <div class="section-head"><h2>Owed to you</h2>
        <a href="/invoices?tab=aging">See each customer</a></div>
      <table class="table-hover">
        <thead><tr><th>How late</th><th class="num">Amount</th></tr></thead>
        <tbody>{rows}</tbody>
        <tfoot><tr><th>Total owed</th>
          <th class="num amount">{cell(grand)}</th></tr></tfoot>
      </table>
    </div>"""


def render_home_page(message=None, error=None):
    # The cards backed by live bank calls fill in after paint via the JSON
    # endpoints below, so
    # opening the app never waits on the network.
    # Cash now lives in the Today region, filled by the same
    # /dashboard/cash endpoint. Only the bank-vs-books line stays a card,
    # inside its own disclosure — and it is labelled in plain language
    # rather than "Reconcile", which assumes the vocabulary.
    reconcile_card = _dash_card("/reconcile", "Bank vs books", """
      <div class="dash-note" id="dash-reconcile-line">Comparing your
        records with your bank&hellip;</div>""")

    # Actionable / anomaly cards lead (needs review, then anything
    # actually flagged); ambient status (this month, cash, reconcile,
    # traffic, subscriptions) follows; the broad health-score summary
    # closes the page rather than opening it, so the first thing a
    # user sees is what to do, not a grade.
    # The daily insight and the uncategorized nudge used to be two more
    # banners stacked above everything; both are facts about the data, so
    # they belong in the briefing beside the other facts. Only the update
    # banner stays a banner, because it is about the app itself.
    # Today leads. The nine equal-weight cards that used to sit here are
    # grouped into two disclosures and the health score becomes
    # on-demand, so the page opens with one region rather than a wall —
    # the audit's "no single next action" and "crowded Home".
    content = f"""
    {render_update_banner()}
    {_briefing()}
    {_today_region()}
    {_connection_briefing()}
    {_home_insights_widget()}
    {ui.disclosure("This month",
                   f'<div class="dash-grid">{dash_month()}'
                   f'{dash_top_categories()}{dash_vendor_flag()}'
                   f'{dash_subscriptions()}</div>',
                   note="income, spending, vendors")}
    {_home_owed_panel()}
    {ui.disclosure("Store and traffic",
                   f'<div class="dash-grid">{dash_shopify()}'
                   f'{dash_traffic()}</div>',
                   note="Shopify and website")}
    {ui.disclosure("Bank vs books", reconcile_card,
                   note="does your bank match your records")}
    {ui.disclosure("Business health score",
                   '<div id="dash-health"><div class="dash-note">'
                   'Open to score your business.</div></div>',
                   note="on demand")}"""

    # Async fills. Two changes from before, both from the audit's
    # "async and failed states lack action-oriented recovery": every
    # fetch now stamps when it ran and offers a real Retry on failure
    # instead of an em-dash, and the regions announce politely so a
    # screen reader hears the value arrive rather than finding it later.
    extra_js = """
    function stamp(el) {
      if (!el) return;
      var now = new Date();
      el.textContent = 'Checked ' + now.toTimeString().slice(0, 5);
      el.classList.remove('stale');
    }
    function retryButton(container, fn, label) {
      if (!container) return;
      container.innerHTML = '';
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'check';
      b.textContent = label || 'Try again';
      b.addEventListener('click', function () { fn(); });
      container.appendChild(b);
    }
    async function fillCash() {
      const value = document.getElementById('today-cash-value');
      const sub = document.getElementById('today-cash-sub');
      const fresh = document.getElementById('today-cash-fresh');
      const cta = document.getElementById('today-cash-cta');
      if (!value) return;
      value.textContent = '\\u2026';
      sub.textContent = 'Checking your bank\\u2026';
      try {
        const data = await (await fetch('/dashboard/cash')).json();
        if (data.state === 'ok') {
          value.textContent = data.balance;
          value.classList.remove('calm');
          sub.textContent = data.alert ? data.alert + ' \\u2014 ' + data.sub
                                       : data.sub;
          if (data.alert) value.classList.add('dash-flag');
          if (cta) {
            cta.innerHTML = '<a class="btn-link" href="/reconcile">'
              + 'Check bank vs books</a>';
          }
          stamp(fresh);
        } else {
          value.textContent = '\\u2014';
          value.classList.add('calm');
          sub.textContent = data.note;
          if (fresh) { fresh.textContent = 'Not checked'; fresh.classList.add('stale'); }
          retryButton(cta, fillCash);
        }
      } catch (e) {
        value.textContent = '\\u2014';
        value.classList.add('calm');
        sub.textContent = "Couldn't reach your bank just now.";
        if (fresh) { fresh.textContent = 'Not checked'; fresh.classList.add('stale'); }
        retryButton(cta, fillCash);
      }
    }
    async function fillReconcile() {
      const line = document.getElementById('dash-reconcile-line');
      if (!line) return;
      try {
        const data = await (await fetch('/dashboard/reconcile')).json();
        line.textContent = data.line;
        line.className = data.state === 'ok' ? 'dash-ok'
          : data.state === 'flagged' ? 'dash-flag' : 'dash-note';
      } catch (e) {
        line.textContent = "Couldn't check just now.";
        line.className = 'dash-note';
        var slot = document.createElement('div');
        slot.className = 'mt-2';
        line.parentNode.appendChild(slot);
        retryButton(slot, function () { slot.remove(); fillReconcile(); });
      }
    }
    // Health is on demand now: it was the slowest fill on the page and
    // the least actionable, and auto-running it made Home read as
    // permanently mid-load.
    var healthLoaded = false;
    async function fillHealth() {
      const el = document.getElementById('dash-health');
      if (!el || healthLoaded) return;
      healthLoaded = true;
      el.innerHTML = '<div class="dash-note">Scoring\\u2026</div>';
      try {
        const data = await (await fetch('/dashboard/health')).json();
        el.innerHTML = data.html;
      } catch (e) {
        healthLoaded = false;
        el.innerHTML = '<div class="dash-note">Couldn\\'t score just now.</div>';
        retryButton(el, fillHealth);
      }
    }
    document.querySelectorAll('details.disclosure').forEach(function (d) {
      d.addEventListener('toggle', function () {
        if (!d.open) return;
        if (d.querySelector('#dash-health')) fillHealth();
        if (d.querySelector('#dash-reconcile-line')) fillReconcile();
      });
    });
    fillCash();
    """

    return render_shell("Home", "", content, message, error,
                        extra_js=extra_js, wide=True)


def render_uncategorized_page(items, message=None, error=None):
    intro = """
    <div class="sub">These were approved into a catch-all category.
      Recategorize them to tidy your books — this list is here so you know
      exactly which ones to fix.</div>"""
    if not items:
        body = """
        <div class="empty">
          <p><strong>Nothing uncategorized.</strong></p>
          <p>Every approved transaction has a real category. Nicely done.</p>
        </div>"""
    else:
        rows = ""
        for item in items:
            note = escape(item.get("notes") or "")
            note_cell = f'<div class="meta">{note}</div>' if note else ""
            rows += f"""
            <tr>
              <td>{escape(item["date"])}</td>
              <td class="vendor-name">{escape(item["vendor"])}{note_cell}</td>
              <td>{escape(item["suggested_category"])}</td>
              <td class="num">{escape(signed_money(item))}</td>
            </tr>"""
        body = f"""
        <table>
          <thead><tr><th data-sort="date">Date</th><th data-sort="text">Vendor</th><th data-sort="text">Category</th>
            <th class="num" data-sort="num">Amount</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
    content = f"{intro}{body}"
    return render_shell("Home", "", content, message, error)


@app.get("/", response_class=HTMLResponse)
def home(msg: str = None, err: str = None):
    db.init_db()
    update_check.check_soon_if_due()  # background thread; never blocks
    return render_home_page(message=msg, error=err)


@app.get("/uncategorized", response_class=HTMLResponse)
def uncategorized_page(msg: str = None, err: str = None):
    db.init_db()
    items = db.list_uncategorized_approved()
    return render_uncategorized_page(items, message=msg, error=err)


@app.post("/uncat-nudge/dismiss")
def uncat_nudge_dismiss():
    db.init_db()
    db.set_meta(UNCAT_NUDGE_DISMISSED_KEY, "1")
    return RedirectResponse(
        f"/?msg={quote('Cleanup reminder dismissed.')}", status_code=303,
    )


@app.get("/dashboard/cash")
def dashboard_cash():
    """Cash position for the home card. Live bank call — fetched after
    page paint. Reuses cash_alert's config/balance/burn logic."""
    db.init_db()
    config = cash_alert.get_config()
    if not config["cash_account_ids"]:
        return {"state": "unconfigured",
                "note": "Pick your business-cash accounts in Settings to "
                        "see balance and runway here."}
    try:
        balance = cash_alert.current_cash_balance(config["cash_account_ids"])
    except Exception as exc:
        return {"state": "error", "note": str(exc)}

    burn = cash_alert.average_daily_burn()
    if burn > 0:
        runway = balance / burn
        sub = f"runway about {runway:.0f} days at current spend"
    else:
        runway = None
        sub = "no spend in the last 30 days"

    alert = None
    if (config["balance_threshold"] is not None
            and balance < config["balance_threshold"]):
        alert = "below your balance alert level"
    elif (config["runway_days"] is not None and runway is not None
            and runway < config["runway_days"]):
        alert = "below your runway alert level"

    # Runway gauge, filled against the configured runway alert level.
    # Only when both a runway and a threshold exist — otherwise there's
    # nothing meaningful to fill against, and the card just shows text.
    gauge_svg = ""
    if runway is not None and config["runway_days"] is not None:
        gauge_svg = charts.gauge(runway, config["runway_days"], unit="days",
                                 aria="Runway against your alert threshold")

    # Pending expense approvals have already left the bank (the balance
    # above already reflects them) but haven't reached the ledger yet —
    # balance-minus-pending answers "what can I safely commit to spend
    # right now" without double-counting money that's already spoken
    # for. Only shown when it would actually differ from the balance.
    pending_out = sum(i["amount"] for i in db.list_items(status="pending")
                      if i["direction"] == "expense")
    safe_to_spend = money(max(balance - pending_out, 0)) if pending_out > 0 else None

    # Cumulative net cash flow, last 8 weeks — a directional trend line
    # built from the same approved-transaction net series the Summary
    # sparkline plots. Not literal bank-balance history (Klearing doesn't
    # store daily balance snapshots), but enough to see whether cash has
    # been trending up or down recently.
    spark_values, _spark_labels = weekly_net_series(8)
    trend_svg = ""
    if len(spark_values) >= 2:
        running = Decimal(0)
        cumulative = []
        for v in spark_values:
            running += v
            cumulative.append(round(running, 2))
        trend_svg = charts.sparkline(
            cumulative, zero_baseline=True,
            aria="Cumulative net cash flow, last 8 weeks")

    return {"state": "ok", "balance": money(balance), "sub": sub,
            "alert": alert, "gauge_svg": gauge_svg,
            "safe_to_spend": safe_to_spend, "trend_svg": trend_svg}


@app.get("/dashboard/reconcile")
def dashboard_reconcile():
    """One-line reconcile status for the home card. Native books + bank
    calls — fetched after page paint. Reuses gather_reconciliation."""
    db.init_db()
    try:
        data = gather_reconciliation()
    except RuntimeError as exc:
        return {"state": "blocked", "line": str(exc)}
    if data["flagged"]:
        return {"state": "flagged",
                "line": (f"Unexplained difference of "
                         f"{money(abs(data['residual']))} after the "
                         f"pending queue — worth a look.")}
    return {"state": "ok",
            "line": "In sync — the books and bank agree once the pending "
                    "queue is accounted for."}


# --------------------------------------------------------- health score


def _income_in_range(start_date, end_date, business_id):
    """Approved income total in [start, end] for the active business."""
    return sum(
        item["amount"]
        for item in db.list_items_in_range("approved", start_date, end_date,
                                           business_id=business_id)
        if item["direction"] == "income"
    )


def gather_health():
    """Compute the composite business-health score. Does the live bank
    and bank calls (so it belongs on the async /dashboard/health
    endpoint, not the page render). All scoring is deterministic in
    health.py; this only gathers the raw inputs."""
    db.init_db()
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    factors = []

    # Cash & runway (live bank call).
    config = cash_alert.get_config()
    cash_configured = bool(config["cash_account_ids"]) and (
        config["balance_threshold"] is not None
        or config["runway_days"] is not None)
    if not cash_configured:
        factors.append(health.score_cash(False, False, None, None, None, None))
    else:
        try:
            balance = cash_alert.current_cash_balance(config["cash_account_ids"])
            burn = cash_alert.average_daily_burn()
            runway = balance / burn if burn > 0 else None
            factors.append(health.score_cash(
                True, True, runway, config["runway_days"], balance,
                config["balance_threshold"]))
        except Exception:
            factors.append(health.score_cash(
                True, False, None, config["runway_days"], None,
                config["balance_threshold"]))

    # Reconcile (native books + live bank call).
    try:
        recon = gather_reconciliation()
        factors.append(health.score_reconcile(
            True, recon["flagged"], recon["residual"]))
    except Exception:
        factors.append(health.score_reconcile(False, False, 0))

    # Revenue trend: income last 30d vs the prior 30d (active business).
    today = date.today()
    recent = _income_in_range((today - timedelta(days=29)).isoformat(),
                              today.isoformat(), active_id)
    prior = _income_in_range((today - timedelta(days=59)).isoformat(),
                             (today - timedelta(days=30)).isoformat(), active_id)
    factors.append(health.score_revenue(recent, prior))

    # Vendor stability: any active spend-jump flags.
    rows, _threshold = vendor_stats()
    flagged = sum(1 for row in rows if row["jump_pct"] is not None)
    factors.append(health.score_vendors(bool(rows), flagged))

    # Website traffic: this-week vs last-week sessions from the snapshot.
    ga4_on = ga4_client.is_configured()
    cur_sessions = prior_sessions = 0
    if ga4_on:
        row = db.get_latest_ga4_snapshot()
        if row:
            try:
                snap = json.loads(row["snapshot_json"])
                traffic = snap.get("windows", {}).get("7", {}).get("traffic", {})
                cur_sessions = traffic.get("current", {}).get("sessions", 0)
                prior_sessions = traffic.get("prior", {}).get("sessions", 0)
            except (ValueError, TypeError):
                pass
    factors.append(health.score_traffic(ga4_on, cur_sessions, prior_sessions))

    return health.combine(factors, health.get_weights())


def _health_pill(score):
    return "ok" if score >= 70 else ("warn" if score >= 45 else "bad")


def render_health_card_body(result):
    """The inner HTML of the dashboard health card, injected after paint."""
    score = result["score"]
    if score is None:
        return ('<div class="health-card"><div class="dash-note">Not enough '
                'data yet to score. Connect a bank or GA4 and approve '
                'some transactions, and your health score appears here.</div>'
                '</div>')
    grade = result["grade"]
    band = "good" if grade in ("A", "B") else ("warn" if grade in ("C", "D")
                                               else "bad")
    weakest = result["weakest"]
    if weakest and weakest["score"] < 100:
        # Name the factor here; its own row in the list below already
        # carries the full detail sentence — repeating it verbatim in
        # both places read as a copy-paste leftover, not a deliberate
        # summary-then-detail pattern.
        summary = (f'<div class="health-summary"><strong>Biggest risk right '
                   f'now:</strong> {escape(weakest["label"].lower())}.</div>')
    else:
        summary = ('<div class="health-summary"><strong>Everything looks '
                   'healthy.</strong> No factor is dragging your score '
                   'down.</div>')

    factors_html = ""
    for factor in result["factors"]:
        if factor["available"]:
            pill = _health_pill(factor["score"])
            factors_html += (
                f'<div class="health-factor"><span class="hf-label">'
                f'{escape(factor["label"])}</span>'
                f'<span class="tag {pill}">{escape(factor["status"])}</span>'
                f'<span class="hf-detail">{escape(factor["detail"])}</span></div>')
        else:
            factors_html += (
                f'<div class="health-factor off"><span class="hf-label">'
                f'{escape(factor["label"])}</span>'
                f'<span class="tag off">not counted</span>'
                f'<span class="hf-detail">{escape(factor["detail"])}</span></div>')

    included = [f["label"].lower() for f in result["available"]]
    note = (f'<div class="health-note">Composite of {len(result["available"])} '
            f'of {len(result["factors"])} factors '
            f'({escape(", ".join(included))}). Weights adjustable in '
            f'<a href="/settings">Settings</a>. Deterministic — plain '
            f'thresholds on your real numbers, no AI.</div>')

    return (f'<div class="health-card"><div class="health-grade {band}">'
            f'<div class="g">{grade}</div><div class="s">{score}/100</div></div>'
            f'<div class="health-body">{summary}'
            f'<div class="health-factors">{factors_html}</div>{note}</div></div>')


@app.get("/dashboard/health")
def dashboard_health():
    """Composite health score for the home card. Native books + live bank,
    fetched after page paint like the cash and reconcile cards."""
    try:
        result = gather_health()
    except Exception as exc:
        return {"html": f'<div class="health-card"><div class="dash-note">'
                        f"Couldn't score just now: {escape(str(exc))}</div></div>"}
    return {"html": render_health_card_body(result)}


# ---------------------------------------------------------------- Reconcile


# Differences smaller than this are noise (rounding, cents in flight),
# not discrepancies worth flagging.
RECONCILE_THRESHOLD = 1.00
NATIVE_RECONCILE_SINCE_KEY = "native_reconciliation_since"


def gather_reconciliation():
    """Everything the Reconcile page shows, or raises RuntimeError with
    a message naming which side failed."""
    active = business_registry.active_business()
    books_balance = float(accountant_export.cash_balance(
        date.today().isoformat()))

    config = cash_alert.get_config()
    if not config["cash_account_ids"]:
        raise RuntimeError(
            "Pick which bank account(s) count as business cash in "
            "Settings first — reconciliation compares the native books against "
            "exactly those accounts."
        )
    try:
        bank_accounts = cash_alert.matched_cash_accounts(
            config["cash_account_ids"])
        bank_balance = sum(a["balance"] for a in bank_accounts)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Couldn't read the bank balance: {exc}")

    pending = db.list_items(status="pending")
    # Largest first, oldest as tiebreak — same ordering dash_needs_review
    # uses on Home, so "what's driving this number" reads the same way
    # wherever it shows up.
    sort_key = lambda i: (-i["amount"], i["date"])
    pending_out_items = sorted(
        (i for i in pending if i["direction"] == "expense"), key=sort_key)
    pending_in_items = sorted(
        (i for i in pending if i["direction"] != "expense"), key=sort_key)
    pending_out = sum(i["amount"] for i in pending_out_items)
    pending_in = sum(i["amount"] for i in pending_in_items)

    difference = books_balance - bank_balance
    # Pending expenses already left the bank but aren't approved into the
    # native ledger yet, so they push books ABOVE bank; income the reverse.
    explained = pending_out - pending_in
    residual = difference - explained

    # Stripe context (optional): in-transit money hasn't reached the
    # bank yet, which is a classic source of an unexplained difference.
    # A Stripe API failure must never take down Reconcile — it degrades
    # to an "unreachable" note.
    stripe = None
    if stripe_client.is_configured():
        try:
            payouts = stripe_client.recent_payouts(days=60)
            stripe = {
                "in_transit": stripe_client.balance()["in_transit"],
                "last_payout": payouts[0] if payouts else None,
            }
        except Exception:
            stripe = {"error": True}

    return {
        "business_name": active["name"] if active else "your business",
        "books_balance": books_balance,
        "bank_balance": bank_balance,
        "bank_accounts": bank_accounts,
        "difference": difference,
        "pending_out": pending_out,
        "pending_in": pending_in,
        "pending_out_items": pending_out_items,
        "pending_in_items": pending_in_items,
        "pending_count": len(pending),
        "explained": explained,
        "residual": residual,
        "flagged": abs(residual) > RECONCILE_THRESHOLD,
        "stripe": stripe,
    }


def record_reconcile_snapshot(data=None):
    """Write today's reconciliation snapshot (one row per day, the
    freshest check wins) — called by the manual page view and by the
    daily check. Gathers live when no data is passed."""
    if data is None:
        data = gather_reconciliation()
    today = date.today().isoformat()
    if not db.get_setting(NATIVE_RECONCILE_SINCE_KEY):
        db.set_setting(NATIVE_RECONCILE_SINCE_KEY, today)
    db.insert_reconcile_snapshot(
        today, data["books_balance"],
        data["bank_balance"], data["residual"], data["flagged"])
    return data


def _reconcile_history_section(signed):
    """30-day residual sparkline + a last-flagged context line, as a
    bare body for the disclosure that wraps it. Omitted
    entirely until the first snapshot exists (nothing to compute yet,
    not a checked-and-clear state)."""
    native_since = db.get_setting(NATIVE_RECONCILE_SINCE_KEY)
    if not native_since:
        return ""
    history = [row for row in db.list_reconcile_history(days=30)
               if row["date"] >= native_since]
    if not history:
        return ""
    values = [row["residual"] for row in history]
    labels = [f'{row["date"]}: {signed(row["residual"])}'
              for row in history]
    spark = charts.sparkline(
        values, labels=labels, last_label=signed(values[-1]),
        zero_baseline=True,
        aria="Unexplained residual over the last 30 days")
    flagged_dates = [row["date"] for row in history if row["flagged"]]
    if flagged_dates:
        days_ago = (date.today()
                    - date.fromisoformat(flagged_dates[-1])).days
        when = ("today" if days_ago == 0
                else "1 day ago" if days_ago == 1
                else f"{days_ago} days ago")
        flag_note = f"Last flagged: {when}."
    else:
        flag_note = "No flags in the last 30 days."
    return f"""
      {spark}
      <div class="none-note">{flag_note}</div>
      <div class="snapshot-meta">One snapshot per day — written by the
        daily check and whenever this page runs a live check.</div>"""


def render_reconcile_page(data=None, problem=None, message=None, error=None):
    adapter = banks.active_adapter()
    bank_name = adapter.display_name if adapter else "your bank"

    if problem:
        content = f"""
        <div class="sub">Native-ledger cash next to {escape(bank_name)}'s
          live balance, checked on demand.</div>
        <div class="empty">
          <p><strong>Can't reconcile yet.</strong></p>
          <p>{escape(problem)}</p>
        </div>"""
        return render_shell("Bank vs books", "", content, message, error)

    # Explicit sign on every figure, matching signed_money()'s convention
    # everywhere else in the app (Home's dash cards, Pending's amounts) —
    # a bare "$12,313.35" next to "-$1,117.20" read as two different
    # money-formatting conventions on the same page.
    signed = lambda v: ("+" if v >= 0 else "-") + money(abs(v))

    if data["flagged"]:
        verdict = f"""
        <div class="flag flag-warn">Unexplained difference of
          {signed(data["residual"])} after accounting for the pending
          queue — worth a look. Likely causes are listed below.</div>"""
        causes = f"""
        <div class="section">
          <div class="section-head"><h2>Likely causes</h2></div>
          <ol class="setup-list">
            <li><strong>Transactions not fetched yet</strong> — run
              "Check for new transactions" on
              <a href="/review?tab=transactions">Pending</a>; anything that arrived at
              the bank since the last check isn't in the queue and
              can't be explained by it.</li>
            <li><strong>Timing</strong> — a card authorization the bank
              already shows but hasn't settled ({APP_NAME} imports only
              settled transactions), or a transfer in flight overnight.</li>
            <li><strong>Entries missing from the native ledger</strong> —
              imported or historical activity may not have passed through
              {APP_NAME}, so the queue cannot account for it.</li>
            <li><strong>Books that started mid-life</strong> — if the native
              ledger opened at zero while the
              bank already held money, the gap is your opening balance,
              not an error.</li>
          </ol>
        </div>"""
    else:
        verdict = """
        <div class="flag flag-info">Reconciled — the two balances agree
          once the pending queue is accounted for.</div>"""
        causes = ""

    explain_rows = f"""
        <tr><td>Native ledger &middot; cash accounts
            ({escape(data["business_name"])})</td>
          <td class="num">{signed(data["books_balance"])}</td></tr>
        <tr><td>{escape(bank_name)} &middot; selected business-cash
            accounts (live)</td>
          <td class="num">{signed(data["bank_balance"])}</td></tr>
        <tr><td><strong>Difference</strong></td>
          <td class="num"><strong>{signed(data["difference"])}</strong></td></tr>
        <tr><td>Pending approvals, money out (left the bank, not approved
            into the ledger yet) &middot; {len(data["pending_out_items"])} item(s)</td>
          <td class="num">{signed(data["pending_out"])}</td></tr>
        <tr><td>Pending approvals, money in &middot;
            {len(data["pending_in_items"])} item(s)</td>
          <td class="num">{signed(data["pending_in"])}</td></tr>
        <tr><td><strong>Unexplained after pending queue</strong></td>
          <td class="num"><strong>{signed(data["residual"])}</strong></td></tr>"""

    # T2a: which specific pending items make up the two totals above —
    # the same "show your work" idea accounting tools give you via
    # per-transaction matching, without asking for any manual matching
    # (Klearing already computed which items are pending; this just
    # shows them instead of only the aggregate).
    def _pending_item_rows(items):
        if not items:
            return '<div class="none-note">None.</div>'
        rows = "".join(
            f'<tr><td class="vendor-name">{escape(item["vendor"])}</td>'
            f'<td class="mono">{escape(item["date"])}</td>'
            f'<td class="num">{money(item["amount"])}</td></tr>'
            for item in items)
        return f"""
        <table>
          <thead><tr><th data-sort="text">Vendor</th><th data-sort="date">Date</th><th class="num" data-sort="num">Amount</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    pending_detail_body = ""
    if data["pending_count"]:
        pending_detail_body = f"""
      <h3>Money out ({len(data["pending_out_items"])})</h3>
      {_pending_item_rows(data["pending_out_items"])}
      <h3>Money in ({len(data["pending_in_items"])})</h3>
      {_pending_item_rows(data["pending_in_items"])}"""

    # Side-by-side bars make an imbalance obvious before the reader
    # parses the numbers; the table below keeps every figure.
    compare_chart = charts.horizontal_bar_chart([
        {"label": "Native ledger · cash",
         "value": data["books_balance"],
         "value_label": signed(data["books_balance"]),
         "neutral": True},
        {"label": f"{bank_name} (live)",
         "value": data["bank_balance"],
         "value_label": signed(data["bank_balance"])},
    ], aria="Native ledger cash versus bank balance", bar_h=30)

    # Per-account breakdown, only when the combined bank total is
    # actually a combination — a single selected account IS the total
    # above, and repeating it would just be noise. An overdrawn account
    # while the books don't reconcile is the one per-account signal we
    # can compute honestly, so that row gets the warn treatment.
    # A connected Stripe account adds an in-transit row: money Stripe
    # holds that hasn't reached the bank (shown for context — it is NOT
    # part of the combined bank total, so it carries no share figure).
    accounts = data.get("bank_accounts") or []
    stripe = data.get("stripe")
    stripe_note = ""
    stripe_rows = []
    if stripe is None:
        stripe_note = ('<div class="none-note">Connect Stripe on the '
                       '<a href="/settings?tab=connections">Connections page</a> to see '
                       "payouts and in-transit amounts here.</div>")
    elif stripe.get("error"):
        stripe_note = ('<div class="none-note">Stripe couldn\'t be reached '
                       "just now — payout and in-transit amounts are "
                       'unavailable this check. <a href="/settings?tab=connections">'
                       "Re-test connection</a>.</div>")
    else:
        last = stripe.get("last_payout")
        last_text = (f'Last payout {signed(last["amount"])} on '
                     f'{escape(last["date"])}. ' if last
                     else "No payouts in the last 60 days. ")
        stripe_note = (f'<div class="none-note">Stripe: {last_text}'
                       f'{signed(stripe["in_transit"])} in transit '
                       f"(not yet at the bank, not in the combined total "
                       f"above).</div>")
        stripe_rows = [{
            "label": "Stripe (in transit)",
            "value": stripe["in_transit"],
            "value_label": f'{signed(stripe["in_transit"])} · in transit',
            "neutral": True,
        }]
    account_breakdown_body = ""
    if len(accounts) >= 2 or stripe_rows:
        total = data["bank_balance"]
        rows = []
        for acct in sorted(accounts, key=lambda a: -a["balance"]):
            share = ""
            if total > 0 and acct["balance"] >= 0:
                share = f' · {acct["balance"] / total * 100:.0f}% of total'
            rows.append({
                "label": acct["name"],
                "value": acct["balance"],
                "value_label": f'{signed(acct["balance"])}{share}',
                "warn": data["flagged"] and acct["balance"] < 0,
            })
        rows += stripe_rows
        account_chart = charts.horizontal_bar_chart(
            rows, aria="Bank balance by account", bar_h=30)
        plural = "s" if len(accounts) != 1 else ""
        account_breakdown_body = f"""
      {account_chart}
      <div class="snapshot-meta">The {len(accounts)} selected
        business-cash account{plural} behind the combined
        {signed(data["bank_balance"])} above.</div>
      {stripe_note}"""
    else:
        account_breakdown_body = stripe_note or ""

    history_body = _reconcile_history_section(signed)

    # In-transit Stripe money is a classic explanation for a flagged
    # residual: it left customers, hasn't reached the bank, and the ledger may
    # already have recorded the sale.
    stripe_residual_note = ""
    if (data["flagged"] and stripe and not stripe.get("error")
            and stripe.get("in_transit", 0) > 0):
        extent = ("could explain all"
                  if stripe["in_transit"] >= abs(data["residual"])
                  - RECONCILE_THRESHOLD else "may explain part")
        stripe_residual_note = f"""
        <div class="flag flag-info">Stripe holds
          {signed(stripe["in_transit"])} in transit to your bank — that
          {extent} of the unexplained difference.</div>"""

    content = f"""
    <div class="sub">Native-ledger cash next to {escape(bank_name)}'s live
      balance, checked just now. Differences up to {money(RECONCILE_THRESHOLD)}
      are treated as noise; the pending queue
      ({data["pending_count"]} item(s)) is accounted for before anything
      is flagged.</div>
    {verdict}
    {stripe_residual_note}
    <div class="section">
      <div class="section-head"><h2>Books vs {escape(bank_name)}</h2></div>
      {compare_chart}
    </div>
    {causes}
    {ui.disclosure("How the two balances are built",
                   f'<table><tbody>{explain_rows}</tbody></table>',
                   note="the arithmetic")}
    {ui.disclosure(f'Pending queue detail ({data["pending_count"]})',
                   pending_detail_body,
                   note="what is already accounted for")
     if pending_detail_body else ""}
    {ui.disclosure("Per-account balances", account_breakdown_body,
                   note="where the money sits")
     if account_breakdown_body else ""}
    {ui.disclosure("Residual over the last 30 days", history_body,
                   note="trend") if history_body else ""}"""
    return render_shell("Bank vs books", "", content, message, error)


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(msg: str = None, err: str = None):
    db.init_db()
    try:
        data = gather_reconciliation()
    except RuntimeError as exc:
        return render_reconcile_page(problem=str(exc), message=msg, error=err)
    try:
        # A manual check is a check — it feeds the history too. A write
        # failure must never take down the page the user is looking at.
        record_reconcile_snapshot(data)
    except Exception:
        pass
    return render_reconcile_page(data=data, message=msg, error=err)


# ---------------------------------------------------------------- Vendors


VENDOR_JUMP_KEY = "vendor_jump_threshold"
DEFAULT_VENDOR_JUMP_PCT = 20.0


def vendor_jump_threshold():
    """User-adjustable month-over-month jump percentage (Settings tab).
    Falls back to 20 when unset or invalid."""
    raw = db.get_setting(VENDOR_JUMP_KEY)
    if raw and raw.strip():
        try:
            value = float(raw.strip())
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_VENDOR_JUMP_PCT


def vendor_stats():
    """
    Approved expense transactions grouped by vendor: spend this
    calendar month, last calendar month, lifetime, and count. Sorted
    by this month's spend (highest first), lifetime as tiebreak, so
    vendors with no spend this month (but history) sink to the bottom
    yet remain visible.
    """
    today = date.today()
    this_month = today.strftime("%Y-%m")
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    active = business_registry.active_business()
    active_id = active["id"] if active else None

    stats = {}
    for item in db.list_items(status="approved"):
        if item["direction"] != "expense":
            continue
        if (active_id is not None
                and item.get("business_id") not in (None, active_id)):
            continue  # another business's spend
        entry = stats.setdefault(
            item["vendor"],
            {"this_month": 0.0, "last_month": 0.0, "lifetime": 0.0, "count": 0},
        )
        entry["lifetime"] += item["amount"]
        entry["count"] += 1
        month = item["date"][:7]
        if month == this_month:
            entry["this_month"] += item["amount"]
        elif month == last_month:
            entry["last_month"] += item["amount"]

    threshold_pct = vendor_jump_threshold()
    rows = []
    for vendor, entry in stats.items():
        jump_pct = None
        # A jump needs a nonzero last month — a brand-new vendor this
        # month is just new, not a spike.
        if (entry["last_month"] > 0
                and entry["this_month"] >= entry["last_month"] * (1 + threshold_pct / 100)):
            jump_pct = (entry["this_month"] / entry["last_month"] - 1) * 100
        rows.append({"vendor": vendor, "jump_pct": jump_pct, **entry})

    rows.sort(key=lambda r: (-r["this_month"], -r["lifetime"]))
    return rows, threshold_pct


def render_subscriptions_section(subscriptions):
    if not subscriptions:
        # Calm "checked, none found" state — matches Home's
        # dash_subscriptions(), which shows this same wording for the
        # same underlying feature rather than omitting the section.
        return """
        <div class="section">
          <div class="section-head"><h2>Detected subscriptions</h2>
            <span class="tag">informational</span></div>
          <div class="none-note">None detected yet — takes a few months
            of approved history to spot a pattern.</div>
        </div>"""
    total = sum(sub.get("monthly_cost", sub["typical_amount"])
                for sub in subscriptions)
    rows = ""
    for sub in subscriptions:
        rows += f"""
        <tr>
          <td class="vendor-name">{escape(sub["vendor"])}</td>
          <td>{escape(sub["cadence"])}</td>
          <td class="num">~{money(sub["typical_amount"])}</td>
          <td class="num">{money(sub.get("monthly_cost", sub["typical_amount"]))}</td>
          <td class="num">{sub["count"]}</td>
          <td>{escape(sub.get("first_detected_date", "—"))}</td>
          <td>{escape(sub["last_date"])}</td>
          <td>{escape(sub["next_expected"])}</td>
        </tr>"""
    plural = "s" if len(subscriptions) != 1 else ""
    return f"""
    <div class="section">
      <div class="section-head"><h2>Detected subscriptions</h2>
        <span class="tag">informational</span></div>
      <div class="none-note pb-2">Vendors charging
        a similar amount on a regular rhythm, spotted in your approved
        history — about {money(total)}/month across
        {len(subscriptions)} subscription{plural}, based on typical
        charge amounts. Nothing is acted on automatically — this is a
        list to glance at when you wonder where the money goes.</div>
      <table>
        <thead><tr><th data-sort="text">Vendor</th><th>Cadence</th>
          <th class="num" data-sort="num">Typical</th><th class="num" data-sort="num">Monthly</th>
          <th class="num" data-sort="num">Charges</th><th>First detected</th>
          <th data-sort="date">Last seen</th><th data-sort="date">Next expected</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


VENDOR_DETAIL_LIMIT = 100


def vendor_expense_items(vendor):
    """Approved expense items for one vendor, active business, newest
    first — the transactions behind vendor_stats()'s lifetime total.
    Returns (items capped at VENDOR_DETAIL_LIMIT, true total count)."""
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    items = [i for i in db.list_items(status="approved")
            if i["direction"] == "expense" and i["vendor"] == vendor
            and (active_id is None or i.get("business_id") in (None, active_id))]
    items.sort(key=lambda i: i["date"], reverse=True)
    return items[:VENDOR_DETAIL_LIMIT], len(items)


def render_vendor_detail_page(vendor, items, total_count, message=None,
                              error=None):
    back_link = '<p class="hint"><a href="/vendors">All vendors</a></p>'
    if not items:
        content = f"""
        <div class="empty">
          <p><strong>No approved expenses found for {escape(vendor)}.</strong></p>
        </div>
        {back_link}"""
        return render_shell("Vendors", "", content, message, error)

    rows = "".join(
        f'<tr><td>{escape(item["date"])}</td>'
        f'<td>{escape(item["suggested_category"])}</td>'
        f'<td class="num">{money(item["amount"])}</td>'
        f'<td>'
        f'<form method="post" '
        f'action="/vendors/detail/annotate/{item["id"]}" '
        f'class="row-inline">'
        f'<input type="hidden" name="vendor" value="{escape(vendor)}">'
        f'<input type="text" name="notes" maxlength="500" '
        f'class="notes-input w-md" '
        f'value="{escape(item.get("notes") or "")}" placeholder="Note">'
        f'<select name="evidence_type">'
        f'{_evidence_options(item.get("evidence_type"))}</select>'
        f'<button class="btn-link" type="submit">Save</button>'
        f'</form></td></tr>'
        for item in items)
    truncated_note = ""
    if total_count > len(items):
        truncated_note = (f'<div class="snapshot-meta">Showing the most '
                          f'recent {len(items)} of {total_count} '
                          f'transactions.</div>')
    content = f"""
    <div class="section">
      <div class="section-head"><h2>{escape(vendor)}</h2>
        <span class="tag">{total_count} transaction(s)</span></div>
      <table>
        <thead><tr><th data-sort="date">Date</th><th data-sort="text">Category</th>
          <th class="num" data-sort="num">Amount</th><th>Note &amp; evidence</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {truncated_note}
    </div>
    {back_link}"""
    return render_shell("Vendors", "", content, message, error, wide=True)


CATEGORY_DETAIL_LIMIT = 100


def category_items(category, direction, status, start_date, end_date):
    """Items in one category for the Summary category drill-down, same
    date-range and business-scoping rules the Summary page itself uses
    (approved items belong to the active business; pending items aren't
    attributed yet). Returns (items capped at CATEGORY_DETAIL_LIMIT,
    true total count)."""
    active = business_registry.active_business()
    business_id = active["id"] if (active and status == "approved") else None
    matches = [i for i in db.list_items_in_range(status, start_date, end_date,
                                                 business_id=business_id)
              if i["direction"] == direction
              and i["suggested_category"] == category]
    matches.sort(key=lambda i: i["date"], reverse=True)
    return matches[:CATEGORY_DETAIL_LIMIT], len(matches)


def render_category_detail_page(category, direction, status, start_date,
                                end_date, items, total_count, message=None,
                                error=None):
    """Transactions behind one Summary category cell — same
    filter-then-list shape as Vendor Detail, filtered by category and
    period instead of by vendor."""
    back_link = (f'<p class="hint"><a href="/summary?start='
                f'{quote(start_date)}&end={quote(end_date)}">'
                f'Back to Summary</a></p>')
    if not items:
        content = f"""
        <div class="empty">
          <p><strong>No {escape(status)} {escape(direction)} transactions
            found for {escape(category)} in this range.</strong></p>
        </div>
        {back_link}"""
        return render_shell("Summary", "", content, message, error)

    rows = "".join(
        f'<tr><td>{escape(item["date"])}</td>'
        f'<td class="vendor-name">{escape(item["vendor"])}</td>'
        f'<td class="num">{money(item["amount"])}</td></tr>'
        for item in items)
    truncated_note = ""
    if total_count > len(items):
        truncated_note = (f'<div class="snapshot-meta">Showing the most '
                          f'recent {len(items)} of {total_count} '
                          f'transactions.</div>')
    content = f"""
    <div class="section">
      <div class="section-head"><h2>{escape(category)}</h2>
        <span class="tag">{total_count} transaction(s)</span></div>
      <table>
        <thead><tr><th data-sort="date">Date</th><th data-sort="text">Vendor</th>
          <th class="num" data-sort="num">Amount</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {truncated_note}
    </div>
    {back_link}"""
    return render_shell("Summary", "", content, message, error, wide=True)


def stripe_fee_row():
    """A synthetic vendor row for Stripe processing fees (this month +
    last month, live from Stripe, cached). Fees are an expense even
    though Stripe's payouts are income — mixing them would hide the
    real cost of taking cards. Returns None when Stripe isn't
    connected or can't be reached (the page shows a calm note
    instead)."""
    if not stripe_client.is_configured():
        return None
    today = date.today()
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    try:
        this_total, this_count = stripe_client.fees_in_range(
            first.isoformat(), today.isoformat())
        last_total, _count = stripe_client.fees_in_range(
            last_month_start.isoformat(), last_month_end.isoformat())
    except Exception:
        return None
    if this_total <= 0 and last_total <= 0:
        return None
    return {"vendor": "Stripe processing fees", "jump_pct": None,
            "this_month": this_total, "last_month": last_total,
            "lifetime": None, "count": this_count, "synthetic": True}


def render_vendors_page(rows, threshold_pct, subscriptions=None,
                        message=None, error=None):
    # Stripe's processing fees join the vendor list automatically — an
    # expense the bank feed never shows as its own line item.
    fee_row = stripe_fee_row()
    if fee_row:
        rows = rows + [fee_row]
        rows.sort(key=lambda r: (-r["this_month"], -(r["lifetime"] or 0)))
    stripe_vendor_note = ""
    if not stripe_client.is_configured():
        stripe_vendor_note = (
            '<div class="none-note">Connect Stripe on the '
            '<a href="/settings?tab=connections">Connections page</a> to track card '
            "processing fees as a vendor automatically.</div>")

    # Receipt emails per vendor (Gmail read-only, optional) — an
    # expense-audit aid: "do I have paperwork for this vendor's spend".
    receipt_counts = {}
    gmail_vendor_note = ""
    if gmail_reader.is_configured():
        try:
            receipt_counts = gmail_reader.receipt_counts_by_domain()
        except Exception:
            receipt_counts = {}
    else:
        gmail_vendor_note = (
            '<div class="none-note">Connect Gmail (read-only) on the '
            '<a href="/settings?tab=connections">Connections page</a> to count receipt '
            "emails on file per vendor.</div>")

    def _receipts_note(vendor):
        total = sum(count for domain, count in receipt_counts.items()
                    if gmail_reader.domain_matches_vendor(domain, vendor))
        if not total:
            return ""
        plural = "s" if total != 1 else ""
        return f'<div class="meta">{total} receipt{plural} on file</div>'

    if not rows:
        body = """
        <div class="empty">
          <p><strong>No vendor spend yet.</strong></p>
          <p>Approved expense transactions will be grouped by vendor here.</p>
        </div>"""
    else:
        # Bar chart of this-month spend (rows already sorted by this_month
        # desc). Flagged vendors get a warning-colored bar and a ▲ marker
        # so the spend-jump flag reads on the chart itself; the full table
        # below keeps the detailed "% vs last month" pills.
        chart_rows = []
        for row in rows:
            if row["this_month"] <= 0:
                continue
            flagged = row["jump_pct"] is not None
            chart_rows.append({
                "label": (f'▲ {row["vendor"]}' if flagged else row["vendor"]),
                "value": row["this_month"],
                "value_label": money(row["this_month"]),
                "warn": flagged,
            })
        vendor_chart = charts.horizontal_bar_chart(
            chart_rows, aria="Vendor spend this month")

        table_rows = ""
        for row in rows:
            flag = ""
            if row["jump_pct"] is not None:
                flag = (f'<span class="jump-tag">&#9650; {row["jump_pct"]:.0f}% '
                        f"vs last month</span>")
            row_cls = "row-link"
            if row.get("synthetic"):
                # Live from Stripe, not locally approved history — no drill-down
                # page exists for it, so no link, and lifetime is
                # honestly a dash rather than a fake $0.
                vendor_cell = (f'{escape(row["vendor"])}'
                               f'<span class="tag">from Stripe</span>')
                lifetime_cell = "&mdash;"
                row_cls = ""
            else:
                vendor_cell = (f'<a href="/vendors/detail?vendor='
                               f'{quote(row["vendor"])}">'
                               f'{escape(row["vendor"])}</a>')
                lifetime_cell = money(row["lifetime"])
                vendor_cell += _receipts_note(row["vendor"])
            table_rows += f"""
            <tr class="{row_cls}">
              <td class="vendor-name">{vendor_cell}{flag}</td>
              <td class="num">{money(row["this_month"])}</td>
              <td class="num">{money(row["last_month"])}</td>
              <td class="num">{lifetime_cell}</td>
              <td class="num">{row["count"]}</td>
            </tr>"""
        spent_now = sum(row["this_month"] for row in rows)
        spent_before = sum(row["last_month"] for row in rows)
        jumps = sum(1 for row in rows if row["jump_pct"] is not None)
        vendor_strip = f"""
        <div class="stat-grid mb-4">
          {ui.stat("Spent this month", money(spent_now))}
          {ui.stat("Spent last month", money(spent_before))}
          {ui.stat("Vendors with a jump", str(jumps),
                   tone="out" if jumps else None)}
        </div>"""
        body = f"""
        {vendor_strip}
        <div class="section">
          <div class="section-head"><h2>This month by vendor</h2>
            <span class="tag">largest first</span></div>
          {vendor_chart}
        </div>
        <div class="section">
          <div class="section-head"><h2>Vendor spend</h2></div>
          <table class="table-hover">
            <thead><tr>
              <th data-sort="text">Vendor</th><th class="num" data-sort="num">This month</th>
              <th class="num" data-sort="num">Last month</th><th class="num" data-sort="num">Lifetime</th>
              <th class="num" data-sort="num">Count</th>
            </tr></thead>
            <tbody>{table_rows}</tbody>
          </table>
        </div>"""

    content = f"""
    <div class="sub">Approved expenses grouped by vendor. Spend jumps of
      {threshold_pct:.0f}%+ over last month are flagged (threshold adjustable
      in <a href="/settings">Settings</a>).</div>
    {render_subscriptions_section(subscriptions or [])}
    {body}
    {stripe_vendor_note}
    {gmail_vendor_note}"""

    return render_shell("Vendors", "", content, message, error)


# ---------------------------------------------------------------- Follow-ups


def days_since(iso_timestamp):
    """Whole days between an ISO timestamp and today (best effort)."""
    try:
        submitted = date.fromisoformat(iso_timestamp[:10])
    except (ValueError, TypeError):
        return None
    return (date.today() - submitted).days


EMAIL_NOT_CONFIGURED = ("Email isn't set up. Connect Gmail on the "
                        "Connections page (under Settings) first.")

# render_shell's error banner always escapes its text (correctly — most
# callers pass dynamic/exception-derived strings, e.g. a live bank-API
# error, which must never be trusted as raw HTML). EMAIL_NOT_CONFIGURED
# is the one exception: a fully hardcoded, developer-authored constant
# with no user input in it, so it's safe to render with a real link.
# Rendered inline in the page's own content (never escaped, same as
# every other block on this page) instead of via render_shell's error=
# param, so it doesn't get silently mangled into escaped text.
EMAIL_NOT_CONFIGURED_HTML = (
    'Email isn\'t set up. Connect Gmail on the '
    '<a href="/settings?tab=connections">Connections page</a> (under Settings) first.')


def _review_followups_content(items, error=None):
    email_banner = ""
    shell_error = error
    if error == EMAIL_NOT_CONFIGURED:
        email_banner = f'<div class="banner error">{EMAIL_NOT_CONFIGURED_HTML}</div>'
        shell_error = None  # avoid rendering the same notice twice

    gmail_page_note = ""
    if not gmail_reader.is_configured():
        gmail_page_note = (
            '<div class="none-note">Connect Gmail (read-only) on the '
            '<a href="/settings?tab=connections">Connections page</a> to see whether a '
            "lead already replied before you send.</div>")

    if not items:
        body = """
        <div class="empty">
          <p><strong>No follow-ups waiting for review.</strong></p>
          <p>Overdue leads with no reply will show up here after
             the daily check (needs <a href="/settings?tab=connections">Klaviyo
             connected</a>).</p>
        </div>"""
    else:
        cards = ""
        for item in items:
            days = days_since(item["submitted_at"])
            days_text = f"{days} days ago" if days is not None else "date unknown"
            send_confirm = confirm_onsubmit(
                f"Send this follow-up to {item['email']}?")
            skip_confirm = confirm_onsubmit(
                "Skip this follow-up? No email will be sent.",
                busy_label="Skipping…")
            # Reply check (Gmail read-only, optional): a heads-up, never
            # an auto-skip — sending stays the user's call.
            reply_note = ""
            if gmail_reader.is_configured():
                try:
                    hit = gmail_reader.find_reply(item["email"],
                                                  item["submitted_at"])
                except Exception:
                    hit = None
                if hit:
                    reply_note = (
                        f'<div class="gmail-note">Reply received '
                        f'{escape(hit["date"])} — check the thread before '
                        f"sending; it's still your call.</div>")
            cards += f"""
            <div class="fu-card">
              <div class="fu-head">
                <span class="fu-name">{escape(item["name"])}</span>
                <span class="fu-meta">{escape(item["program"])} &middot;
                  submitted {escape(days_text)} &middot; {escape(item["email"])}</span>
              </div>
              {reply_note}
              <form method="post" action="/followups/send/{item["id"]}">
                <div class="fu-field">
                  <label for="fu-subject-{item["id"]}">Subject</label>
                  <input class="fu-subject" id="fu-subject-{item["id"]}"
                         name="subject" value="{escape(item["draft_subject"] or "")}">
                </div>
                <div class="fu-field">
                  <label for="fu-body-{item["id"]}">Draft</label>
                  <textarea class="fu-body" id="fu-body-{item["id"]}"
                            name="body">{escape(item["draft_body"] or "")}</textarea>
                </div>
                <div class="fu-actions">
                  <button class="approve" type="submit" onclick="{send_confirm}">
                    Send follow-up</button>
                  <button class="deny" type="submit" onclick="{skip_confirm}"
                          formaction="/followups/skip/{item["id"]}">Skip</button>
                </div>
              </form>
            </div>"""
        body = cards

    content = f"""
    {email_banner}
    <div class="sub">Drafted follow-ups for leads with no reply after
      48 hours. Edit the wording, then send or skip. Nothing sends without a
      click.</div>
    {body}
    {gmail_page_note}"""

    return content


# ---------------------------------------------------------------- Invoices


def _review_invoices_content(items, error=None):
    # Same known-safe-constant technique as Follow-ups: render_shell's
    # error= banner unconditionally escapes its text (correct for every
    # OTHER caller here, which pass dynamic messages like "That reminder
    # is no longer pending."). EMAIL_NOT_CONFIGURED is fully hardcoded
    # with no user input, so it's safe to render with a real link —
    # inline in content (never escaped), not via the escaped error= path.
    email_banner = ""
    shell_error = error
    if error == EMAIL_NOT_CONFIGURED:
        email_banner = f'<div class="banner error">{EMAIL_NOT_CONFIGURED_HTML}</div>'
        shell_error = None  # avoid rendering the same notice twice

    gmail_page_note = ""
    if not gmail_reader.is_configured():
        gmail_page_note = (
            '<div class="none-note">Connect Gmail (read-only) on the '
            '<a href="/settings?tab=connections">Connections page</a> to spot payment-'
            "confirmation emails before sending a reminder.</div>")

    if not items:
        body = """
        <div class="empty">
          <p><strong>No invoice reminders waiting for review.</strong></p>
          <p>When a native invoice goes overdue, a drafted payment reminder
             will show up here after the daily check.</p>
        </div>"""
    else:
        cards = ""
        for item in items:
            overdue_days = days_since(item["due_date"])
            overdue_text = (f"{overdue_days} days overdue"
                            if overdue_days is not None else "due date unknown")
            # Urgency tiers — color/weight only, no structural change:
            # 1-7 days warns, 8-30 reads as expense red, 31+ turns the
            # whole head line red and bold (.fu-head.overdue-severe).
            days_cls = head_cls = ""
            if overdue_days is not None:
                if overdue_days >= 31:
                    days_cls = "overdue-late"
                    head_cls = " overdue-severe"
                elif overdue_days >= 8:
                    days_cls = "overdue-late"
                elif overdue_days >= 1:
                    days_cls = "overdue-warn"
            overdue_html = (f'<span class="{days_cls}">{escape(overdue_text)}'
                            "</span>" if days_cls else escape(overdue_text))
            send_confirm = confirm_onsubmit(
                f"Send this reminder to {item['customer_email']}?")
            skip_confirm = confirm_onsubmit(
                "Skip this reminder? It will be redrafted next week if "
                "the invoice is still overdue.", busy_label="Skipping…")
            # Payment-confirmation check (Gmail read-only, optional):
            # surfaces "they may already have paid" before a
            # reminder goes out. Match: client address + amount within
            # 5% (see gmail_reader.amounts_match).
            paid_note = ""
            if gmail_reader.is_configured():
                try:
                    amount = float(
                        (item["amount_due"] or "0").split()[0]
                        .replace(",", "").lstrip("$"))
                except (ValueError, IndexError):
                    amount = 0.0
                try:
                    hit = gmail_reader.find_payment_confirmation(
                        item["customer_email"], amount)
                except Exception:
                    hit = None
                if hit:
                    paid_note = (
                        f'<div class="gmail-note">Payment email received '
                        f'{escape(hit["date"])} — they may have paid and '
                        f"the invoice may not be marked paid yet. Check before "
                        f"sending.</div>")
            cards += f"""
            <div class="fu-card">
              <div class="fu-head{head_cls}">
                <span class="fu-name">{escape(item["customer_name"] or "Unknown customer")}</span>
                <span class="fu-meta">Invoice #{escape(item["invoice_number"] or "?")} &middot;
                  {escape(item["amount_due"])} due &middot; {overdue_html} &middot;
                  {escape(item["customer_email"])}</span>
              </div>
              {paid_note}
              <form method="post" action="/invoices/send/{item["id"]}">
                <div class="fu-field">
                  <label for="inv-subject-{item["id"]}">Subject</label>
                  <input class="fu-subject" id="inv-subject-{item["id"]}"
                         name="subject" value="{escape(item["draft_subject"] or "")}">
                </div>
                <div class="fu-field">
                  <label for="inv-body-{item["id"]}">Draft</label>
                  <textarea class="fu-body" id="inv-body-{item["id"]}"
                            name="body">{escape(item["draft_body"] or "")}</textarea>
                </div>
                <div class="fu-actions">
                  <button class="approve" type="submit" onclick="{send_confirm}">
                    Send reminder</button>
                  <button class="deny" type="submit" onclick="{skip_confirm}"
                          formaction="/invoices/skip/{item["id"]}">Skip</button>
                </div>
              </form>
            </div>"""
        body = cards

    content = f"""
    {email_banner}
    <div class="sub">Drafted payment reminders for overdue native invoices.
      Edit the wording, then send or skip. Skipped reminders come back in a
      week if still unpaid. Nothing sends without a click.</div>
    {body}
    {gmail_page_note}"""

    return content


def _review_recurring_content(instances):
    """Recurring invoice cycles waiting to be approved.

    Per-row approve/skip, matching Payments, Follow-ups and invoice
    reminders. Deliberately no bulk bar: bulk exists only on the
    Transactions queue, and the three sibling queues all decide one at a
    time. Adding a fourth idiom here would be the inconsistency, not the
    consistency -- and a recurring invoice is money leaving the building,
    which is the last place to make approving many at once frictionless.
    """
    if not instances:
        return """
        <div class="sub">Recurring invoices that have come due, waiting for
          you to approve them. Nothing is sent on a schedule &mdash; every
          cycle lands here first.</div>
        <div class="empty">
          <p><strong>No recurring invoices waiting.</strong></p>
          <p>When a template's next date arrives, its invoice shows up here
             to approve or skip. Set one up on
             <a href="/invoices?tab=recurring">Recurring templates</a>.</p>
        </div>"""

    cards = ""
    for instance in instances:
        totals = recurring_invoices.preview_totals(instance["template_id"])
        gross = Decimal(totals["gross"]) / 100
        overdue = days_since(instance["due_date"])
        age = ""
        if overdue and overdue > 0:
            # A cycle queued by catch-up can be weeks old. Saying so
            # matters: the issue date will be the original due date, not
            # today, and that changes which period it lands in.
            age = (f' &middot; <span class="overdue-warn">generated for a '
                   f"date {overdue} days ago</span>")
        approve_attrs = ui.confirm_attrs(
            f"Create and send this invoice for "
            f"{escape(instance['contact_name'])} for {money(gross)}?",
            title="Approve recurring invoice",
            confirm_label="Create and send", busy_label="Sending…")
        skip_attrs = ui.confirm_attrs(
            "Skip this cycle? The template keeps running and the next one "
            "arrives as normal.",
            title="Skip this cycle", confirm_label="Skip",
            busy_label="Skipping…")
        name = instance["template_name"] or "Recurring invoice"
        cards += f"""
        <div class="fu-card">
          <div class="fu-head">
            <span class="fu-name">{escape(instance["contact_name"])}</span>
            <span class="fu-meta">{escape(name)} &middot;
              {escape(instance["due_date"])}{age} &middot;
              <span class="amount">{money(gross)}</span></span>
          </div>
          <div class="fu-actions">
            <form method="post" class="inline-form"
                  action="/review/recurring/{instance["id"]}/approve"
                  {approve_attrs}>
              <button class="approve" type="submit">Create and send</button>
            </form>
            <form method="post" class="inline-form"
                  action="/review/recurring/{instance["id"]}/skip"
                  {skip_attrs}>
              <button class="deny" type="submit">Skip</button>
            </form>
          </div>
        </div>"""

    return f"""
    <div class="sub">Recurring invoices that have come due. Approving one
      creates and sends a real invoice, posting exactly what a manually
      written invoice of the same value would. Nothing sends on its own.</div>
    {cards}"""


# ---------------------------------------------------------------- Insights


# Rule thresholds for deterministic flags (no AI commentary anywhere:
# every flag is plain arithmetic on snapshot numbers).
EVENT_DROP_RATIO = 0.8         # current < 80% of prior -> flag
TRAFFIC_DROP_RATIO = 0.7       # current < 70% of prior -> flag
MIN_EVENTS_FOR_FLAG = 10       # ignore noise below this volume
MIN_SESSIONS_FOR_FLAG = 10


def pct_change_text(current, prior):
    if prior == 0:
        return "New" if current else "no change"
    return f"{(current - prior) / prior * 100:+.0f}%"


def compute_insight_flags(week):
    """Deterministic flags from the 7-day window. Returns a list of
    (kind, message) where kind is 'warn' or 'info'."""
    flags = []

    traffic = week["traffic"]
    cur_sessions = traffic["current"]["sessions"]
    prior_sessions = traffic["prior"]["sessions"]
    if (prior_sessions >= MIN_SESSIONS_FOR_FLAG
            and cur_sessions < prior_sessions * TRAFFIC_DROP_RATIO):
        flags.append(("warn",
                      f"Traffic dropped significantly this week: "
                      f"{prior_sessions} sessions last week, "
                      f"{cur_sessions} this week."))

    events = week.get("events", {"current": {}, "prior": {}})
    for name, prior_count in events["prior"].items():
        current_count = events["current"].get(name, 0)
        if (prior_count >= MIN_EVENTS_FOR_FLAG
                and current_count < prior_count * EVENT_DROP_RATIO):
            flags.append(("warn",
                          f'"{name}" events are down: {prior_count} last '
                          f"week, {current_count} this week."))
    return flags


def render_events_table(events):
    names = sorted(set(events["current"]) | set(events["prior"]))
    if not names:
        return ('<div class="none-note">No conversion events configured. '
                'Add event names on the <a href="/settings?tab=connections">Connections '
                "page</a> (GA4 card) to track them here.</div>")
    rows = ""
    for name in names:
        current = events["current"].get(name, 0)
        prior = events["prior"].get(name, 0)
        rows += f"""
        <tr>
          <td class="mono">{escape(name)}</td>
          <td class="num">{current:,}</td>
          <td class="num">{prior:,}</td>
          <td class="num">{escape(pct_change_text(current, prior))}</td>
        </tr>"""
    return (f'<table><thead><tr><th>Event</th><th class="num" data-sort="num">This '
            f'period</th><th class="num" data-sort="num">Prior</th><th class="num" data-sort="num">Change'
            f"</th></tr></thead><tbody>{rows}</tbody></table>")


# The landing funnel, in order. These are GA4 conversion event names; the
# funnel plots whatever counts the snapshot holds for them (0 / missing →
# the chart degrades to "Not enough data yet"). Rendering only — no new
# GA4 query.
FUNNEL_STAGES = [
    ("Quiz started", "quiz_started"),
    ("Quiz completed", "quiz_completed"),
    ("Waitlist joined", "waitlist_joined"),
]


def traffic_history(snapshots, metric="sessions"):
    """(values, labels) of the trailing-7-day `metric` across the stored
    daily snapshots, oldest first — the trend the sparkline plots. Reads
    existing snapshot rows only; malformed/older ones are skipped."""
    values, labels = [], []
    for row in snapshots:
        try:
            snap = json.loads(row["snapshot_json"])
        except (ValueError, TypeError):
            continue
        current = (snap.get("windows", {}).get("7", {})
                   .get("traffic", {}).get("current", {}))
        if metric not in current:
            continue
        values.append(int(current[metric]))
        labels.append((row.get("date") or "")[5:])  # MM-DD
    return values, labels


def _insights_nav(active):
    return ui.sub_nav([("overview", "Overview", "/insights"),
                       ("financial", "Financial", "/insights?tab=financial"),
                       ("marketing", "Marketing", "/insights?tab=marketing")],
                      active, "Insights sections")


def _render_marketing_insights_page(snapshot_row, message=None, error=None,
                                    trend_range="8w"):
    ga4_on = ga4_client.is_configured()
    if not ga4_on:
        content = f"""
        {_insights_nav("marketing")}
        <div class="sub">Website analytics from Google Analytics, refreshed
          once a day by the morning check.</div>
        <div class="empty">
          <p><strong>GA4 isn't connected.</strong></p>
          <p>This tab is optional — connect GA4 on the
            <a href="/settings?tab=connections">Connections</a> page whenever you like.</p>
        </div>"""
        return render_shell("Insights", "", content, message, error)

    if snapshot_row is None:
        content = f"""
        {_insights_nav("marketing")}
        <div class="sub">Website analytics from GA4, refreshed once a day
          by the morning check.</div>
        <div class="empty">
          <p><strong>No snapshot yet.</strong></p>
          <p>The daily check takes the first snapshot at 8am — or run the
            daily check now from
            <a href="/settings?tab=automation">Settings &rarr; Automation</a>.</p>
        </div>"""
        return render_shell("Insights", "", content, message, error)

    snapshot = json.loads(snapshot_row["snapshot_json"])
    windows = snapshot.get("windows", {})
    week = windows.get("7")
    month = windows.get("30")
    if not week or not month or "events" not in week:
        content = f"""
        {_insights_nav("marketing")}
        <div class="empty">
          <p><strong>The stored snapshot is from an older version.</strong></p>
          <p>A fresh one is taken at the next daily check (or run it now
            from <a href="/settings?tab=automation">Settings &rarr; Automation</a>).</p>
        </div>"""
        return render_shell("Insights", "", content, message, error)

    flags = compute_insight_flags(week)
    if flags:
        flags_html = "".join(
            f'<div class="flag flag-{kind}">{escape(text)}</div>'
            for kind, text in flags
        )
    else:
        # Calm "checked, all clear" state — an absent flag row reads as
        # "not checked", and the intro copy above promises "flags below"
        # (same anti-pattern as Home's old vanishing vendor-flag card).
        flags_html = ('<div class="flag flag-info">No drop flags this week '
                      "— traffic and conversion events checked against last "
                      "week's numbers.</div>")

    t_cur, t_prior = week["traffic"]["current"], week["traffic"]["prior"]
    traffic_rows = ""
    for label, key in (("Users", "users"), ("Sessions", "sessions"),
                       ("Page views", "page_views")):
        traffic_rows += f"""
        <tr>
          <td>{label}</td>
          <td class="num">{t_cur[key]:,}</td>
          <td class="num">{t_prior[key]:,}</td>
          <td class="num">{escape(pct_change_text(t_cur[key], t_prior[key]))}</td>
        </tr>"""

    # Top pages / product popularity as a horizontal bar chart (was a
    # ranked table). Values are labeled so the numbers are still present.
    pages_html = charts.horizontal_bar_chart(
        [{"label": p["path"], "value": p["views"],
          "value_label": f'{p["views"]:,}'} for p in month["top_pages"]],
        aria="Top pages by views")

    # Landing funnel (30-day counts) as a shrinking-width funnel.
    funnel_html = charts.funnel_chart(
        [(label, month["events"]["current"].get(key, 0))
         for label, key in FUNNEL_STAGES],
        aria="Quiz to waitlist funnel")

    # End-to-end headline under the funnel: last stage as a share of the
    # first. Ratio wording, not cohort wording — the last stage can fire
    # from entry points outside the funnel (see charts.funnel_chart).
    first_label, first_key = FUNNEL_STAGES[0]
    last_label, last_key = FUNNEL_STAGES[-1]
    first_n = month["events"]["current"].get(first_key, 0)
    last_n = month["events"]["current"].get(last_key, 0)
    funnel_note = ""
    if first_n > 0:
        funnel_note = (
            f'<div class="none-note">End to end: {last_n:,} '
            f'&ldquo;{escape(last_label)}&rdquo; per {first_n:,} '
            f'&ldquo;{escape(first_label)}&rdquo; — about '
            f"{last_n / first_n * 100:.0f}%.</div>")

    # Sessions / users trend across the stored daily snapshots, over the
    # chosen window (8w / 12w / 12mo).
    snapshots = db.list_ga4_snapshots(
        limit=TREND_SNAPSHOT_LIMIT.get(trend_range, 60))
    sess_vals, sess_labels = traffic_history(snapshots, "sessions")
    user_vals, user_labels = traffic_history(snapshots, "users")
    sessions_spark = charts.sparkline(
        sess_vals, labels=sess_labels,
        last_label=(f"{sess_vals[-1]:,}" if sess_vals else None),
        aria="Sessions over time")
    users_spark = charts.sparkline(
        user_vals, labels=user_labels,
        last_label=(f"{user_vals[-1]:,}" if user_vals else None),
        aria="Users over time")

    generated = (snapshot.get("generated_at") or "")[:16].replace("T", " ")

    content = f"""
    {_insights_nav("marketing")}
    <div class="sub">Website analytics from GA4, refreshed once a day by the
      morning check. Numbers are computed, never interpreted: flags below use
      plain thresholds.</div>
    {flags_html}
    <div class="section">
      <div class="section-head"><h2>Quiz to waitlist funnel</h2>
        <span class="tag">last 30 days</span></div>
      {funnel_html}
      {funnel_note}
    </div>
    <div class="section">
      <div class="section-head"><h2>Traffic</h2></div>
      {render_range_toggle("/insights", trend_range, "tab=marketing")}
      <div class="chart-pair">
        <div>
          <div class="chart-title">Sessions (7-day, by snapshot)</div>
          {sessions_spark}
        </div>
        <div>
          <div class="chart-title">Users (7-day, by snapshot)</div>
          {users_spark}
        </div>
      </div>
      <table class="mt-4">
        <thead><tr><th data-sort="text">Metric</th><th class="num" data-sort="num">This week</th>
          <th class="num" data-sort="num">Last week</th><th class="num" data-sort="num">Change</th></tr></thead>
        <tbody>{traffic_rows}</tbody>
      </table>
    </div>
    <div class="section">
      <div class="section-head"><h2>Conversion events</h2>
        <span class="tag">last 7 days vs prior 7</span></div>
      {render_events_table(week["events"])}
    </div>
    <div class="section">
      <div class="section-head"><h2>Conversion events</h2>
        <span class="tag">last 30 days vs prior 30</span></div>
      {render_events_table(month["events"])}
    </div>
    <div class="section">
      <div class="section-head"><h2>Top pages</h2>
        <span class="tag">last 30 days</span></div>
      {pages_html}
    </div>
    <div class="snapshot-meta">Last updated: {escape(generated)} UTC
      (daily snapshot, not live data)</div>"""

    return render_shell("Insights", "", content, message, error)


def _render_cross_source_insights_page(metrics, message=None, error=None,
                                       metrics_failed=False):
    """Every available shared-registry metric; no pinned filtering."""
    metrics = list(metrics or [])
    if metrics_failed:
        body = """
        <div class="insight-metrics-empty">
          <strong>Insights could not read the stored source data.</strong>
          <span>Reload this page to try again. No source system was contacted.</span>
          <a href="/insights">Reload Insights</a>
        </div>"""
    elif metrics:
        body = ('<div class="insight-metric-grid insights-overview-grid">'
                + "".join(_insight_metric_link(metric) for metric in metrics)
                + "</div>")
    else:
        body = """
        <div class="insight-metrics-empty">
          <strong>No cross-source metrics are ready yet.</strong>
          <span>Connect Shopify or Plaid and let Klearing store the first sync.</span>
          <a href="/settings?tab=connections">Open Connections</a>
        </div>"""

    source_order = ("shopify", "plaid", "inventory")
    sources = [source.title() for source in source_order
               if any(metric["source"] == source for metric in metrics)]
    source_text = " + ".join(sources) if sources else "Local source data"
    count = len(metrics)
    scope = f"{count} {_plural(count, 'metric')} \u00b7 {source_text}"
    content = f"""
    {_insights_nav("overview")}
    <div class="sub measure-prose">Every available Shopify, Plaid, and
      Inventory metric from the same local registry used by Home. Values
      compare with the preceding stored period; no source is fetched when
      this page opens.</div>
    <section class="home-insights insights-overview"
             aria-labelledby="insights-overview-title">
      <div class="insight-metrics-head">
        <div>
          <h2 id="insights-overview-title">Across your business</h2>
          <p>{escape(scope)}</p>
        </div>
        <a class="insight-metrics-action"
           href="/settings#home-insights-widget">Customize Home</a>
      </div>
      {body}
    </section>"""
    return render_shell("Insights", "", content, message, error, wide=True)


def _financial_metric(label, value, element_id=None, mixed=False,
                      tone=""):
    value_class = "value"
    if tone:
        value_class += f" {tone}"
    if mixed:
        value_class += " mixed"
    id_html = f' id="{element_id}"' if element_id else ""
    return (f'<div class="planning-result"><span class="label">{label}</span>'
            f'<span class="{value_class}"{id_html}>{value}</span></div>')


def _render_financial_insights_page(data, message=None, error=None):
    trends = data["trends"]
    margin_rows = [row for row in trends if row["net_revenue_cents"] > 0]
    labels = [row["label"] for row in margin_rows]
    gross_values = [row["gross_margin_pct"] for row in margin_rows]
    net_values = [row["net_margin_pct"] for row in margin_rows]
    gross_chart = charts.sparkline(
        gross_values, labels=labels,
        last_label=f"{gross_values[-1]:.1f}%" if gross_values else None,
        zero_baseline=True, aria="Gross margin trend over six months")
    net_chart = charts.sparkline(
        net_values, labels=labels,
        last_label=f"{net_values[-1]:.1f}%" if net_values else None,
        zero_baseline=True, aria="Net margin trend over six months")

    def optional_money(value):
        return money(value / 100) if value is not None else "Not available"

    payroll = (f'{data["payroll_ratio_pct"]:.1f}%'
               if data["payroll_ratio_pct"] is not None else "Not available")
    dso = (f'{data["dso_days"]:.0f} days'
           if data["dso_days"] is not None else "Not available")
    no_data = ""
    if data["source_entry_count"] == 0:
        no_data = ('<div class="flag flag-info">No native Ledger entries '
                   'yet. <a href="/ledger?tab=sync">Post local source records</a> '
                   'to start building financial trends.</div>')

    content = f"""
    {_insights_nav("financial")}
    <div class="sub">Decision-ready metrics from the native double-entry
      Ledger. Legacy Wave Tax records never affect these numbers.</div>
    {no_data}
    <div id="financial-insights" class="tax-workspace"
         data-monthly-revenue="{data['monthly_revenue_cents']}"
         data-monthly-expenses="{data['monthly_expenses_cents']}">
      <div class="planning-results" aria-label="Financial health summary">
        {_financial_metric("Daily burn", money(data["daily_burn_cents"] / 100))}
        {_financial_metric("Monthly burn", money(data["monthly_burn_cents"] / 100))}
        {_financial_metric("Current cash", "Checking...", "insights-cash-balance", mixed=True)}
        {_financial_metric("Runway", "Checking...", "insights-runway", mixed=True)}
      </div>
      <p id="insights-cash-status" class="planning-note" aria-live="polite">
        Cash and runway use the selected business-cash accounts from Settings.</p>

      <div class="section">
        <div class="section-head"><h2>Margin trends</h2>
          <span class="tag">native Ledger, 6 months</span></div>
        <div class="chart-pair">
          <div><div class="chart-title">Gross margin</div>{gross_chart}</div>
          <div><div class="chart-title">Net margin</div>{net_chart}</div>
        </div>
      </div>

      <div class="section">
        <div class="section-head"><h2>Operating efficiency</h2>
          <span class="tag">trailing {data['window']['days']} days</span></div>
        <div class="planning-results">
          {_financial_metric("Payroll ratio", payroll, mixed=data["payroll_ratio_pct"] is None)}
          {_financial_metric("Customer LTV", optional_money(data["ltv_cents"]), mixed=data["ltv_cents"] is None)}
          {_financial_metric("Acquisition cost", optional_money(data["cac_cents"]), mixed=data["cac_cents"] is None)}
          {_financial_metric("Overdue DSO estimate", dso, mixed=True)}
        </div>
        <div class="none-note">Payroll ratio uses Ledger accounts categorized
          as wages and employee benefits against revenue. Category-dependent
          metrics remain unavailable until every used account is categorized.
          LTV and acquisition cost also require local Shopify customers. DSO is
          an estimate from tracked overdue invoices, not a complete
          receivables ledger.</div>
      </div>

      <div class="section">
        <div class="section-head"><h2>What-if scenario</h2>
          <span class="tag">local calculation</span></div>
        <form id="insights-scenario-form" class="planning-controls">
          <div class="planning-manual">
            <label>Revenue change (%)<input name="revenue_change" type="number"
              min="-100" max="1000" step="1" value="0" inputmode="decimal"></label>
            <label>Expense change (%)<input name="expense_change" type="number"
              min="-100" max="1000" step="1" value="0" inputmode="decimal"></label>
            <label>One-time cost ($)<input name="one_time_cost" type="number"
              min="0" step="1" value="0" inputmode="decimal"></label>
            <button type="reset">Reset</button>
          </div>
        </form>
        <div class="planning-results" aria-live="polite">
          {_financial_metric("Monthly revenue", money(data["monthly_revenue_cents"] / 100), "scenario-revenue")}
          {_financial_metric("Monthly expenses", money(data["monthly_expenses_cents"] / 100), "scenario-expenses")}
          {_financial_metric("Net income", money((data["monthly_revenue_cents"] - data["monthly_expenses_cents"]) / 100), "scenario-net")}
          {_financial_metric("Net margin", "Not available", "scenario-margin", mixed=True)}
          {_financial_metric("Runway after scenario", "Not available", "scenario-runway", mixed=True)}
        </div>
        <p class="planning-note">The scenario changes no transactions and saves
          nothing. One-time cost reduces available cash without becoming an
          ongoing monthly burn assumption.</p>
      </div>
    </div><script src="/static/insights/financial.js?v=1"></script>"""
    return render_shell("Insights", "", content, message, error, wide=True)


def render_insights_page(snapshot_row, message=None, error=None,
                         trend_range="8w", tab="overview", financial=None,
                         metrics=None, metrics_failed=False):
    if tab == "marketing":
        return _render_marketing_insights_page(
            snapshot_row, message=message, error=error,
            trend_range=trend_range)
    if tab == "financial":
        data = financial or financial_insights.empty_snapshot()
        return _render_financial_insights_page(data, message=message, error=error)
    return _render_cross_source_insights_page(
        metrics, message=message, error=error, metrics_failed=metrics_failed)


# ---------------------------------------------------------------- Logs


def _error_log_entry_count():
    """Entries in error.log — one per record_failure() separator line;
    plain non-empty lines count individually when no separator exists
    (nothing but record_failure writes here today)."""
    err_file = paths.log_path("error")
    if not err_file.exists():
        return 0
    try:
        lines = err_file.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
    except OSError:
        return 0
    separators = sum(1 for line in lines if line == "=" * 60)
    if separators:
        return separators
    return sum(1 for line in lines if line.strip())


SETTINGS_TABS = (
    ("general", "General"),
    ("connections", "Connections"),
    ("automation", "Automation"),
    ("logs", "Logs"),
)


def _settings_nav(active):
    """Connections, Automation and Logs were three sidebar entries that
    are all the same thing: how the app is set up and what it has been
    doing. They are tabs of Settings now, which is where somebody looks
    for them anyway."""
    return ui.sub_nav([(key, label, f"/settings?tab={key}")
                       for key, label in SETTINGS_TABS],
                      active, "Settings sections")


def render_logs_page(active, message=None, error=None):
    # Tab semantics on the file switcher (the tabs really do swap the
    # visible content, via a server round-trip). The error tab carries a
    # count badge when entries exist — recomputed on every render, no
    # polling; a server-rendered page re-counts on each load anyway.
    error_count = _error_log_entry_count()
    tabs = ui.sub_nav(
        [(name, f"{name}.log", f"/settings?tab=logs&file={name}",
          error_count if name == "error" and error_count else None,
          "bad" if name == "error" else None)
         for name in paths.LOG_NAMES],
        active, "Log file")

    log_file = paths.log_path(active)
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
        except OSError as exc:
            lines = [f"Could not read the log: {exc}"]
        tail = lines[-300:]
        text = "\n".join(tail) or "(empty)"
        if len(lines) > 300:
            note = f"Showing the last {len(tail)} of {len(lines)} lines."
        else:
            plural = "s" if len(lines) != 1 else ""
            note = f"{len(lines)} line{plural}."
    else:
        text = "(no entries yet)"
        note = ""

    # The packaged app runs in a native window with no browser chrome,
    # so there's no F5 — an in-page refresh is the only way to see new
    # lines while a check is running.
    refresh = (f'<a href="/settings?tab=logs&file={active}">Refresh this view</a>'
               f" &middot; ")

    content = f"""
    <div class="sub">What the app and its scheduled checks have been doing —
      no terminal needed. <span class="mono">app</span> is the web app,
      <span class="mono">daily</span> the morning check,
      <span class="mono">weekly</span> the digest,
      <span class="mono">error</span> full failure tracebacks.</div>
    {tabs}
    <div class="log-box" tabindex="0">{escape(text)}</div>
    <div class="snapshot-meta">{refresh}{note} Files live in
      {escape(str(paths.data_dir()))}</div>"""
    return render_shell("Settings", "", _settings_nav("logs") + content,
                        message, error)


# ---------------------------------------------------------------- AI assistant
#
# Local Ollama by default (http://localhost:11434); OpenAI or Anthropic
# only by explicit, warned opt-in in Settings (see ai_providers). The
# app has no hard dependency on any of them: /ai/status drives the
# button's offline state, and /ai/stream answers 503 with a plain
# instruction when the selected provider isn't usable. Context is
# rebuilt fresh per message from DB aggregates — never raw transaction
# rows (see ollama_client.build_context).


@app.get("/ai/status")
def ai_status():
    return ai_providers.status()


@app.post("/ai/stream")
async def ai_stream(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return Response("Bad request.", status_code=400,
                        media_type="text/plain")
    message = (str(payload.get("message") or "")).strip()[:2000]
    section = (str(payload.get("section") or "")).strip()[:40]
    if not message:
        return Response("Say something first.", status_code=400,
                        media_type="text/plain")
    provider_status = ai_providers.status()
    if not provider_status["available"]:
        # Calm degraded state either way — Ollama offline, or a cloud
        # provider selected with no key saved yet. Never a crash.
        return Response(provider_status["hint"],
                        status_code=503, media_type="text/plain")

    def generate():
        try:
            # Native tool calling: the model decides which Klearing
            # aggregates to query per question, through whichever
            # provider is selected (local Ollama by default). The
            # Ollama path still falls back internally to static
            # context for models without tool support.
            for chunk in ai_providers.chat_stream(message,
                                                  section=section):
                yield chunk
        except Exception:
            # Mid-stream failures (model unloaded, server stopped,
            # API error) end the answer with an honest note instead
            # of dying silently.
            yield ("\n[The assistant lost its connection to "
                   f"{provider_status['label']}.]")

    return StreamingResponse(generate(), media_type="text/plain")


# ---------------------------------------------------------------- Connections


def conn_mark_tested(service, ok=True, detail=""):
    """Record that a connection check ran for this service just now,
    and how it went."""
    db.mark_connection_tested(service, ok=ok, detail=detail)


# Services whose check actually pulls data (a sync), vs. ones whose
# check only validates the credential/connectivity. Drives the card's
# "Last synced" vs "Last tested" wording — every card shows one of the
# two, never neither.
_CONN_SYNC_SERVICES = {"bank", "plaid", "stripe", "gusto", "gmail_reader",
                       "shopify"}
# Deliberately NOT "simplefin": check_bank.py's daily sync only ever
# stamps db.mark_connection_tested("bank"), never "simplefin" -- so
# claiming this is a sync service would make the SimpleFIN card's
# freshness line permanently wrong (it could only ever be updated by a
# manual Re-test click, which tests the connection but does not sync
# anything). "Last tested" is the honest wording here.


def _conn_tested_line(service):
    """The per-card freshness line — stamped by the manual Re-test and
    by the daily check's bank/GA4 steps. Three persistent states, not
    just a timestamp: a failed last test stays red with its reason, a
    48h+-old stamp goes amber, otherwise the plain muted line."""
    verb = "Last synced" if service in _CONN_SYNC_SERVICES else "Last tested"
    stamp = db.get_connection_tested(service)
    if not stamp:
        never = ("Not yet synced" if service in _CONN_SYNC_SERVICES
                 else "Not yet tested")
        return f'<div class="conn-tested never">{never}</div>'
    shown = stamp.replace("T", " ")[:16]
    result = db.get_connection_result(service)
    if result and result != "ok":
        return (f'<div class="conn-tested error">Last test failed &mdash; '
                f'{escape(result)} (<span class="mono">{escape(shown)}</span>'
                f") &middot; use Re-test below</div>")
    try:
        age_hours = ((datetime.now() - datetime.fromisoformat(stamp))
                     .total_seconds() / 3600)
    except ValueError:
        age_hours = None
    if age_hours is not None and age_hours >= 48:
        return (f'<div class="conn-tested stale">{verb}: '
                f'<span class="mono">{escape(shown)}</span> &middot; '
                f"over 48 hours ago</div>")
    return (f'<div class="conn-tested">{verb}: '
            f'<span class="mono">{escape(shown)}</span></div>')


def _conn_card(name, connected, detail, actions_html, service=None):
    """One integration.

    Connected services render open, because their status and last-tested
    stamp are the reason to visit this page. Unconnected ones collapse to
    a single line: thirteen cards, thirty forms and thirty-two buttons on
    one 2500px page meant the two things you had set up were buried under
    eleven you had not, with no way to tell without scrolling.
    """
    state = ('<span class="tag ok">connected</span>' if connected
             else '<span class="tag off">not connected</span>')
    tested = _conn_tested_line(service) if service else ""
    body = f"""
      <div class="conn-detail">{detail}</div>
      {tested}
      <div class="wizard-actions mt-4">{actions_html}</div>"""
    if connected:
        return f"""
    <div class="section">
      <div class="conn-row"><span class="conn-name">{escape(name)}</span>{state}</div>
      {body}
    </div>"""
    return f"""
    <details class="disclosure conn-collapsed">
      <summary><span class="conn-name">{escape(name)}</span>{state}
        <span class="why"></span></summary>
      <div class="disclosure-body">{body}</div>
    </details>"""


def _conn_group(label):
    """A label + divider between clusters of connection cards — purely
    organizational, the cards themselves (_conn_card) are unchanged."""
    return (f'<div class="conn-group">'
            f'<span class="conn-group-label">{escape(label)}</span>'
            f'<span class="conn-group-line"></span></div>')


def _format_account_name_list(names, cap=5):
    """Join already-escaped account names for a warning line, capped so
    a person with many unresolved/remapped accounts never gets an
    unbounded wall of names inlined into one sentence."""
    if len(names) <= cap:
        return ", ".join(names)
    remaining = len(names) - cap
    return ", ".join(names[:cap]) + f", and {remaining} more"


def render_connections_page(message=None, error=None):
    db.init_db()

    # Bank
    adapter = banks.active_adapter()
    # is_configured() makes a live secrets_store call, which can raise
    # secrets_store.SecretsError (e.g. the OS credential-store key no
    # longer matches what encrypted this credential) -- narrowly caught
    # here, not swallowed as a bare "not connected", since that is a
    # real, fixable problem with its own actionable message, not the
    # same thing as never having connected a bank at all.
    bank_credential_error = False
    try:
        bank_on = adapter is not None and adapter.is_configured()
    except secrets_store.SecretsError:
        bank_on = False
        bank_credential_error = True
    bank_name = adapter.display_name if adapter else "Bank"
    if bank_credential_error:
        bank_detail = (
            f"{escape(bank_name)}'s stored credentials could not be "
            "read right now -- the encryption key in your system's "
            "credential store no longer matches. Reconnect it from "
            "Setup.")
    elif bank_on:
        bank_detail = f"Reading transactions from {escape(bank_name)} (read-only)."
    else:
        bank_detail = "Required. The pending queue fills from here."
    bank_actions = """
      <form method="post" action="/connections/test/bank" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>
      <a class="btn-link" href="/setup/step/3">Update / switch bank</a>"""
    # Once SimpleFIN is the active bank, the dedicated "Bank — SimpleFIN
    # Bridge" card below already covers this exact state, more
    # completely (setup progress, mapped-account count, the persistent
    # unresolved-account warning) -- showing this generic card too would
    # be a second, identically-titled card with a DIFFERENT (and wrong)
    # freshness line, since check_bank.py's daily sync only ever stamps
    # the "bank" service key, never "simplefin". Suppress this one,
    # smaller-scoped card rather than duplicate/contradict the dedicated
    # one; every other active bank keeps this card exactly as before.
    show_generic_bank_card = not (adapter is not None
                                  and adapter.id == "simplefin")

    # Plaid Phase 1 (dotenv-backed app credentials, non-OAuth institutions).
    # OAuth redirect handling and webhooks are deliberately deferred to
    # Phase 2. Access tokens and Item IDs remain in the encrypted vault.
    plaid_status = plaid_client.credential_status()
    try:
        plaid_on = (secrets_store.has_secret(plaid_client.TOKEN_SECRET_NAME)
                    and secrets_store.has_secret(plaid_client.ITEM_ID_SECRET_NAME))
    except Exception:
        plaid_on = False
    try:
        plaid_connection_id = (secrets_store.get_secret(plaid_client.ITEM_ID_SECRET_NAME)
                               if plaid_on else None)
    except Exception:
        plaid_connection_id = None
    plaid_detail = (
        "A bank is connected through Plaid Link. Read-only transactions can "
        "be synced locally."
        if plaid_on else
        f'Phase 1 supports non-OAuth institutions in Plaid {escape(plaid_status["environment"])}. '
        f'Add PLAID_CLIENT_ID and PLAID_SECRET to <code>{escape(plaid_status["path"])}</code>.'
    )
    plaid_sync_button = (
        f'<button class="check" type="button" id="plaid-sync-btn" '
        f'data-connection-id="{escape(plaid_connection_id)}">Sync now</button>'
        if plaid_connection_id else ""
    )
    plaid_actions = plaid_sync_button + """
      <button class="save-btn" type="button" id="plaid-connect-btn">Connect Bank</button>
      <div class="test-status" id="plaid-link-status" aria-live="polite"
           class="mt-2"></div>
      <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
      <script>
      (function () {
        const button = document.getElementById('plaid-connect-btn');
        const syncButton = document.getElementById('plaid-sync-btn');
        const status = document.getElementById('plaid-link-status');
        if (!button) return;
        button.addEventListener('click', async function () {
          button.disabled = true;
          status.textContent = 'Starting Plaid Link…';
          try {
            const tokenResp = await fetch('/plaid/link-token', {method: 'POST'});
            const tokenData = await tokenResp.json();
            if (!tokenResp.ok || !tokenData.link_token) {
              throw new Error(tokenData.error || 'Could not start Plaid Link.');
            }
            if (!window.Plaid) throw new Error('Plaid Link did not load. Check your network connection.');
            const handler = window.Plaid.create({
              token: tokenData.link_token,
              onSuccess: async function (publicToken) {
                try {
                  status.textContent = 'Finishing bank connection…';
                  const exchangeResp = await fetch('/plaid/exchange', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({public_token: publicToken})
                  });
                  const exchangeData = await exchangeResp.json();
                  if (!exchangeResp.ok || !exchangeData.ok) {
                    throw new Error(exchangeData.error || 'Could not finish the bank connection.');
                  }
                  window.location = '/connections?msg=' + encodeURIComponent('Bank connected through Plaid.');
                } catch (err) {
                  button.disabled = false;
                  status.textContent = err.message || 'Could not finish the bank connection.';
                }
              },
              onExit: function (err) {
                button.disabled = false;
                status.textContent = err && err.display_message
                  ? err.display_message : 'Plaid Link closed.';
              }
            });
            handler.open();
          } catch (err) {
            button.disabled = false;
            status.textContent = err.message || 'Could not connect the bank.';
          }
        });
        if (syncButton) syncButton.addEventListener('click', async function () {
          syncButton.disabled = true;
          status.textContent = 'Syncing Plaid transactionsâ€¦';
          try {
            const syncResp = await fetch(
              '/plaid/sync/' + encodeURIComponent(syncButton.dataset.connectionId),
              {method: 'POST'}
            );
            const syncData = await syncResp.json();
            if (!syncResp.ok || !syncData.ok) {
              throw new Error(syncData.error || 'Could not sync Plaid transactions.');
            }
            const count = (syncData.added || []).length;
            status.textContent = 'Synced ' + count + ' new transaction' +
              (count === 1 ? '' : 's') + '.';
          } catch (err) {
            status.textContent = err.message || 'Could not sync Plaid transactions.';
          } finally {
            syncButton.disabled = false;
          }
        });
      }());
      </script>"""

    # SimpleFIN Bridge — deliberately imports banks.get_adapter("simplefin")
    # directly, like the Plaid section above, rather than relying on
    # banks.active_adapter(): this card must show SimpleFIN's OWN state
    # regardless of which bank is currently active (Mercury, Plaid, or
    # SimpleFIN itself), same reasoning as the Plaid card.
    simplefin_adapter = banks.get_adapter("simplefin")
    # Narrowly catch secrets_store.SecretsError only (a real, fixable
    # problem -- the OS credential-store key no longer matches what
    # encrypted this secret, with its own actionable remedy message)
    # rather than a bare `except Exception`, which would also swallow a
    # genuine bug and misreport it as "never set up". See
    # bank_credential_error above for the same reasoning applied to the
    # generic bank card -- and note that guard is what actually stops
    # THIS section from ever being reached at all when SimpleFIN is
    # active with a bad credential: the generic card's unguarded
    # is_configured() call used to raise first and 500 the whole page
    # before this code ever ran.
    simplefin_credential_error = False
    try:
        simplefin_on = simplefin_adapter.is_configured()
    except secrets_store.SecretsError:
        simplefin_on = False
        simplefin_credential_error = True
    simplefin_stage_value = setup_wizard.simplefin_stage()
    simplefin_done = simplefin_stage_value == setup_wizard.SIMPLEFIN_STAGE_DONE
    # Whether setup previously reached (at least) account mapping --
    # independent of simplefin_on, which only reflects whether the
    # CREDENTIAL is present right now. A stage this far along with a
    # credential that is entirely MISSING (not just failing a live
    # test) is a real, reachable state (the secret was deleted, or a
    # restored backup didn't include it) that would otherwise fall into
    # the "never set up" branch below with no way back to the paste
    # form except hand-typing ?reconnect=1 in the URL bar -- a genuine
    # dead-end loop the reviewer found: intro -> token -> "already
    # connected, continue to mapping" -> mapping can't reach SimpleFIN
    # -> back -> token -> repeat.
    simplefin_reached_mapping = (
        setup_wizard._simplefin_stage_index(simplefin_stage_value)
        >= setup_wizard._simplefin_stage_index(setup_wizard.SIMPLEFIN_STAGE_MAPPING))
    simplefin_reconnect_link = (
        '<a class="btn-link" href="/setup/simplefin/token?reconnect=1">'
        'Reconnect with a new token</a>')

    if simplefin_credential_error:
        simplefin_detail = (
            "SimpleFIN's stored credentials could not be read right "
            "now -- the encryption key in your system's credential "
            "store no longer matches. Reconnect with a new setup token "
            "to restore access.")
        simplefin_actions = simplefin_reconnect_link
    elif not simplefin_on:
        simplefin_detail = (
            "Read-only bank sync through SimpleFIN Bridge, an independent "
            "aggregator most banks and credit unions support. "
            "SimpleFIN Bridge costs $15/year, billed by SimpleFIN "
            "directly. Klearing credits that against your subscription.")
        simplefin_actions = (
            '<a class="save-btn" href="/setup/simplefin">Set up SimpleFIN</a>')
        if simplefin_reached_mapping:
            simplefin_detail += (
                " Setup previously reached account mapping, but the "
                "stored Access URL is missing now.")
            simplefin_actions += " " + simplefin_reconnect_link
    elif not simplefin_done:
        simplefin_detail = (
            "Setup started but not finished. Pick up where you left off "
            "— account mapping, cutover date, and opening balances are "
            "saved as you go.")
        simplefin_actions = (
            '<a class="save-btn" href="/setup/simplefin">Continue setup</a> '
            + simplefin_reconnect_link)
    else:
        # Total mapped-account count derives from the SAME structured
        # summary the warning below reads, rather than a second,
        # independently-re-derived db.list_bank_accounts() filter --
        # one source of truth for "which accounts are mapped", not two
        # copies of the same predicate that could silently drift apart.
        resolution = setup_wizard.simplefin_resolution_summary()
        count = (len(resolution["posted"]) + len(resolution["zero_resolved"])
                 + len(resolution["missing"]) + len(resolution["remapped"]))
        plural = "" if count == 1 else "s"
        start_date = db.get_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY) or "an unknown date"
        simplefin_detail = (
            f"Connected via SimpleFIN Bridge. Reading {count} "
            f"account{plural} since {escape(start_date)}.")
        # Persistent, distinct warnings. Two different gaps, both
        # previously invisible or visible only once on a since-vanished
        # redirect banner:
        #   - "missing": an account mapped (via "Edit account mapping"
        #     below) AFTER setup finished has no UI path to ever get an
        #     opening balance (Task 6's gap).
        #   - "remapped": REVIEWER-FOUND GAP. An account that WAS posted
        #     correctly, then had its mapping changed to a DIFFERENT
        #     Ledger account afterward -- the old posted entry is
        #     orphaned in the old account while new transactions sync
        #     into the new one. Strictly worse than "missing" (real
        #     money silently in the wrong place), and ledger_source_
        #     import_exists() alone can never distinguish it from a
        #     genuinely-fine "posted" account -- see
        #     simplefin_resolution_summary()'s own docstring.
        # Both must stay visible on every visit until the app grows a
        # real "resolve"/"move the entry" flow (out of scope here).
        missing = resolution["missing"]
        remapped = resolution["remapped"]
        warning_lines = []
        if missing:
            names = _format_account_name_list(
                [escape(row.get("name") or row["account_id"]) for row in missing])
            if len(missing) == 1:
                warning_lines.append(
                    f"1 account ({names}) is mapped but has no opening "
                    "balance recorded.")
            else:
                warning_lines.append(
                    f"{len(missing)} accounts ({names}) are mapped but "
                    "have no opening balance recorded.")
        if remapped:
            names = _format_account_name_list(
                [escape(row.get("name") or row["account_id"]) for row in remapped])
            if len(remapped) == 1:
                warning_lines.append(
                    f"1 account ({names}) has an opening balance posted "
                    "to a different account than where it's currently "
                    "mapped.")
            else:
                warning_lines.append(
                    f"{len(remapped)} accounts ({names}) have an opening "
                    "balance posted to a different account than where "
                    "each is currently mapped.")
        for warning_text in warning_lines:
            simplefin_detail += (
                '<div class="callout"><strong>Needs attention</strong> '
                f'{warning_text}</div>')
        simplefin_actions = f"""
      <form method="post" action="/connections/test/simplefin" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>
      <a class="btn-link" href="/setup/simplefin/accounts">Edit account mapping</a>
      {simplefin_reconnect_link}"""

    # Klaviyo
    klaviyo_on = klaviyo_client.is_configured()
    klaviyo_detail = (
        f"Watching list {escape(db.get_setting(klaviyo_client.LIST_ID_KEY) or '?')} "
        f"for leads needing a follow-up."
        if klaviyo_on else
        'Optional. Unlocks the <a href="/review?tab=followups">Follow-ups tab</a> '
        "(drafts for leads who never got a reply)."
    )
    klaviyo_actions = """
      <form method="post" action="/connections/test/klaviyo" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>
      <a class="btn-link" href="/setup/step/4">Connect / update</a>"""
    if klaviyo_on:
        klaviyo_actions += f"""
      <form method="post" action="/connections/disconnect/klaviyo"
            class="inline-form" {confirm_attrs('Disconnect Klaviyo? Existing drafts are kept.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""

    # GA4
    ga4_on = ga4_client.is_configured()
    events = ", ".join(ga4_client.conversion_events()) or "none configured"
    ga4_detail = (
        f"Property {escape(db.get_setting(ga4_client.PROPERTY_ID_KEY) or '?')} "
        f"&middot; conversion events: {escape(events)}"
        if ga4_on else
        "Optional and the fiddliest — a guided walkthrough is one click "
        'away, and skipping it costs you only the '
        '<a href="/insights">Insights tab</a>.'
    )
    ga4_actions = """
      <form method="post" action="/connections/test/ga4" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>
      <a class="btn-link" href="/setup/ga4?return_to=connections">Guided setup / update</a>"""
    if ga4_on:
        ga4_actions += f"""
      <form method="post" action="/connections/disconnect/ga4"
            class="inline-form" {confirm_attrs('Disconnect GA4? Stored snapshots are kept.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""
        ga4_actions += f"""
      <form method="post" action="/connections/ga4-events"
            class="field-row">
        <input type="text" name="events" placeholder="sign_up, purchase"
               value="{escape(', '.join(ga4_client.conversion_events()))}"
               class="grow">
        <button class="check" type="submit">Save conversion events</button>
      </form>"""

    # Email
    email_on = email_utils.is_configured()
    email_detail = (
        f"Sending from {escape(db.get_setting(email_utils.GMAIL_ADDRESS_KEY) or '?')}"
        + (f" &middot; notifying {escape(db.get_setting(email_utils.NOTIFY_EMAIL_KEY))}"
           if db.get_setting(email_utils.NOTIFY_EMAIL_KEY) else "")
        if email_on else
        "Recommended. Notifications, digests, alerts — and how drafted "
        "follow-ups and reminders actually send."
    )
    email_actions = """
      <form method="post" action="/connections/test/email" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>
      <a class="btn-link" href="/setup/step/6">Connect / update</a>"""
    if email_on:
        email_actions += f"""
      <form method="post" action="/connections/disconnect/email"
            class="inline-form" {confirm_attrs('Disconnect email? Nothing will be sent until it is reconnected.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""

    # Stripe (optional, read-only)
    stripe_on = stripe_client.is_configured()
    if stripe_on:
        stripe_name = db.get_setting("stripe_account_name") or "Stripe account"
        mode_tag = ('<span class="jump-tag">TEST mode — not real money</span>'
                    if stripe_client.is_test_mode() else "")
        stripe_detail = (
            f'"{escape(stripe_name)}" {mode_tag} &middot; read-only: card '
            f'revenue on <a href="/growth">Growth</a>, payouts and '
            f'in-transit on <a href="/reconcile">Reconcile</a>, processing '
            f'fees on <a href="/vendors">Vendors</a>.')
    else:
        stripe_detail = (
            "Optional. Adds card revenue, payouts, in-transit amounts, and "
            "processing fees to Reconcile, Vendors, and Growth. Read-only.")
    stripe_actions = """
      <form method="post" action="/connections/test/stripe" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>"""
    if stripe_on:
        stripe_actions += f"""
      <form method="post" action="/connections/disconnect/stripe"
            class="inline-form" {confirm_attrs('Disconnect Stripe? Nothing at Stripe changes; Klearing just stops reading it.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""
    stripe_actions += """
      <form method="post" action="/connections/stripe"
            class="field-row">
        <input type="password" name="secret_key" placeholder="sk_live_..."
               autocomplete="off" class="grow">
        <button class="check" type="submit">Connect / update key</button>
      </form>"""

    # Shopify (optional, read-only; all page views read the local sync).
    shopify_on = shopify_client.is_configured()
    shopify_detail = (f'Local orders, products, and customers from <a href="/shopify">Shopify</a>. '
                      f'Last sync: {escape((db.get_setting("shopify_last_synced") or "not yet").replace("T", " ")[:16])}.'
                      if shopify_on else
                      f'Optional. Adds locally synced revenue, product, customer, and fulfillment health. '
                      f'Connect through Shopify in your browser after configuring the app credentials in '
                      f'<code>{escape(shopify_client.credential_status()["path"])}</code>.')
    shopify_actions = """
      <form method="post" action="/connections/test/shopify" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>"""
    if shopify_on:
        shopify_actions += f'''<form method="post" action="/connections/disconnect/shopify" class="inline-form"
          {confirm_attrs('Disconnect Shopify? Local synced history stays available.', busy_label='Disconnecting...')}>
          <button class="deny" type="submit">Disconnect</button></form>'''
    shopify_actions += """
      <form method="post" action="/connections/shopify" class="field-row">
        <input type="text" name="store" placeholder="store.myshopify.com" autocomplete="off" class="grow">
        <button class="check" type="submit">Connect / update</button>
      </form>"""

    # Gusto (optional, read-only)
    gusto_on = gusto_client.is_configured()
    if gusto_on:
        try:
            facts = gusto_client.company_facts()
        except Exception:
            facts = {"name": db.get_setting(gusto_client.COMPANY_NAME_KEY)
                     or "Gusto company",
                     "pay_schedule": None, "last_payroll": None}
        parts = [f'"{escape(facts["name"])}"']
        if facts.get("pay_schedule"):
            parts.append(f'paid {escape(str(facts["pay_schedule"]))}')
        if facts.get("last_payroll"):
            parts.append(f'last payroll {escape(facts["last_payroll"])}')
        gusto_detail = (
            " &middot; ".join(parts)
            + ' &middot; read-only: payroll on <a href="/summary">Summary'
              '</a> and <a href="/growth">Growth</a>, contractor payments '
              'in <a href="/tools/contractors">1099 contractors</a>.')
    else:
        gusto_detail = (
            "Optional. Adds payroll to Summary and Growth, and contractor "
            "payments to the 1099 tracker. Read-only.")
    gusto_actions = """
      <form method="post" action="/connections/test/gusto" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>"""
    if gusto_on:
        gusto_actions += f"""
      <form method="post" action="/connections/disconnect/gusto"
            class="inline-form" {confirm_attrs('Disconnect Gusto? Nothing at Gusto changes; Klearing just stops reading it.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""
    gusto_actions += """
      <form method="post" action="/connections/gusto"
            class="field-row">
        <input type="password" name="api_key" placeholder="Gusto API token"
               autocomplete="off" class="grow">
        <button class="check" type="submit">Connect / update token</button>
      </form>"""

    # Gmail read-only (optional) — deliberately separate from the
    # Email (Gmail) SENDING card above: different credential, different
    # direction, and the trust line below is the point.
    gmailr_on = gmail_reader.is_configured()
    if gmailr_on:
        gmailr_address = db.get_setting(gmail_reader.ADDRESS_KEY) or "?"
        gmailr_sync = db.get_setting(gmail_reader.LAST_SYNC_KEY)
        sync_text = (f' &middot; last sync <span class="mono">'
                     f'{escape(gmailr_sync.replace("T", " ")[:16])}</span>'
                     if gmailr_sync else "")
        gmailr_detail = (
            f"Reading {escape(gmailr_address)}{sync_text} &middot; payment "
            f'confirmations on <a href="/review?tab=invoices">invoice reminders</a>, replies '
            f'on <a href="/review?tab=followups">Follow-ups</a>, receipts on '
            f'<a href="/vendors">Vendors</a>.')
    else:
        gmailr_detail = (
            "Optional. Spots payment-confirmation emails, lead replies, "
            "and vendor receipts. Uses a separate OAuth token with the "
            "gmail.readonly scope only.")
    gmailr_detail += ('<div class="conn-trust">Read-only — Klearing never '
                      "sends from or modifies your Gmail.</div>")
    gmailr_actions = """
      <form method="post" action="/connections/test/gmail_reader" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>"""
    if gmailr_on:
        gmailr_actions += f"""
      <form method="post" action="/connections/disconnect/gmail_reader"
            class="inline-form" {confirm_attrs('Disconnect Gmail (read-only)? The token is deleted from this computer.', busy_label='Disconnecting…')}>
        <button class="deny" type="submit">Disconnect</button></form>"""
    gmailr_actions += """
      <form method="post" action="/connections/gmail-reader"
            class="field-row">
        <input type="password" name="token" autocomplete="off"
               placeholder="Paste the read-only OAuth token JSON"
               class="grow">
        <button class="check" type="submit">Connect / update token</button>
      </form>"""

    # Slack (incoming webhook only; no OAuth or bot token)
    slack_on = slack_client.is_configured()
    slack_digest = slack_client.daily_digest_enabled()
    slack_alerts = slack_client.instant_alerts_enabled()
    slack_detail = (
        "Sends only the summaries and alerts you enable. Raw transactions, "
        "account numbers, and full vendor lists are never sent."
        if slack_on else
        f'Optional. Paste an incoming webhook URL. See <a href="{slack_client.DOCS_URL}" '
        'target="_blank" rel="noreferrer">Slack webhook docs</a>.')
    if slack_on:
        slack_detail += (f' If a test fails, see <a href="{slack_client.DOCS_URL}" '
                         'target="_blank" rel="noreferrer">Slack webhook docs</a>.')
    slack_actions = """
      <form method="post" action="/connections/test/slack" class="inline-form">
        <button class="check" type="submit">Send test message</button></form>"""
    if slack_on:
        slack_actions += f"""
      <form method="post" action="/connections/disconnect/slack"
            class="inline-form" {confirm_attrs('Disconnect Slack? No more messages will be sent.', busy_label='Disconnecting…')}
      ><button class="deny" type="submit">Disconnect</button></form>"""
    slack_actions += f"""
      <form method="post" action="/connections/slack"
            class="field-row">
        <input type="password" name="webhook" autocomplete="off"
               placeholder="Paste incoming webhook URL"
               class="grow">
        <label class="field-row tight">
          <input type="checkbox" name="daily_digest" value="1"{' checked' if slack_digest else ''}>
          Daily digest after the daily check
        </label>
        <label class="field-row tight">
          <input type="checkbox" name="instant_alerts" value="1"{' checked' if slack_alerts else ''}>
          Instant alerts for new issues
        </label>
        <button class="check" type="submit">Connect / save settings</button>
      </form>"""

    # Notion (internal integration token + parent page)
    notion_on = notion_client.is_configured()
    notion_detail = (
        f'Exports close packages and maintains the <span class="mono">'
        f'{escape(notion_client.SNAPSHOT_TITLE)}</span> page.'
        if notion_on else
        "Optional. Share a parent page with your Notion integration before "
        "testing the connection.")
    notion_actions = """
      <form method="post" action="/connections/test/notion" class="inline-form">
        <button class="check" type="submit">Re-test</button></form>"""
    if notion_on:
        notion_actions += f"""
      <form method="post" action="/connections/disconnect/notion"
            class="inline-form" {confirm_attrs('Disconnect Notion? Existing pages are kept.', busy_label='Disconnecting…')}
      ><button class="deny" type="submit">Disconnect</button></form>"""
    notion_actions += """
      <form method="post" action="/connections/notion"
            class="field-row">
        <input type="password" name="token" autocomplete="off"
               placeholder="Paste Notion integration token"
               class="grow">
        <input type="text" name="parent_page_id" autocomplete="off"
               placeholder="Parent page ID"
               class="grow">
        <button class="check" type="submit">Connect / update</button>
      </form>"""

    keyfile_note = ""
    if secrets_store.used_fallback_keyfile():
        keyfile_note = """
        <div class="callout"><strong>Credential store fallback in use</strong>
          No OS credential store was found, so the encryption key lives in
          a protected file next to the database. Credentials are still
          encrypted at rest, but OS-level protection is weaker here.</div>"""

    content = f"""
    <div class="sub">Every credential is encrypted on this computer; the
      encryption key lives in your operating system's credential store
      (Credential Manager / Keychain), unlocked by your OS login. Nothing
      is ever sent anywhere except to the service it belongs to.</div>
    {keyfile_note}
    {_conn_group("Banking")}
    {_conn_card(f"Bank — {bank_name}", bank_on, bank_detail, bank_actions, service="bank") if show_generic_bank_card else ""}
    {_conn_card("Bank — Plaid (Phase 1)", plaid_on, plaid_detail, plaid_actions, service="plaid")}
    {_conn_card("Bank — SimpleFIN Bridge", simplefin_on, simplefin_detail, simplefin_actions, service="simplefin")}
    {_conn_group("Payments and payroll")}
    {_conn_card("Stripe (card payments)", stripe_on, stripe_detail, stripe_actions, service="stripe")}
    {_conn_card("Shopify (storefront)", shopify_on, shopify_detail, shopify_actions, service="shopify")}
    {_conn_card("Gusto (payroll)", gusto_on, gusto_detail, gusto_actions, service="gusto")}
    {_conn_group("Communication")}
    {_conn_card("Klaviyo (lead follow-ups)", klaviyo_on, klaviyo_detail, klaviyo_actions, service="klaviyo")}
    {_conn_card("Email (Gmail)", email_on, email_detail, email_actions, service="email")}
    {_conn_card("Gmail (read-only)", gmailr_on, gmailr_detail, gmailr_actions, service="gmail_reader")}
    {_conn_group("Analytics")}
    {_conn_card("Google Analytics (GA4)", ga4_on, ga4_detail, ga4_actions, service="ga4")}
    {_conn_group("Notifications and workspace exports")}
    {_conn_card("Slack (notifications)", slack_on, slack_detail, slack_actions, service="slack")}
    {_conn_card("Notion (exports)", notion_on, notion_detail, notion_actions, service="notion")}"""
    return render_shell("Settings", "", _settings_nav("connections") + content,
                        message, error)


@app.post("/connections/test/{service}")
def connections_test(service: str):
    """Re-test a STORED credential (no re-entry) and report plainly."""
    try:
        if service == "bank":
            adapter = banks.active_adapter()
            credentials = adapter.stored_credentials() if adapter else None
            if not credentials:
                return _redirect_connections(err="No bank is connected yet.")
            result = adapter.test_connection(credentials)
        elif service == "simplefin":
            # Deliberately banks.get_adapter("simplefin"), not
            # banks.active_adapter() — the SimpleFIN card must be
            # testable on its own even when a different bank (Mercury,
            # Plaid) is the currently active one.
            adapter = banks.get_adapter("simplefin")
            credentials = adapter.stored_credentials()
            if not credentials:
                return _redirect_connections(err="SimpleFIN isn't connected yet.")
            result = adapter.test_connection(credentials)
        elif service == "klaviyo":
            config = klaviyo_client.load_config()
            if config is None:
                return _redirect_connections(err="Klaviyo isn't connected yet.")
            result = klaviyo_client.test_connection(*config)
        elif service == "ga4":
            raw = secrets_store.get_secret_bytes(ga4_client.SERVICE_ACCOUNT_SECRET)
            property_id = db.get_setting(ga4_client.PROPERTY_ID_KEY)
            if not raw or not property_id:
                return _redirect_connections(err="GA4 isn't connected yet.")
            result = ga4_client.test_connection(raw, property_id)
        elif service == "email":
            credentials = email_utils.email_credentials()
            if credentials is None:
                return _redirect_connections(err="Email isn't connected yet.")
            result = email_utils.test_connection(*credentials)
        elif service == "stripe":
            key = stripe_client.get_key()
            if not key:
                return _redirect_connections(err="Stripe isn't connected yet.")
            result = stripe_client.test_connection(key)
            if result.ok:
                try:
                    db.set_setting("stripe_account_name",
                                   stripe_client.account_display_name())
                except Exception:
                    pass
        elif service == "gusto":
            key = gusto_client.get_key()
            if not key:
                return _redirect_connections(err="Gusto isn't connected yet.")
            result = gusto_client.test_connection(key)
        elif service == "gmail_reader":
            raw = secrets_store.get_secret(gmail_reader.SECRET_NAME)
            if not raw:
                return _redirect_connections(
                    err="Gmail (read-only) isn't connected yet.")
            result = gmail_reader.test_connection(raw)
        elif service == "slack":
            webhook = slack_client.get_webhook()
            if not webhook:
                return _redirect_connections(err="Slack isn't connected yet.")
            result = slack_client.test_connection(webhook)
        elif service == "notion":
            token = notion_client.get_token()
            parent_page_id = notion_client.get_parent_page_id()
            if not token or not parent_page_id:
                return _redirect_connections(err="Notion isn't connected yet.")
            result = notion_client.test_connection(token, parent_page_id)
        elif service == "shopify":
            token, store = shopify_client.get_token(), shopify_client.get_store()
            if not token or not store:
                return _redirect_connections(err="Shopify isn't connected yet.")
            result = shopify_client.test_connection(token, store)
        else:
            return _redirect_connections(err="Unknown service.")
    except secrets_store.SecretsError as exc:
        return _redirect_connections(err=str(exc))

    # The check ran (pass or fail) — the stamp records when, the outcome
    # keeps a failed test visible on the card across page loads (the
    # banner below only lives for one redirect).
    conn_mark_tested(service, ok=result.ok, detail=result.message)
    if result.ok:
        return _redirect_connections(msg=result.message)
    text = result.message + (f" Hint: {result.hint}" if result.hint else "")
    return _redirect_connections(err=text)


@app.post("/connections/disconnect/{service}")
def connections_disconnect(service: str):
    if service == "klaviyo":
        secrets_store.delete_secret(klaviyo_client.PRIVATE_KEY_SECRET)
        db.set_setting(klaviyo_client.LIST_ID_KEY, "")
    elif service == "ga4":
        secrets_store.delete_secret(ga4_client.SERVICE_ACCOUNT_SECRET)
        db.set_setting(ga4_client.PROPERTY_ID_KEY, "")
        ga4_client.reset_cache()
    elif service == "email":
        secrets_store.delete_secret(email_utils.APP_PASSWORD_SECRET)
        db.set_setting(email_utils.GMAIL_ADDRESS_KEY, "")
    elif service == "stripe":
        secrets_store.delete_secret(stripe_client.SECRET_NAME)
        db.set_setting("stripe_account_name", "")
        stripe_client.reset_cache()
    elif service == "gusto":
        secrets_store.delete_secret(gusto_client.SECRET_NAME)
        db.set_setting(gusto_client.COMPANY_ID_KEY, "")
        db.set_setting(gusto_client.COMPANY_NAME_KEY, "")
        gusto_client.reset_cache()
    elif service == "gmail_reader":
        secrets_store.delete_secret(gmail_reader.SECRET_NAME)
        db.set_setting(gmail_reader.ADDRESS_KEY, "")
        gmail_reader.reset_cache()
    elif service == "slack":
        secrets_store.delete_secret(slack_client.SECRET_NAME)
        db.set_setting(slack_client.DAILY_DIGEST_KEY, "")
        db.set_setting(slack_client.INSTANT_ALERTS_KEY, "")
    elif service == "notion":
        secrets_store.delete_secret(notion_client.SECRET_NAME)
        db.set_setting(notion_client.PARENT_PAGE_ID_KEY, "")
        db.set_setting(notion_client.SNAPSHOT_PAGE_ID_KEY, "")
    elif service == "shopify":
        secrets_store.delete_secret(shopify_client.SECRET_NAME)
        db.set_setting(shopify_client.STORE_KEY, "")
    elif service == "plaid":
        secrets_store.delete_secret(plaid_client.TOKEN_SECRET_NAME)
        secrets_store.delete_secret(plaid_client.ITEM_ID_SECRET_NAME)
        secrets_store.delete_secret(plaid_client.SYNC_CURSOR_SECRET_NAME)
    else:
        return _redirect_connections(err="The bank connection is required — "
                                         "update it instead of disconnecting.")
    return _redirect_connections(msg=f"{service.title()} disconnected.")


@app.post("/connections/gusto")
def connections_gusto(api_key: str = Form("")):
    """Store (or replace) the Gusto API token — validated live before
    anything is saved (the test also caches the company id/name)."""
    key = (api_key or "").strip()
    format_error = gusto_client.validate_key_format(key)
    if format_error:
        return _redirect_connections(err=format_error)
    result = gusto_client.test_connection(key)
    if not result.ok:
        text = result.message + (f" Hint: {result.hint}" if result.hint
                                 else "")
        return _redirect_connections(err=text)
    secrets_store.set_secret(gusto_client.SECRET_NAME, key)
    gusto_client.reset_cache()
    conn_mark_tested("gusto")
    return _redirect_connections(msg=result.message)


@app.post("/connections/gmail-reader")
def connections_gmail_reader(token: str = Form("")):
    """Store (or replace) the read-only Gmail OAuth token — validated
    live before anything is kept (test_connection swaps the token in,
    and restores the previous one on failure)."""
    raw = (token or "").strip()
    format_error = gmail_reader.validate_token_format(raw)
    if format_error:
        return _redirect_connections(err=format_error)
    result = gmail_reader.test_connection(raw)
    if not result.ok:
        text = result.message + (f" Hint: {result.hint}" if result.hint
                                 else "")
        return _redirect_connections(err=text)
    gmail_reader.reset_cache()
    conn_mark_tested("gmail_reader")
    return _redirect_connections(msg=result.message)


@app.post("/connections/stripe")
def connections_stripe(secret_key: str = Form("")):
    """Store (or replace) the Stripe secret key — validated live before
    anything is saved, so a bad paste never half-connects."""
    key = (secret_key or "").strip()
    format_error = stripe_client.validate_key_format(key)
    if format_error:
        return _redirect_connections(err=format_error)
    result = stripe_client.test_connection(key)
    if not result.ok:
        text = result.message + (f" Hint: {result.hint}" if result.hint
                                 else "")
        return _redirect_connections(err=text)
    secrets_store.set_secret(stripe_client.SECRET_NAME, key)
    try:
        db.set_setting("stripe_account_name",
                       stripe_client.account_display_name())
    except Exception:
        pass
    stripe_client.reset_cache()
    conn_mark_tested("stripe")
    return _redirect_connections(msg=result.message)


@app.post("/connections/slack")
def connections_slack(webhook: str = Form(""), daily_digest: bool = Form(False),
                      instant_alerts: bool = Form(False)):
    candidate = (webhook or "").strip() or slack_client.get_webhook()
    format_error = slack_client.validate_webhook_format(candidate)
    if format_error:
        return _redirect_connections(err=format_error)
    result = slack_client.test_connection(candidate)
    if not result.ok:
        text = result.message + (f" Hint: {result.hint}" if result.hint else "")
        return _redirect_connections(err=text)
    secrets_store.set_secret(slack_client.SECRET_NAME, candidate)
    slack_client.set_preferences(daily_digest, instant_alerts)
    conn_mark_tested("slack")
    return _redirect_connections(msg=result.message)


@app.post("/connections/notion")
def connections_notion(token: str = Form(""), parent_page_id: str = Form("")):
    token = (token or "").strip() or notion_client.get_token()
    parent_page_id = (parent_page_id or "").strip() or notion_client.get_parent_page_id()
    format_error = notion_client.validate_config(token, parent_page_id)
    if format_error:
        return _redirect_connections(err=format_error)
    result = notion_client.test_connection(token, parent_page_id)
    if not result.ok:
        text = result.message + (f" Hint: {result.hint}" if result.hint else "")
        return _redirect_connections(err=text)
    secrets_store.set_secret(notion_client.SECRET_NAME, token)
    db.set_setting(notion_client.PARENT_PAGE_ID_KEY, parent_page_id)
    conn_mark_tested("notion")
    return _redirect_connections(msg=result.message)


@app.post("/connections/shopify")
def connections_shopify(store: str = Form("")):
    store = (store or "").strip() or shopify_client.get_store()
    error = shopify_client.validate_store(store)
    if error:
        return _redirect_connections(err=error)
    store = store.lower().replace("https://", "").rstrip("/")
    token, error = shopify_client.authorize(store)
    if error:
        return _redirect_connections(err=error)
    result = shopify_client.test_connection(token, store)
    if not result.ok:
        return _redirect_connections(err=result.message + (f" Hint: {result.hint}" if result.hint else ""))
    secrets_store.set_secret(shopify_client.SECRET_NAME, token)
    db.set_setting(shopify_client.STORE_KEY, store)
    conn_mark_tested("shopify")
    return _redirect_connections(msg=result.message)


@app.post("/connections/business/add")
def connections_business_add(business_name: str = Form("")):
    try:
        record = business_registry.upsert_business(business_name)
    except ValueError as exc:
        return _redirect_settings(err=str(exc))
    return _redirect_settings(
        msg=f'Added "{record["name"]}". Use the header dropdown to switch to it.')


@app.post("/connections/business/remove")
def connections_business_remove(business_id: str = Form("")):
    name = business_registry.business_name_by_id(business_id) or "that business"
    try:
        business_registry.remove_business(business_id)
    except RuntimeError as exc:
        return _redirect_settings(err=str(exc))
    return _redirect_settings(msg=f'Removed "{name}".')


@app.post("/connections/ga4-events")
def connections_ga4_events(events: str = Form("")):
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    db.set_setting(ga4_client.CONVERSION_EVENTS_KEY, json.dumps(event_list))
    return _redirect_connections(
        msg=f"Tracking {len(event_list)} conversion event(s) from the next "
            f"daily snapshot on."
    )


# ---------------------------------------------------------------- Automation


def render_automation_page(message=None, error=None):
    jobs = scheduler.status()
    all_on = all(j["installed"] for j in jobs.values())
    rows = ""
    for key, job in jobs.items():
        state = ('<span class="tag ok">scheduled</span>' if job["installed"]
                 else '<span class="tag off">off</span>')
        # Next trigger time, read-only from the OS scheduler. Only the
        # time-triggered jobs — the app job fires at login, and a clock
        # time for it would be an invented number.
        next_line = ""
        if key in ("daily", "weekly"):
            next_run = scheduler.next_run_time(key)
            if next_run:
                next_line = (f'<div class="next-run">Next run: '
                             f'<span class="mono">{escape(next_run)}</span>'
                             f"</div>")
            else:
                next_line = ('<div class="next-run none">Schedule not '
                             "found</div>")
        rows += (f'<li><span class="acct-name">{escape(job["title"])}</span>'
                 f'{state}<span class="acct-bal">{escape(job["description"])}</span>'
                 f"{next_line}</li>")

    # Slack digest rides on the existing daily check rather than creating a
    # second OS job. Show that shared next run so the schedule is honest.
    slack_state = ('<span class="tag ok">scheduled</span>'
                   if slack_client.is_configured()
                   and slack_client.daily_digest_enabled()
                   else '<span class="tag off">off</span>')
    slack_next = scheduler.next_run_time("daily") if scheduler.is_supported() else None
    slack_next_line = (f'<div class="next-run">Next run: '
                       f'<span class="mono">{escape(slack_next)}</span></div>'
                       if slack_next else
                       '<div class="next-run none">Uses the daily check schedule</div>')
    rows += (f'<li><span class="acct-name">Slack Daily Digest</span>{slack_state}'
             '<span class="acct-bal">Sends the enabled summary after the daily check.</span>'
             f'{slack_next_line}</li>')

    heartbeat = escape(
        db.get_meta(check_bank.LAST_SUCCESS_KEY) or "not yet recorded"
    )

    if scheduler.is_supported():
        toggle = (f"""
        <form method="post" action="/automation/uninstall"
              {confirm_attrs('Turn off all scheduled automation?')}>
          <button class="deny" type="submit">Turn automation off</button>
        </form>""" if all_on else """
        <form method="post" action="/automation/install">
          <button class="save-btn" type="submit">Turn automation on</button>
        </form>""")
    else:
        toggle = ('<div class="none-note">Automatic scheduling isn\'t wired '
                  "up for this operating system — run the checks manually "
                  "below, or via cron.</div>")

    mac_note = ""
    if sys.platform == "darwin":
        mac_note = """
        <div class="callout info">macOS shows a "background items added"
          notification the first time — that's this, and it's normal. You
          can see the items under System Settings &rarr; General &rarr;
          Login Items.</div>"""

    content = f"""
    <div class="sub">Automation is what makes {APP_NAME} hands-off: the app
      starts at login, the bank is checked every morning at 8 (or as soon
      as the computer wakes), and the digest goes out Monday mornings. All
      of it runs quietly in the background as your user account — no
      terminal, no extra passwords.</div>
    {mac_note}
    <div class="section">
      <div class="section-head"><h2>Scheduled jobs</h2></div>
      <ul class="accounts jobs mb-5">{rows}</ul>
      {toggle}
    </div>
    <div class="section">
      <div class="section-head"><h2>Run now</h2></div>
      <div class="hint mb-3">Kick off a run without
        waiting for the schedule. Output lands on the
        <a href="/settings?tab=logs">Logs page</a>.</div>
      <div class="wizard-actions">
        <form method="post" action="/automation/run/daily" class="inline-form">
          <button class="check" type="submit">Run daily check now</button></form>
        <form method="post" action="/automation/run/weekly" class="inline-form">
          <button class="check" type="submit">Send weekly digest now</button></form>
      </div>
    </div>
    <div class="field">
      <label class="q">Daily check heartbeat</label>
      <div class="hint">Stamped after a fully clean run. A stale date means
        something has been failing — details on the
        <a href="/settings?tab=logs&file=error">Logs page (error.log)</a>
        and in a failure alert email.</div>
      <div>Last succeeded: <strong>{heartbeat}</strong></div>
    </div>"""
    return render_shell("Settings", "", _settings_nav("automation") + content,
                        message, error)


@app.post("/automation/install")
def automation_install():
    try:
        scheduler.install_all()
    except Exception as exc:
        return _redirect_automation(err=f"Could not schedule: {exc}")
    return _redirect_automation(msg="Automation is on: app at login, daily "
                                    "check at 8:00 AM, digest Mondays.")


@app.post("/automation/uninstall")
def automation_uninstall():
    scheduler.uninstall_all()
    return _redirect_automation(msg="Automation turned off. Everything "
                                    "still works manually.")


@app.post("/automation/run/{job}")
def automation_run(job: str):
    if job not in ("daily", "weekly"):
        return _redirect_automation(err="Unknown job.")
    try:
        scheduler.run_job_now(job)
    except Exception as exc:
        return _redirect_automation(err=f"Could not start the run: {exc}")
    label = "Daily check" if job == "daily" else "Weekly digest"
    return _redirect_automation(
        msg=f"{label} started in the background — watch the Logs page "
            f"({'daily' if job == 'daily' else 'weekly'}.log)."
    )


# ---------------------------------------------------------------- Settings


def get_bank_accounts():
    """Every account the active bank adapter returns, or None if the
    bank can't be reached. We list them all and let the user choose
    which count as business cash."""
    try:
        adapter = banks.active_adapter()
        if adapter is None:
            return None
        credentials = adapter.stored_credentials()
        if credentials is None:
            return None
        return adapter.list_accounts(credentials)
    except Exception:
        return None


def _settings_select_options(labels, current):
    """Render an allowlisted select without hiding an old invalid value."""
    options = ""
    if current not in labels:
        options += (f'<option value="{escape(str(current))}" selected>'
                    f'Unsupported saved value: {escape(str(current))}</option>')
    for value, label in labels.items():
        selected = " selected" if value == current else ""
        options += (f'<option value="{escape(value)}"{selected}>'
                    f'{escape(label)}</option>')
    return options


def _tax_settings_fields():
    """Settings controls for assumptions already used by live projections."""
    settings = tax_settings.get()
    filing_options = _settings_select_options(
        tax_settings.FILING_STATUS_LABELS, settings.get("filing_status"))
    business_options = _settings_select_options(
        tax_settings.BUSINESS_TYPE_LABELS, settings.get("business_type"))
    state_options = _settings_select_options(
        tax_settings.STATE_LABELS, settings.get("state"))
    stored_other_income = settings.get("estimated_other_income")
    income_note = ""
    try:
        stored_other_income_cents = int(stored_other_income or 0)
        other_income = f'{Decimal(stored_other_income_cents) / 100:.2f}'
        if stored_other_income_cents < 0:
            income_note = """
            <div class="callout">
              <strong>Saved other income is unsupported</strong>
              The live projection treats a negative saved value as zero.
              Enter zero or a positive annual amount before saving.
            </div>"""
    except (TypeError, ValueError, InvalidOperation):
        other_income = "0.00"
        income_note = f"""
        <div class="callout">
          <strong>Saved other income is not a valid amount</strong>
          The database currently says
          <span class="mono">{escape(str(stored_other_income))}</span>.
          Enter a valid annual amount before saving.
        </div>"""

    stored_year = settings.get("tax_year")
    year_note = ""
    if str(stored_year) != str(tax_projection.TAX_YEAR):
        year_note = f"""
        <div class="callout">
          <strong>Saved tax year is unsupported</strong>
          The database currently says <span class="mono">{escape(str(stored_year))}</span>.
          Saving this form will move the live projection to the supported
          <span class="mono">{tax_projection.TAX_YEAR}</span> tax year.
        </div>"""

    return f"""
      <div class="section mt-6">
        <div class="section-head"><h2>Tax projection assumptions</h2></div>
        <div class="hint mb-4 measure-prose">These settings change the live
          federal and state estimates in Taxes. They are calculation inputs,
          not profile details saved for a future feature.</div>
        {year_note}
        <div class="field">
          <label class="q" for="tax_business_type">Business type</label>
          <select id="tax_business_type" name="tax_business_type" class="w-lg">
            {business_options}
          </select>
          <div class="hint measure-prose">Sole proprietorships and
            single-member LLCs receive the supported self-employment-tax
            estimate. S corporations and partnerships are marked incomplete
            for professional review; Klearing does not invent payroll or K-1
            treatment.</div>
        </div>
        <div class="field">
          <label class="q" for="tax_filing_status">Federal filing status</label>
          <select id="tax_filing_status" name="tax_filing_status" class="w-lg">
            {filing_options}
          </select>
          <div class="hint measure-prose">Changes the federal tax brackets,
            standard deduction, and Additional Medicare threshold used by the
            live estimate.</div>
        </div>
        <div class="field">
          <label class="q" for="tax_state">State</label>
          <select id="tax_state" name="tax_state" class="w-lg">
            {state_options}
          </select>
          <div class="hint measure-prose">State tax is currently calculated
            only for Iowa. Choosing another state leaves the state estimate
            unavailable instead of guessing.</div>
        </div>
        <div class="field">
          <label class="q" for="tax_other_income">Estimated other annual income</label>
          <div class="hint measure-prose">Income outside this business Ledger.
            The projection treats it as ordinary taxable income, not wages or
            self-employment income.</div>
          <div class="prefix-input">
            <span class="prefix">$</span>
            <input type="number" step="0.01" min="0" id="tax_other_income"
                   name="tax_other_income" value="{other_income}" class="mono">
          </div>
          {income_note}
        </div>
        <div class="field">
          <label class="q" for="tax_year">Tax year</label>
          <input type="number" id="tax_year" value="{tax_projection.TAX_YEAR}"
                 class="mono" readonly aria-describedby="tax-year-hint">
          <div class="hint measure-prose" id="tax-year-hint">The calculator
            currently supports {tax_projection.TAX_YEAR} only, so this value
            is visible but cannot be changed to an unsupported year.</div>
        </div>
      </div>"""


def render_settings_page(accounts, message=None, error=None):
    stored_balance = db.get_setting(cash_alert.BALANCE_THRESHOLD_KEY)
    stored_runway = db.get_setting(cash_alert.RUNWAY_DAYS_KEY)
    stored_jump = db.get_setting(VENDOR_JUMP_KEY)
    balance_value = "" if stored_balance is None else escape(stored_balance)
    runway_value = "14" if stored_runway is None else escape(stored_runway)
    vendor_jump_value = (
        f"{DEFAULT_VENDOR_JUMP_PCT:.0f}" if not (stored_jump or "").strip()
        else escape(stored_jump)
    )

    config = cash_alert.get_config()
    selected_ids = set(config["cash_account_ids"])

    # Business-health-score weights (one number per factor).
    weights = health.get_weights()
    weight_rows = ""
    for key in health.FACTORS:
        weight_rows += f"""
        <label class="inline-chip">
          {escape(health.FACTOR_LABELS[key])}
          <input type="number" step="1" min="0" class="w-xs"
                 name="{health.WEIGHT_KEY[key]}"
                 value="{weights[key]:.0f}"></label>"""
    health_weights_html = f"""
      <div class="field">
        <label class="q">Business health score weights</label>
        <div class="hint">How much each factor counts toward the composite
          score on the <a href="/">Home tab</a>. Any factor with no data yet
          is skipped and its weight ignored. Set a weight to 0 to drop a
          factor entirely.</div>
        {weight_rows}
      </div>"""

    adapter = banks.active_adapter()
    bank_name = adapter.display_name if adapter else "your bank"

    if accounts is None:
        accounts_html = (
            f'<div class="none-note">Could not reach {escape(bank_name)} to '
            'list accounts. Check the <a href="/settings?tab=connections">connection</a> '
            "and reload.</div>"
        )
    elif not accounts:
        accounts_html = f'<div class="none-note">{escape(bank_name)} returned no accounts.</div>'
    else:
        rows = ""
        for acct in accounts:
            checked = " checked" if acct["id"] in selected_ids else ""
            # The label wraps checkbox + name so the whole row toggles,
            # not just the 16px box.
            rows += f"""
            <li>
              <label class="acct-row">
                <input type="checkbox" name="cash_accounts" value="{escape(acct["id"])}"
                       id="acct-{escape(acct["id"])}"{checked}>
                <span class="acct-name">{escape(acct["name"])}</span>
              </label>
              <span class="acct-bal">{money(acct["balance"])}</span>
            </li>"""
        accounts_html = f'<ul class="accounts">{rows}</ul>'

    active = (config["balance_threshold"] is not None
              or config["runway_days"] is not None)
    status = (
        "Cash alert is active." if active
        else "Cash alert is inactive. Set a threshold below and Save to turn it on."
    )

    business_value = escape(db.get_setting("business_name") or "")
    sender_value = escape(db.get_setting(email_utils.SENDER_NAME_KEY) or "")
    auto_categorize = db.get_setting("auto_categorize_enabled") != "0"
    auto_categorize_checked = " checked" if auto_categorize else ""

    current_basis = ledger.accounting_basis()
    accounting_basis_options = "".join(
        f'<option value="{value}"'
        f'{" selected" if value == current_basis else ""}>{label}</option>'
        for value, label in (("accrual", "Accrual — count income when invoiced"),
                             ("cash", "Cash — count income when paid")))

    # Test-alert section: a real send through the same transport the cash
    # alerts and daily notifications use. When email isn't connected, the
    # button would be a guaranteed no-op — show the calm pointer instead.
    if email_utils.is_configured():
        recipient = ((db.get_setting(email_utils.NOTIFY_EMAIL_KEY) or "")
                     .strip()
                     or (db.get_setting(email_utils.GMAIL_ADDRESS_KEY) or "")
                     .strip())
        test_alert_html = f"""
    <div class="section mt-6">
      <div class="section-head"><h2>Alert email</h2></div>
      <div class="hint mb-3">Sends one real test
        email through the same path cash alerts and daily notifications
        use, so you can confirm the whole chain works. Goes to
        {escape(recipient)}.</div>
      <form method="post" action="/settings/test-alert"
            {confirm_attrs('Send a test alert email now?',
                                        busy_label='Sending…')}>
        <button class="save-btn" type="submit">Send test alert</button>
      </form>
    </div>"""
    else:
        test_alert_html = """
    <div class="section mt-6">
      <div class="section-head"><h2>Alert email</h2></div>
      <div class="none-note">Email isn't connected yet — connect it on the
        <a href="/settings?tab=connections">Connections page</a> to send a test
        alert.</div>
    </div>"""

    # AI provider: local Ollama by default; OpenAI/Anthropic are an
    # explicit, warned opt-in because they send data off this machine.
    ai_provider = ai_providers.provider_name()
    provider_options = ""
    for value, label in (("ollama", "Ollama (local, default)"),
                         ("openai", "OpenAI"),
                         ("anthropic", "Anthropic")):
        selected = " selected" if value == ai_provider else ""
        provider_options += (f'<option value="{value}"{selected}>'
                             f"{escape(label)}</option>")

    def _key_state(name):
        return ('<span class="tag ok">key saved</span>'
                if ai_providers.has_api_key(name)
                else '<span class="tag off">no key</span>')

    cloud_note = ""
    if ai_providers.is_cloud(ai_provider):
        cloud_note = f"""
      <div class="callout mt-2"><strong>Cloud AI
        active</strong>Questions to the assistant send your transaction
        summaries and balances to
        {escape(ai_providers.PROVIDER_LABELS[ai_provider])}'s servers.
        Switch back to Ollama to keep everything on this machine.</div>"""

    # The confirm dialog is the explicit consent step: switching TO a
    # cloud provider requires acknowledging that data leaves the
    # machine. Ollama never warns.
    ai_confirm_js = escape(
        "var p = this.ai_provider.value;"
        " if (p !== 'ollama') {"
        " var n = p === 'openai' ? 'OpenAI' : 'Anthropic';"
        " return confirm('This provider sends your financial data to '"
        " + n + ' servers. Your transaction summaries and balances will"
        " leave this machine. Continue?'); } return true;")

    ai_provider_html = f"""
    <div class="section mt-6">
      <div class="section-head"><h2>AI assistant</h2></div>
      <div class="hint mb-3">Which model answers
        the chat panel. Ollama runs locally and nothing leaves this
        machine. OpenAI and Anthropic are cloud services: they receive
        your transaction summaries and balances with every question,
        and each needs its own API key.</div>
      {cloud_note}
      <form method="post" action="/settings/ai-provider"
            onsubmit="{ai_confirm_js}">
        <div class="field mt-4">
          <label class="q" for="ai_provider">AI Provider</label>
          <select id="ai_provider" name="ai_provider">{provider_options}</select>
        </div>
        <div class="cred-field measure">
          <label class="q" for="openai_api_key">OpenAI API key
            {_key_state("openai")}</label>
          <div class="hint">Stored encrypted in your system credential
            store. Leave blank to keep the saved key.</div>
          <input type="password" id="openai_api_key" name="openai_api_key"
                 autocomplete="off" spellcheck="false" placeholder="sk-...">
        </div>
        <div class="cred-field measure">
          <label class="q" for="anthropic_api_key">Anthropic API key
            {_key_state("anthropic")}</label>
          <div class="hint">Stored encrypted in your system credential
            store. Leave blank to keep the saved key.</div>
          <input type="password" id="anthropic_api_key"
                 name="anthropic_api_key" autocomplete="off"
                 spellcheck="false" placeholder="sk-ant-...">
        </div>
        <button class="save-btn" type="submit">Save AI settings</button>
      </form>
    </div>"""

    pinned_metric_ids = set(insight_widget.get_pinned_metric_ids())
    insight_metric_rows = ""
    for definition in insight_metrics.METRIC_CATALOG:
        metric_id = definition["id"]
        checked = " checked" if metric_id in pinned_metric_ids else ""
        insight_metric_rows += f"""
          <li>
            <label class="acct-row insight-widget-option"
                   for="insight-metric-{escape(metric_id)}">
              <input type="checkbox" name="metric_ids"
                     value="{escape(metric_id)}"
                     id="insight-metric-{escape(metric_id)}"{checked}>
              <span class="insight-widget-copy">
                <span class="acct-name">{escape(definition["label"])}</span>
                <span class="insight-widget-meta">
                  <span class="insight-widget-source">{escape(definition["source"].title())}</span>
                  <span>{escape(definition["description"])}</span>
                </span>
              </span>
            </label>
          </li>"""

    insight_widget_html = f"""
    <div class="section mt-6" id="home-insights-widget">
      <div class="section-head"><h2>Home insights widget</h2></div>
      <div class="hint mb-3 measure">Choose the metrics shown in the compact
        Insights widget on Home. The full Insights page will still include
        every available metric. Leave all unchecked to hide the Home widget.</div>
      <form method="post" action="/settings/insights-widget">
        <fieldset class="insight-widget-fieldset">
          <legend class="sr-only">Metrics shown on Home</legend>
          <ul class="accounts insight-widget-options">
            {insight_metric_rows}
          </ul>
        </fieldset>
        <button class="save-btn mt-4" type="submit">Save Home widget</button>
      </form>
    </div>"""

    active = business_registry.active_business() or {}
    business_rows = ""
    for business in business_registry.list_businesses():
        is_active = business["id"] == active.get("id")
        state = '<span class="tag ok">active</span>' if is_active else ""
        controls = ""
        if not is_active:
            controls = f"""
            <form method="post" action="/business/switch" class="inline-form">
              <input type="hidden" name="business_id" value="{escape(business["id"])}">
              <button class="check" type="submit">Make active</button></form>
            <form method="post" action="/connections/business/remove" class="inline-form"
                  {confirm_attrs('Remove this business from Klearing? Historical transaction stamps remain in the local database.', busy_label='Removing…')}>
              <input type="hidden" name="business_id" value="{escape(business["id"])}">
              <button class="deny" type="submit">Remove</button></form>"""
        business_rows += (f'<li><span class="acct-name">'
                          f'{escape(business["name"])}</span>{state}{controls}</li>')
    business_registry_html = f"""
    <div class="section mt-6">
      <div class="section-head"><h2>Local businesses</h2></div>
      <div class="hint mb-3">Business identities and transaction stamps stay
        on this computer. The active business controls Review and local
        transaction views.</div>
      <ul class="accounts">{business_rows}</ul>
      <form method="post" action="/connections/business/add" class="row-inline mt-3">
        <input type="text" name="business_name" maxlength="120"
               placeholder="Another business" required>
        <button class="check" type="submit">Add business</button>
      </form>
    </div>"""

    content = f"""
    <div class="sub">{status}</div>
    <div class="wizard-actions mb-5">
      <a class="btn-link" href="/settings?tab=connections">Connections</a>
      <a class="btn-link" href="/settings?tab=automation">Automation</a>
      <a class="btn-link" href="/settings?tab=logs">Logs</a>
    </div>
    <form method="post" action="/settings">
      <div class="field">
        <label class="q" for="balance_threshold">Alert me when balance drops below</label>
        <div class="hint">Leave blank for no balance alert.</div>
        <div class="prefix-input">
          <span class="prefix">$</span>
          <input type="number" step="0.01" min="0" name="balance_threshold"
                 id="balance_threshold" value="{balance_value}" placeholder="e.g. 500">
        </div>
      </div>
      <div class="field">
        <label class="q" for="runway_days">Alert me when runway drops below</label>
        <div class="hint">Days of cash left at your recent spending rate. Clear to disable.</div>
        <input type="number" step="1" min="0" name="runway_days"
               id="runway_days" value="{runway_value}"> days
      </div>
      <div class="field">
        <label class="q" for="vendor_jump_threshold">Flag vendors when monthly spend jumps by</label>
        <div class="hint">The <a href="/vendors">Vendors tab</a> flags any vendor whose
          this-month spend exceeds last month's by at least this percentage.
          Blank falls back to {DEFAULT_VENDOR_JUMP_PCT:.0f}.</div>
        <input type="number" step="1" min="1" name="vendor_jump_threshold"
               id="vendor_jump_threshold" value="{vendor_jump_value}"> %
      </div>
      <div class="field">
        <label class="q">Which {escape(bank_name)} account(s) count as business cash</label>
        <div class="hint">Only checked accounts are summed for the balance and runway
          calculation. Personal or linked accounts can be left unchecked.</div>
        {accounts_html}
      </div>
      {health_weights_html}
      <div class="field">
        <label class="q" for="business_name">Business name</label>
        <div class="hint">Used in drafted follow-ups and reminders.</div>
        <input type="text" id="business_name" name="business_name"
               value="{business_value}" class="w-lg">
      </div>
      <div class="field">
        <label class="q" for="sender_name">Your name (signs drafted emails)</label>
        <input type="text" id="sender_name" name="sender_name"
               value="{sender_value}" class="w-lg">
      </div>
      <div class="field">
        <label class="q" for="auto_categorize_enabled">
          <input type="checkbox" id="auto_categorize_enabled"
                 name="auto_categorize_enabled"{auto_categorize_checked}>
          Auto-suggest categories
        </label>
        <div class="hint">Rules run instantly and unknown vendors may use
          local Ollama. Suggestions never approve or post anything, and can
          be hidden here without stopping categorization.</div>
      </div>
      <div class="field">
        <label class="q" for="accounting_basis">Accounting basis</label>
        <select id="accounting_basis" name="accounting_basis" class="w-md">
          {accounting_basis_options}
        </select>
        <div class="hint">Accrual counts income when you send an invoice.
          Cash counts it when the money actually arrives. Most sole
          proprietors file on cash; ask whoever prepares your taxes if you
          are not sure. This changes what invoicing records in the Ledger,
          and does not change bank or Shopify entries.</div>
      </div>
      {_tax_settings_fields()}
      <button class="save-btn" type="submit">Save settings</button>
    </form>
    {business_registry_html}
    {insight_widget_html}
    {ai_provider_html}
    {test_alert_html}
    <div class="section mt-6">
      <div class="section-head"><h2>My Klearing Book</h2></div>
      <div class="hint mb-3">A readable, offline copy of your posted books.
        Klearing builds it only when you ask, and every copy visibly states
        when it was last updated so an accountant can tell whether it is
        current. Pending work, credentials, CRM data, and app settings are
        not included.</div>
      <a class="save-btn" href="/book" target="_blank">Open readable book</a>
      <form method="post" action="/book/open-folder" class="inline-form">
        <button class="btn" type="submit">Open my book folder</button>
      </form>
    </div>
    <div class="section mt-6">
      <div class="section-head"><h2>Backup</h2></div>
      <div class="hint mb-3">One file with
        everything — history, settings, and credentials (including the
        encryption key), locked under a password you choose here. Keep it
        somewhere safe; restoring it on a fresh install brings the whole
        app back without re-running Setup. Anyone with the file AND this
        password can read your credentials, so pick a real password.</div>
      <form method="post" action="/backup/export">
        <div class="cred-field measure-sm">
          <label class="q" for="bk-pw">Backup password</label>
          <input type="password" id="bk-pw" name="backup_password"
                 minlength="8" required autocomplete="new-password">
        </div>
        <div class="cred-field measure-sm">
          <label class="q" for="bk-pw2">Confirm backup password</label>
          <input type="password" id="bk-pw2" name="confirm_backup_password"
                 minlength="8" required autocomplete="new-password">
        </div>
        <button class="save-btn" type="submit">Export backup</button>
      </form>
    </div>"""

    return render_shell("Settings", "", _settings_nav("general") + content,
                        message, error)


@app.post("/settings/insights-widget")
def save_insights_widget(metric_ids: List[str] = Form(default=[])):
    insight_widget.save_pinned_metric_ids(metric_ids)
    return _redirect_settings(msg="Home insights widget updated.")


@app.post("/settings/ai-provider")
def save_ai_provider(ai_provider: str = Form("ollama"),
                     openai_api_key: str = Form(""),
                     anthropic_api_key: str = Form("")):
    """Provider selection + API keys. The browser-side confirm dialog
    is the consent step for cloud providers; this handler validates
    the value and stores keys through the encrypted credential store.
    Blank key fields leave any saved key untouched."""
    db.init_db()
    provider = (ai_provider or "").strip().lower()
    if provider not in ai_providers.PROVIDER_LABELS:
        return _redirect_settings(err="Unknown AI provider.")
    try:
        if (openai_api_key or "").strip():
            secrets_store.set_secret(ai_providers.OPENAI_KEY_NAME,
                                     openai_api_key.strip())
        if (anthropic_api_key or "").strip():
            secrets_store.set_secret(ai_providers.ANTHROPIC_KEY_NAME,
                                     anthropic_api_key.strip())
    except secrets_store.SecretsError as exc:
        return _redirect_settings(err=str(exc))
    db.set_setting(ai_providers.PROVIDER_SETTING_KEY, provider)
    label = ai_providers.PROVIDER_LABELS[provider]
    if ai_providers.is_cloud(provider) and not ai_providers.has_api_key(provider):
        return _redirect_settings(
            msg=f"AI provider set to {label}. Add your API key in "
                "Settings to use this provider.")
    return _redirect_settings(msg=f"AI provider set to {label}.")


@app.post("/settings/test-alert")
def settings_test_alert():
    """One real send through the alert transport (email_utils.send_email,
    the same path cash alerts, digests, and reminders use)."""
    db.init_db()
    if not email_utils.is_configured():
        return _redirect_settings(
            err="Email isn't connected yet — connect it on the "
                "Connections page first.")
    recipient = ((db.get_setting(email_utils.NOTIFY_EMAIL_KEY) or "").strip()
                 or (db.get_setting(email_utils.GMAIL_ADDRESS_KEY) or "")
                 .strip())
    try:
        sent = email_utils.send_email(
            f"{APP_NAME}: test alert",
            "This is a test alert from Klearing's Settings page.\n\n"
            "If you're reading it, alert emails are set up correctly — "
            "cash alerts, daily notifications, and reminders will all "
            "arrive at this address.")
    except Exception as exc:
        return _redirect_settings(err=f"Test alert failed: {exc}")
    if not sent:
        return _redirect_settings(
            err="Test alert not sent — email looks disconnected. Check "
                "the Connections page.")
    return _redirect_settings(msg=f"Test alert sent to {recipient}.")


@app.post("/backup/export")
def backup_export(backup_password: str = Form(""),
                  confirm_backup_password: str = Form("")):
    import backup
    try:
        filename, file_bytes = backup.create_backup(backup_password,
                                                    confirm_backup_password)
    except (backup.BackupError, secrets_store.SecretsError) as exc:
        return _redirect_settings(err=str(exc))
    return Response(
        content=file_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/book")
def open_continuity_book():
    """A live-user request: publish when dirty, then serve the local copy."""
    try:
        result = continuity_artifact.publish_if_needed()
    except continuity_artifact.ContinuityArtifactError as exc:
        return HTMLResponse(render_shell(
            "My Klearing Book", "",
            "<div class='section'><h1>Readable book unavailable</h1>"
            f"<p>{escape(str(exc))}</p><p>No existing book files were overwritten.</p></div>",
        ), status_code=503)
    return FileResponse(result["path"], media_type="text/html")


@app.post("/book/open-folder")
def open_continuity_book_folder():
    """Publish and reveal only in response to this explicit user click."""
    try:
        continuity_artifact.reveal_folder()
    except continuity_artifact.ContinuityArtifactError as exc:
        return _redirect_settings(err=str(exc))
    except OSError as exc:
        return _redirect_settings(
            err=f"The book is current, but Windows could not open its folder: {exc}")
    return _redirect_settings(msg="Your readable book is current and its folder is open.")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(tab: str = "general", file: str = "app",
                  msg: str = None, err: str = None):
    """Settings and its three former siblings, under one tab bar."""
    db.init_db()
    if tab == "connections":
        return render_connections_page(message=msg, error=err)
    if tab == "automation":
        return render_automation_page(message=msg, error=err)
    if tab == "logs":
        active = file if file in paths.LOG_NAMES else "app"
        return render_logs_page(active, message=msg, error=err)
    accounts = get_bank_accounts()
    return render_settings_page(accounts, message=msg, error=err)


@app.post("/settings")
async def save_settings(
    request: Request,
    balance_threshold: str = Form(""),
    runway_days: str = Form(""),
    vendor_jump_threshold: str = Form(""),
    business_name: str = Form(""),
    sender_name: str = Form(""),
    cash_accounts: List[str] = Form(default=[]),
):
    db.init_db()
    form = await request.form()
    current_tax_settings = tax_settings.get()
    filing_status = str(form.get("tax_filing_status")
                        or current_tax_settings["filing_status"] or "").strip()
    state = str(form.get("tax_state")
                or current_tax_settings["state"] or "").strip().upper()
    business_type = str(form.get("tax_business_type")
                        or current_tax_settings["business_type"] or "").strip()
    try:
        posted_other_income = form.get("tax_other_income")
        raw_other_income = (
            posted_other_income.strip() if posted_other_income is not None
            else f'{Decimal(int(current_tax_settings["estimated_other_income"])) / 100:.2f}'
        )
        other_income = Decimal(raw_other_income or "0")
        if not other_income.is_finite() or other_income < 0:
            raise ValueError
        scaled_other_income = other_income * 100
        if scaled_other_income != scaled_other_income.to_integral_value():
            return _redirect_settings(
                err="Estimated other income can have at most two decimal places.")
        other_income_cents = int(scaled_other_income)
    except (InvalidOperation, TypeError, ValueError):
        return _redirect_settings(
            err="Estimated other income must be a dollar amount of zero or more.")
    try:
        tax_settings.validate(
            filing_status, state, other_income_cents, business_type)
    except ValueError as exc:
        return _redirect_settings(err=str(exc))

    # Health-score weights (one field per factor; blank/invalid ignored).
    for key in health.FACTORS:
        raw = (form.get(health.WEIGHT_KEY[key], "") or "").strip()
        if raw:
            db.set_setting(health.WEIGHT_KEY[key], raw)
    # Store the raw strings; readers parse/validate them. A blank
    # threshold field is stored as "" meaning disabled (or, for the
    # vendor jump, "use the default").
    db.set_setting(cash_alert.BALANCE_THRESHOLD_KEY, (balance_threshold or "").strip())
    db.set_setting(cash_alert.RUNWAY_DAYS_KEY, (runway_days or "").strip())
    db.set_setting(VENDOR_JUMP_KEY, (vendor_jump_threshold or "").strip())
    db.set_setting(cash_alert.CASH_ACCOUNTS_KEY, json.dumps(cash_accounts))
    clean_business_name = (business_name or "").strip()
    db.set_setting("business_name", clean_business_name)
    active = business_registry.active_business()
    if clean_business_name and active:
        business_registry.upsert_business(
            clean_business_name, business_id=active["id"], make_active=True)
    db.set_setting(email_utils.SENDER_NAME_KEY, (sender_name or "").strip())
    db.set_setting("auto_categorize_enabled",
                   "1" if form.get("auto_categorize_enabled") else "0")
    # Allowlisted rather than stored raw: an unrecognized basis would make
    # every downstream posting decision ambiguous, and silently defaulting a
    # typo to accrual is safer than persisting it.
    basis = (form.get(ledger.ACCOUNTING_BASIS_KEY) or "").strip().lower()
    if basis in ledger.ACCOUNTING_BASES:
        db.set_setting(ledger.ACCOUNTING_BASIS_KEY, basis)
    tax_settings.save(filing_status, state, other_income_cents, business_type)
    return _redirect_settings(msg="Settings saved.")


# -------------------------------------------------------------- Invoicing
# Outbound authoring, deliberately NOT the Review drawer. Review is inbound
# triage: things arrive, the user adjudicates one at a time. An invoice is
# originated by the user, has many fields, is iterated on and saved as a
# draft. Forcing authoring through a triage queue produces a drawer that
# grows into a form and a queue with one self-created item in it.


INVOICE_TABS = (
    ("all", "All"),
    ("drafts", "Drafts"),
    ("aging", "A/R aging"),
    ("recurring", "Recurring"),
)

INVOICE_STATUS_TAGS = {
    "draft": "tag off", "sent": "tag", "partial": "jump-tag",
    "paid": "tag ok", "overdue": "tag bad", "void": "tag off",
    "written_off": "tag off",
}


def _invoice_nav(active):
    return ui.sub_nav([(key, label, f"/invoices?tab={key}")
                       for key, label in INVOICE_TABS],
                      active, "Invoice views")


def _invoice_view(invoice, today=None):
    """One invoice plus everything derived from it, computed once."""
    lines = db.list_invoice_lines(invoice["id"])
    totals = invoice_ledger.invoice_totals(lines)
    balance = invoice_ledger.balance_cents(invoice["id"], lines)
    state = invoice_ledger.status(invoice, balance=balance, today=today)
    return {"invoice": invoice, "lines": lines, "totals": totals,
            "balance": balance, "status": state,
            "label": invoice_ledger.STATUS_LABELS.get(state, state),
            "tag": INVOICE_STATUS_TAGS.get(state, "tag"),
            "bucket": invoice_ledger.aging_bucket(invoice, today),
            "days_overdue": invoice_ledger.days_overdue(invoice, today)}


def _cents_to_input(cents):
    return f"{Decimal(int(cents)) / 100:.2f}"


# Declared once so each card-reflowed cell's data-label is the header's
# own string rather than a second copy of it. See ui.table_head.
INVOICE_COLUMNS = (
    ("Number", "text", ""),
    ("Customer", "text", ""),
    ("Issued", "text", ""),
    ("Due", "text", ""),
    ("Status", "text", ""),
    ("Total", "num", "num"),
    ("Outstanding", "num", "num"),
)


def _invoice_rows(views):
    label = lambda index: ui.cell_label(INVOICE_COLUMNS, index)
    rows = ""
    for view in views:
        invoice = view["invoice"]
        number = invoice["invoice_number"] or "Draft"
        contact = db.get_contact(invoice["contact_id"]) or {}
        rows += (
            f'<tr><td{label(0)}><a href="/invoices/{invoice["id"]}">'
            f'{escape(number)}</a></td>'
            f'<td{label(1)}>{escape(contact.get("display_name") or "—")}</td>'
            f'<td{label(2)}>{escape(invoice["issue_date"])}</td>'
            f'<td{label(3)}>{escape(invoice["due_date"])}</td>'
            f'<td{label(4)}><span class="{view["tag"]}">'
            f'{escape(view["label"])}</span></td>'
            f'<td class="num amount"{label(5)}>'
            f'{ui.money(Decimal(view["totals"]["gross"]) / 100)}</td>'
            f'<td class="num amount"{label(6)}>'
            f'{ui.money(Decimal(view["balance"]) / 100)}</td>'
            "</tr>")
    return rows


def _invoice_table(views, empty_note):
    """A record table: one row is one invoice you open and act on, and
    the figure you want (Outstanding) is the rightmost column, so on a
    phone it is exactly what horizontal scroll hides. Hence card-reflow
    here, while scanning tables like Contacts keep the scroll."""
    if not views:
        return f'<div class="none-note">{escape(empty_note)}</div>'
    return f"""
    <table class="table-hover card-reflow">
      {ui.table_head(INVOICE_COLUMNS)}
      <tbody>{_invoice_rows(views)}</tbody>
    </table>"""


def _ar_aging_content(views):
    """The standard five-bucket aging report.

    Every figure links through to the invoices behind it — an aging table
    whose numbers are not clickable is informational, not actionable, and
    the whole point of this report is deciding who to chase next.
    """
    open_views = [v for v in views
                  if v["status"] in ("sent", "partial", "overdue")]
    if not open_views:
        return ui.empty_state(
            "Nothing outstanding",
            "Every invoice you have sent is paid. Outstanding balances "
            "appear here, grouped by how overdue they are.")

    by_contact = {}
    totals = {bucket: 0 for bucket in invoice_ledger.AGING_BUCKETS}
    for view in open_views:
        contact = db.get_contact(view["invoice"]["contact_id"]) or {}
        name = contact.get("display_name") or "Unknown"
        row = by_contact.setdefault(
            name, {bucket: 0 for bucket in invoice_ledger.AGING_BUCKETS})
        row[view["bucket"]] += view["balance"]
        totals[view["bucket"]] += view["balance"]

    headers = "".join(f'<th class="num">{escape(bucket)}</th>'
                      for bucket in invoice_ledger.AGING_BUCKETS)
    rows = ""
    for name in sorted(by_contact):
        cells = "".join(
            f'<td class="num amount">'
            f'{ui.money(Decimal(by_contact[name][bucket]) / 100)}</td>'
            for bucket in invoice_ledger.AGING_BUCKETS)
        row_total = sum(by_contact[name].values())
        rows += (f"<tr><td>{escape(name)}</td>{cells}"
                 f'<td class="num amount">'
                 f"{ui.money(Decimal(row_total) / 100)}</td></tr>")
    total_cells = "".join(
        f'<td class="num amount">{ui.money(Decimal(totals[b]) / 100)}</td>'
        for b in invoice_ledger.AGING_BUCKETS)
    grand = sum(totals.values())

    return f"""
    <div class="sub">Outstanding balances by how far past due they are.
      Aged from the due date.</div>
    <div class="section">
      <table class="table-hover">
        <thead><tr><th data-sort="text">Customer</th>{headers}
          <th class="num">Total</th></tr></thead>
        <tbody>{rows}</tbody>
        <tfoot><tr><th>All customers</th>{total_cells}
          <th class="num amount">{ui.money(Decimal(grand) / 100)}</th></tr></tfoot>
      </table>
    </div>"""


def render_invoices_page(tab="all", message=None, error=None):
    today = date.today().isoformat()
    views = [_invoice_view(invoice, today) for invoice in db.list_invoices()]
    active = tab if tab in dict(INVOICE_TABS) else "all"

    if active == "recurring":
        body = _recurring_templates_content()
    elif not views and active != "aging":
        body = ui.empty_state(
            "No invoices yet",
            "Create one, send it, and Klearing records it in the Ledger and "
            "tracks what you are owed.",
            action_href="/invoices/new", action_label="New invoice")
    elif active == "drafts":
        body = _invoice_table([v for v in views if v["status"] == "draft"],
                              "No drafts. Everything you have written is sent.")
    elif active == "aging":
        body = _ar_aging_content(views)
    else:
        body = _invoice_table(views, "No invoices yet.")

    header_extra = ""
    strip = (_invoice_summary_strip(views)
             if views and active != "recurring" else "")
    content = f"{strip}{_invoice_nav(active)}{body}"
    return render_shell("Invoices", header_extra, content, message, error,
                        wide=True)


def _invoice_summary_strip(views, today=None):
    """What is owed, at a glance: overdue, due soon, and everything open.

    Sums the per-invoice balances already computed in cents by
    _invoice_view; it derives nothing new about money. Paid, draft, void
    and written-off invoices owe nothing and are left out.
    """
    today_date = date.fromisoformat(today or date.today().isoformat())
    overdue = due_soon = outstanding = 0
    for view in views:
        if view["status"] not in ("sent", "partial", "overdue"):
            continue
        outstanding += view["balance"]
        if view["status"] == "overdue":
            overdue += view["balance"]
            continue
        try:
            due = date.fromisoformat(view["invoice"]["due_date"])
        except (TypeError, ValueError):
            continue
        if 0 <= (due - today_date).days <= 30:
            due_soon += view["balance"]
    cell = lambda cents: ui.money(Decimal(cents) / 100)
    return f"""
    <div class="stat-grid mb-4">
      {ui.stat("Overdue", cell(overdue), tone="out" if overdue else None)}
      {ui.stat("Due in the next 30 days", cell(due_soon))}
      {ui.stat("Outstanding", cell(outstanding))}
    </div>"""


def _recurring_templates_content():
    """The list of recurring templates, with what each one bills and when."""
    templates = db.list_recurring_templates()
    contacts = db.list_contacts()

    if not contacts:
        return ui.empty_state(
            "No customers yet",
            "A recurring invoice needs someone to bill. Customers arrive "
            "with your Shopify sync, or you can add one by hand on Contacts.",
            action_href="/contacts", action_label="Contacts")

    if not templates:
        body = ui.empty_state(
            "No recurring invoices set up",
            "Set one up and Klearing queues it for your approval each time "
            "it comes due. Nothing is ever sent on a schedule without you.")
    else:
        rows = ""
        for template in templates:
            contact = db.get_contact(template["contact_id"]) or {}
            totals = recurring_invoices.preview_totals(template["id"])
            gross = Decimal(totals["gross"]) / 100
            paused = not template["active"]
            status = ('<span class="tag off">Paused</span>' if paused
                      else '<span class="tag ok">Active</span>')
            next_due = ("&mdash;" if paused
                        else escape(template["next_due_date"]))
            toggle_label = "Resume" if paused else "Pause"
            delete_attrs = ui.confirm_attrs(
                f"Delete this recurring invoice for "
                f"{contact.get('display_name', 'this contact')}? Any cycles "
                "already queued for review are deleted with it. Pausing "
                "keeps the record.",
                title="Delete recurring invoice", confirm_label="Delete",
                danger=True)
            label = lambda index: ui.cell_label(RECURRING_COLUMNS, index)
            rows += f"""
            <tr>
              <td{label(0)} data-sort-value="{escape(template["name"] or "", quote=True)}">
                <a href="/invoices/recurring/{template["id"]}">
                  {escape(template["name"] or "Untitled")}</a>
                <div class="meta">{escape(contact.get("display_name", "Unknown"))}</div></td>
              <td class="mixed"{label(1)}>{escape(recurring_invoices.describe(template))}</td>
              <td class="num"{label(2)}>{next_due}</td>
              <td class="num"{label(3)} data-sort-value="{totals["gross"]}">{money(gross)}</td>
              <td class="mixed"{label(4)}>{status}</td>
              <td class="actions"{label(5)}>
                <form method="post" class="inline-form"
                      action="/invoices/recurring/{template["id"]}/toggle">
                  <button class="check" type="submit">{toggle_label}</button>
                </form>
                <form method="post" class="inline-form"
                      action="/invoices/recurring/{template["id"]}/delete"
                      {delete_attrs}>
                  <button class="deny" type="submit">Delete</button>
                </form>
              </td>
            </tr>"""
        body = f"""
        <div class="section">
          <div class="section-head"><h2>Recurring invoices</h2>
            <span class="tag">{len(templates)}</span></div>
          <table class="table-hover card-reflow">
            {ui.table_head(RECURRING_COLUMNS)}
            <tbody>{rows}</tbody>
          </table>
          <p class="hint">A paused template queues nothing. Cycles that came
            due while the app was closed are caught up the next time it
            starts, so closing your laptop never loses a billing date.</p>
        </div>"""

    options = "".join(
        f'<option value="{contact["id"]}">{escape(contact["display_name"])}</option>'
        for contact in contacts)
    return f"""
    <div class="sub">Invoices that bill on a schedule. Each one lands in
      Review when it comes due, and only sends once you approve it.</div>
    {body}
    <div class="section">
      <div class="section-head"><h2>New recurring invoice</h2></div>
      <p class="hint">Pick who to bill and how often. You add the line items
        next, on the template's own page.</p>
      <form method="post" action="/invoices/recurring/create" class="field-row">
        <label class="grow">Customer
          <select name="contact_id" required>{options}</select></label>
        <label class="grow">Name
          <input type="text" name="name" maxlength="120" required
                 placeholder="e.g. Monthly retainer"></label>
        <label>Repeats
          <select name="recurrence">
            <option value="monthly">Monthly</option>
            <option value="weekly">Weekly</option>
            <option value="custom">Every N days</option>
          </select></label>
        <label>Every (days)
          <input type="number" name="interval_days" min="1" step="1"
                 class="w-xs" placeholder="30">
          <span class="hint">only for Every N days</span></label>
        <label>First due
          <input type="date" name="first_due_date" required></label>
        <button class="approve" type="submit">Create</button>
      </form>
    </div>"""


def _recurring_template_editor(template, message=None, error=None):
    """One template: who, how often, and what it bills.

    The line editor is the invoice one (_line_row + _parse_invoice_lines),
    reused rather than reimplemented, so a template's money arithmetic is
    the same code that prices a real invoice.
    """
    template_id = template["id"]
    contacts = db.list_contacts()
    lines = db.list_recurring_template_lines(template_id)
    totals = recurring_invoices.preview_totals(template_id)

    options = "".join(
        f'<option value="{contact["id"]}"'
        f'{" selected" if contact["id"] == template["contact_id"] else ""}>'
        f'{escape(contact["display_name"])}</option>'
        for contact in contacts)

    remove_action = f"/invoices/recurring/{template_id}/remove-line"
    rows = "".join(_line_row(index, line, remove_action)
                   for index, line in enumerate(lines))
    if not rows:
        rows = ('<tr><td colspan="6" class="none-note">No lines yet. '
                "Add one below.</td></tr>")

    recurrence_options = "".join(
        f'<option value="{key}"'
        f'{" selected" if template["recurrence"] == key else ""}>{label}</option>'
        for key, label in (("monthly", "Monthly"), ("weekly", "Weekly"),
                           ("custom", "Every N days")))

    gross = Decimal(totals["gross"]) / 100
    net = Decimal(totals["net"]) / 100
    tax = Decimal(totals["tax"]) / 100

    return f"""
    <p class="hint"><a href="/invoices?tab=recurring">All recurring
      invoices</a></p>
    <div class="section">
      <div class="section-head"><h2>{escape(template["name"] or "Untitled")}</h2>
        <span class="tag">{escape(recurring_invoices.describe(template))}</span>
        {'<span class="tag off">Paused</span>' if not template["active"]
         else '<span class="tag ok">Active</span>'}</div>
      <form method="post" action="/invoices/recurring/{template_id}/save">
        <div class="field-row">
          <label class="grow">Customer
            <select name="contact_id">{options}</select></label>
          <label class="grow">Name
            <input type="text" name="name" maxlength="120"
                   value="{escape(template["name"] or "", quote=True)}"></label>
          <label>Repeats
            <select name="recurrence">{recurrence_options}</select></label>
          <label>Every (days)
            <input type="number" name="interval_days" min="1" step="1"
                   class="w-xs"
                   value="{template["interval_days"] or ""}"></label>
        </div>
        <div class="field-row">
          <label>Next due
            <input type="date" name="next_due_date"
                   value="{escape(template["next_due_date"], quote=True)}"></label>
          <label>Payment terms (days)
            <input type="number" name="due_in_days" min="0" step="1"
                   class="w-xs" value="{template["due_in_days"]}"></label>
          <label class="grow">Terms
            <input type="text" name="terms" maxlength="200"
                   value="{escape(template["terms"] or "", quote=True)}"></label>
        </div>
        <table class="table-hover">
          <thead><tr>
            <th>Description</th><th class="num">Qty</th>
            <th class="num">Unit price</th><th class="num">Tax %</th>
            <th class="num">Amount</th><th></th>
          </tr></thead>
          <tbody>{rows}</tbody>
          <tfoot>
            <tr><td colspan="4">Net</td>
              <th class="num amount">{money(net)}</th><td></td></tr>
            <tr><td colspan="4">Tax</td>
              <th class="num amount">{money(tax)}</th><td></td></tr>
            <tr><td colspan="4">Total each time</td>
              <th class="num amount">{money(gross)}</th><td></td></tr>
          </tfoot>
        </table>
        <div class="fu-actions">
          <button class="approve" type="submit">Save</button>
          <button class="check" type="submit"
                  formaction="/invoices/recurring/{template_id}/add-line"
                  formnovalidate>Add line</button>
        </div>
      </form>
      <p class="hint">Changes apply to cycles generated from now on. Anything
        already queued in Review keeps the figures it was generated with,
        because that is what the date it was due actually said.</p>
    </div>"""


def _line_row(index, line, remove_action="/invoices/{invoice_id}/remove-line"):
    """One editable line.

    Inputs are named with their row index and parsed back server-side.
    Remove is a submit button carrying formaction, so the rest of the
    form still posts and in-progress edits on other rows survive being
    told to drop this one — the repo has no add/remove-row JS and this
    needs none.

    remove_action is a parameter so recurring-invoice templates reuse
    this row rather than growing a second, near-identical line editor —
    the fields, the parsing (_parse_invoice_lines) and the money
    disciplines are then shared by construction, not by convention.
    """
    quantity = f"{Decimal(int(line.get('quantity_milli', 1000))) / 1000:.3f}"
    price = _cents_to_input(line.get("unit_price_cents", 0))
    tax = f"{Decimal(int(line.get('tax_rate_bp', 0))) / 100:.2f}"
    net, line_tax = invoice_ledger.line_totals(line)
    return f"""
    <tr>
      <td><input type="text" name="description_{index}"
                 value="{escape(line.get("description", ""), quote=True)}"
                 class="w-lg" placeholder="What you are billing for"></td>
      <td class="num"><input type="number" step="0.001" min="0"
                 name="quantity_{index}" value="{quantity}" class="w-xs"></td>
      <td class="num"><input type="number" step="0.01" min="0"
                 name="price_{index}" value="{price}" class="w-sm"></td>
      <td class="num"><input type="number" step="0.01" min="0" max="100"
                 name="tax_{index}" value="{tax}" class="w-xs"></td>
      <td class="num amount">{ui.money(Decimal(net + line_tax) / 100)}</td>
      <td class="actions">
        <button class="check" type="submit" name="remove_line" value="{index}"
                formaction="{remove_action}"
                formnovalidate>Remove</button>
      </td>
    </tr>"""


def _invoice_editor(view, contacts, message=None, error=None):
    invoice = view["invoice"]
    invoice_id = invoice["id"]
    editable = view["status"] == "draft"

    options = "".join(
        f'<option value="{contact["id"]}"'
        f'{" selected" if contact["id"] == invoice["contact_id"] else ""}>'
        f'{escape(contact["display_name"])}</option>'
        for contact in contacts)

    rows = "".join(_line_row(index, line).replace("{invoice_id}",
                                                  str(invoice_id))
                   for index, line in enumerate(view["lines"]))
    if not rows:
        rows = ('<tr><td colspan="6" class="none-note">No lines yet. '
                "Add one below.</td></tr>")

    totals = view["totals"]
    gross_display = ui.money(Decimal(totals["gross"]) / 100)

    if editable:
        send_confirm = confirm_attrs(
            f"This sends the invoice and records {gross_display} in your "
            f"Ledger: {ui.money(Decimal(totals['net']) / 100)} to Sales "
            f"Revenue and {ui.money(Decimal(totals['tax']) / 100)} to Sales "
            "Tax Payable. The invoice cannot be edited afterwards.",
            title="Send this invoice?", confirm_label="Send",
            busy_label="Sending…")
        actions = f"""
        <div class="fu-actions">
          <button class="check" type="submit"
                  formaction="/invoices/{invoice_id}/add-line"
                  formnovalidate>Add line</button>
          <button class="check" type="submit">Save draft</button>
          <button class="approve" type="submit"
                  formaction="/invoices/{invoice_id}/send"
                  {send_confirm}>Send invoice</button>
        </div>"""
        note = ""
    else:
        actions = ""
        note = ('<div class="hint">A sent invoice is a record, so its lines '
                "are locked. Use a credit note or a refund to change what is "
                "owed.</div>")

    fieldset = f"""
    <div class="field-row">
      <div class="field">
        <label class="q" for="contact_id">Customer</label>
        <select id="contact_id" name="contact_id" class="w-lg"
                {"" if editable else "disabled"}>{options}</select>
      </div>
      <div class="field">
        <label class="q" for="issue_date">Issue date</label>
        <input type="date" id="issue_date" name="issue_date"
               value="{escape(invoice["issue_date"], quote=True)}"
               {"" if editable else "disabled"}>
      </div>
      <div class="field">
        <label class="q" for="due_date">Due date</label>
        <input type="date" id="due_date" name="due_date"
               value="{escape(invoice["due_date"], quote=True)}"
               {"" if editable else "disabled"}>
      </div>
      <div class="field">
        <label class="q" for="terms">Terms</label>
        <input type="text" id="terms" name="terms" class="w-sm"
               value="{escape(invoice["terms"], quote=True)}"
               placeholder="Net 30" {"" if editable else "disabled"}>
      </div>
    </div>"""

    editor = f"""
    <form method="post" action="/invoices/{invoice_id}/save">
      <div class="section">
        <div class="section-head"><h2>Details</h2></div>
        {fieldset}
      </div>
      <div class="section">
        <div class="section-head"><h2>Lines</h2></div>
        <table>
          <thead><tr><th>Description</th><th class="num">Qty</th>
            <th class="num">Unit price</th><th class="num">Tax %</th>
            <th class="num">Amount</th><th></th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <div class="totals">
          <div><span class="label">Subtotal</span>
            <span class="value amount">{ui.money(Decimal(totals["net"]) / 100)}</span></div>
          <div><span class="label">Tax</span>
            <span class="value amount">{ui.money(Decimal(totals["tax"]) / 100)}</span></div>
          <div><span class="label">Total</span>
            <span class="value amount">{gross_display}</span></div>
          <div><span class="label">Outstanding</span>
            <span class="value amount">{ui.money(Decimal(view["balance"]) / 100)}</span></div>
        </div>
        {note}
      </div>
      <div class="section">
        <div class="section-head"><h2>Notes on the invoice</h2></div>
        <textarea name="notes" rows="3" class="w-lg"
                  {"" if editable else "disabled"}
                  placeholder="Payment details, thanks, anything the customer should read"
                  >{escape(invoice["notes"])}</textarea>
      </div>
      {actions}
    </form>"""
    return editor


def _invoice_activity(invoice_id):
    payments = db.list_invoice_payments(invoice_id)
    notes = db.list_invoice_credit_notes(invoice_id)
    if not payments and not notes:
        return ""
    rows = ""
    for payment in payments:
        fee = int(payment["fee_cents"])
        detail = f"{escape(payment['method'])}"
        if fee:
            detail += f" &middot; fee {ui.money(Decimal(fee) / 100)}"
        rows += (f'<tr><td>{escape(payment["paid_on"])}</td>'
                 f"<td>Payment</td><td>{detail}</td>"
                 f'<td class="num amount">'
                 f'{ui.money(Decimal(int(payment["amount_cents"])) / 100)}</td></tr>')
    for note in notes:
        kind = "Refund" if note["is_refund"] else "Credit note"
        rows += (f'<tr><td>{escape(note["issued_on"])}</td>'
                 f"<td>{kind}</td><td>{escape(note['reason'])}</td>"
                 f'<td class="num amount">'
                 f'{ui.money(Decimal(int(note["amount_cents"])) / 100)}</td></tr>')
    return f"""
    <div class="section">
      <div class="section-head"><h2>Activity</h2></div>
      <table class="table-hover">
        <thead><tr><th>Date</th><th>Kind</th><th>Detail</th>
          <th class="num">Amount</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _invoice_money_actions(view):
    """Record a payment, credit, or write-off on a sent invoice."""
    invoice = view["invoice"]
    if view["status"] in ("draft", "void", "written_off"):
        return ""
    invoice_id = invoice["id"]
    outstanding = ui.money(Decimal(view["balance"]) / 100)
    write_off_confirm = confirm_attrs(
        f"This writes {outstanding} off as uncollectible and removes it from "
        "A/R aging. It posts to your Ledger and cannot be undone from here.",
        title="Write this invoice off?", confirm_label="Write off",
        danger=True)
    return f"""
    <div class="section">
      <div class="section-head"><h2>Record money</h2></div>
      <form method="post" action="/invoices/{invoice_id}/payment"
            class="range-form">
        <div class="field">
          <label class="q" for="paid_on">Date received</label>
          <input type="date" id="paid_on" name="paid_on"
                 value="{date.today().isoformat()}" required>
        </div>
        <div class="field">
          <label class="q" for="amount">Amount</label>
          <span class="prefix-input"><span class="prefix">$</span>
            <input type="number" step="0.01" min="0.01" id="amount"
                   name="amount" class="w-sm" required></span>
        </div>
        <div class="field">
          <label class="q" for="fee">Processor fee</label>
          <span class="prefix-input"><span class="prefix">$</span>
            <input type="number" step="0.01" min="0" id="fee" name="fee"
                   class="w-xs" value="0.00"></span>
          <div class="hint">Recorded as an expense, never netted off
            revenue.</div>
        </div>
        <button class="approve" type="submit">Record payment</button>
      </form>
      <form method="post" action="/invoices/{invoice_id}/write-off"
            class="inline-form mt-4" {write_off_confirm}>
        <button class="deny" type="submit">Write off {escape(outstanding)}</button>
      </form>
    </div>"""


def render_invoice_page(invoice_id, message=None, error=None):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return render_shell("Invoices", "", ui.empty_state(
            "That invoice does not exist",
            "It may have been removed.",
            action_href="/invoices", action_label="Back to invoices"),
            message, error, wide=True)
    view = _invoice_view(invoice)
    contacts = db.list_contacts()
    number = invoice["invoice_number"] or "Draft invoice"
    # PDF is offered on any invoice (a draft preview is useful); Email only
    # once it is a real sent document with a customer to send it to.
    pdf_link = (f'<a class="btn-link" href="/invoices/{invoice_id}/pdf">'
                "Download PDF</a>")
    email_action = ""
    if invoice["sent_at"] and not invoice["voided_at"]:
        email_action = (
            f'<form method="post" action="/invoices/{invoice_id}/email" '
            'class="inline-form"><button class="check" type="submit">'
            "Email invoice</button></form>")
    header_extra = f"""
      <div class="header-actions">
        <span class="{view["tag"]}">{escape(view["label"])}</span>
        {pdf_link}{email_action}
        <a class="btn-link" href="/invoices">All invoices</a>
      </div>"""
    content = (f'<div class="sub">{escape(number)}</div>'
               + _invoice_overview(view)
               + _invoice_steps(view)
               + _invoice_payment_link_block(view)
               + _invoice_editor(view, contacts)
               + _invoice_money_actions(view)
               + _invoice_activity(invoice_id))
    return render_shell("Invoices", header_extra, content, message, error,
                        wide=True)


def _invoice_overview(view):
    """Customer, total, outstanding and due date, above the editor."""
    invoice = view["invoice"]
    contact = db.get_contact(invoice["contact_id"]) or {}
    cell = lambda cents: ui.money(Decimal(cents) / 100)
    return f"""
    <div class="stat-grid mb-4">
      {ui.stat("Customer", escape(contact.get("display_name") or "Not set"))}
      {ui.stat("Total", cell(view["totals"]["gross"]))}
      {ui.stat("Outstanding", cell(view["balance"]))}
      {ui.stat("Due", escape(invoice["due_date"] or "Not set"))}
    </div>"""


def _invoice_steps(view):
    """Create, send, get paid: where this invoice is in its life.

    Read-only. Every control stays where it already was (the editor and
    the money actions below); this only says which step is current.
    """
    invoice = view["invoice"]
    status = view["status"]
    sent = bool(invoice["sent_at"])
    if status == "draft":
        create_note = "Draft. Not issued yet."
    else:
        create_note = f"Issued {escape(invoice['issue_date'] or '')}."
    if sent:
        send_note = f"Sent {escape(str(invoice['sent_at'])[:10])}."
    else:
        send_note = "Not sent yet."
    if status == "paid":
        paid_note = "Paid in full."
    elif status in ("void", "written_off"):
        paid_note = f"{escape(view['label'])}. Nothing is owed."
    else:
        paid_note = (f"{ui.money(Decimal(view['balance']) / 100)} outstanding, "
                     f"due {escape(invoice['due_date'] or 'on no date')}.")
    steps = [("Create", create_note, True),
             ("Send", send_note, sent),
             ("Get paid", paid_note, status in ("paid", "void", "written_off"))]
    current = next((i for i, step in enumerate(steps) if not step[2]), None)
    check = ('<svg viewBox="0 0 20 20" aria-hidden="true" width="14" '
             'height="14"><path d="M4 10.5l4 4 8-9" fill="none" '
             'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round"/></svg>')
    items = ""
    for index, (title, note, done) in enumerate(steps):
        attrs = ' aria-current="step"' if index == current else ""
        cls = "progress-step done" if done else "progress-step"
        mark = check if done else str(index + 1)
        items += (f'<li class="{cls}"{attrs}><span class="step-mark">{mark}</span>'
                  f'<div><div class="step-title">{title}</div>'
                  f'<div class="step-detail">{note}</div></div></li>')
    return f'<ol class="progress-steps" aria-label="Invoice progress">{items}</ol>'


def _invoice_payment_link_block(view):
    """Show the Stripe pay-online link, or offer to create one.

    Only meaningful once the invoice is a real sent document with an open
    balance. A paid, void, written-off or draft invoice needs no link.
    """
    invoice = view["invoice"]
    if not invoice["sent_at"] or view["status"] in (
            "paid", "void", "written_off"):
        return ""
    if invoice["payment_link_url"]:
        url = escape(invoice["payment_link_url"], quote=True)
        text = escape(invoice["payment_link_url"])
        return f"""
        <div class="section">
          <div class="section-head"><h2>Online payment</h2></div>
          <div class="sub">Customers can pay this invoice here. The link
            redirects to Stripe's own secure page — card details never
            touch Klearing.</div>
          <p><a href="{url}" target="_blank" rel="noopener">{text}</a></p>
        </div>"""
    if not stripe_client.is_configured():
        return ""
    return f"""
    <div class="section">
      <div class="section-head"><h2>Online payment</h2></div>
      <form method="post" action="/invoices/{invoice["id"]}/payment-link"
            class="inline-form">
        <button class="check" type="submit">Create a Stripe payment link</button>
      </form>
    </div>"""


def _is_blank_line(line):
    return not line["description"] and not line["unit_price_cents"]


def _parse_invoice_lines(form, keep_blanks=False):
    """Rebuild the line list from indexed form fields.

    Iterates the indices actually present rather than assuming range(n),
    since a removal leaves a hole for one request. Blank rows are dropped
    on save so an untouched "Add line" never persists as an empty row --
    but keep_blanks preserves positions, which the remove handler needs so
    the index the user clicked still lines up with the row they saw.
    """
    indices = sorted({
        int(key.split("_", 1)[1])
        for key in form
        if key.startswith("description_") and key.split("_", 1)[1].isdigit()
    })
    lines = []
    for index in indices:
        quantity = _decimal_or_zero(form.get(f"quantity_{index}"))
        price = _decimal_or_zero(form.get(f"price_{index}"))
        tax = _decimal_or_zero(form.get(f"tax_{index}"))
        lines.append({
            "description": (form.get(f"description_{index}") or "").strip(),
            "quantity_milli": int((quantity * 1000).quantize(Decimal("1"))),
            "unit_price_cents": int((price * 100).quantize(Decimal("1"))),
            "tax_rate_bp": int((tax * 100).quantize(Decimal("1"))),
        })
    if keep_blanks:
        return lines
    return [line for line in lines if not _is_blank_line(line)]


def _decimal_or_zero(raw):
    try:
        value = Decimal(str(raw or "0").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return value if value.is_finite() and value >= 0 else Decimal("0")


def _invoice_pdf(invoice):
    """(filename, pdf_bytes) for one invoice, or raise ExportError.

    Gathers the data here and hands plain values to
    accountant_export.build_invoice_pdf, which stays free of app/db
    imports. The filename uses the invoice number, or the id for a draft
    preview so a not-yet-numbered invoice still downloads sensibly.
    """
    lines = db.list_invoice_lines(invoice["id"])
    totals = invoice_ledger.invoice_totals(lines)
    contact = db.get_contact(invoice["contact_id"]) or {}
    business = (db.get_setting("business_name") or "").strip() or None
    pdf = accountant_export.build_invoice_pdf(
        invoice, lines, contact, totals, business_name=business)
    stem = invoice["invoice_number"] or f"draft-{invoice['id']}"
    return f"Invoice-{stem}.pdf", pdf


def _ensure_payment_link(invoice):
    """Create and store a Stripe payment link for a sent invoice, if one is
    wanted and not already present.

    Returns (ok: bool, detail: str) where detail is the URL on success or a
    plain-language reason otherwise. Best-effort, never raises: like the
    email, a Stripe hiccup must not undo a posted invoice. A 403 is called
    out specifically because it almost always means a restricted key that
    lacks Payment Links write scope, which is a fixable setup problem
    rather than an outage.
    """
    if invoice.get("payment_link_url"):
        return True, invoice["payment_link_url"]
    if not stripe_client.is_configured():
        return False, "Stripe isn't connected"
    if not invoice.get("invoice_number"):
        return False, "the invoice must be sent before a link can be made"
    lines = db.list_invoice_lines(invoice["id"])
    gross = invoice_ledger.invoice_totals(lines)["gross"]
    try:
        link = stripe_client.create_payment_link(
            invoice["invoice_number"], gross, invoice["id"])
    except stripe_client.StripeHTTPError as exc:
        if getattr(exc, "status", None) == 403:
            return False, ("Stripe refused the request — the connected key "
                           "cannot create payment links. Use a key with "
                           "Payment Links write access.")
        return False, f"Stripe error: {exc}"
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return False, f"could not reach Stripe ({exc})"
    db.update_invoice(invoice["id"], payment_link_url=link["url"],
                      stripe_payment_link_id=link["id"])
    return True, link["url"]


def _email_invoice(invoice):
    """Best-effort: email the invoice PDF to its customer.

    Returns (sent: bool, reason: str). Deliberately never raises -- the
    ledger posting is the authoritative act and must not be undone by a
    mail problem, so a failure here downgrades to a reported warning and
    an offer to download or resend, exactly the trade the Settings
    test-alert route already makes.
    """
    contact = db.get_contact(invoice["contact_id"]) or {}
    recipient = (contact.get("email") or "").strip()
    if not recipient:
        return False, "this customer has no email address on file"
    if not email_utils.is_configured():
        return False, "email isn't connected"
    try:
        filename, pdf = _invoice_pdf(invoice)
    except accountant_export.ExportError as exc:
        return False, str(exc)
    number = invoice["invoice_number"] or "your invoice"
    business = (db.get_setting("business_name") or "").strip() or "us"
    body_lines = [
        f"Hi {contact.get('display_name') or 'there'},",
        "",
        f"Please find invoice {number} attached, due "
        f"{invoice['due_date']}.",
    ]
    if invoice.get("payment_link_url"):
        body_lines += ["", f"Pay online: {invoice['payment_link_url']}"]
    body_lines += ["", f"Thank you,", business]
    try:
        email_utils.send_email(
            f"Invoice {number} from {business}", "\n".join(body_lines),
            to=recipient, attachments=[(filename, pdf, "pdf")])
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return False, f"the email did not go through ({exc})"
    return True, recipient


# ---------------------------------------------------------------- Routes


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(tab: str = "all", msg: str = None, err: str = None):
    db.init_db()
    return render_invoices_page(tab, message=msg, error=err)


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new_page():
    """Create a draft immediately and open it.

    A draft row costs nothing and gets no invoice number, so there is no
    reason to make the user fill in a form before they can see the editor.
    """
    db.init_db()
    contacts = db.list_contacts()
    if not contacts:
        return render_shell("Invoices", "", ui.empty_state(
            "No customers yet",
            "An invoice needs someone to bill. Customers arrive with your "
            "Shopify sync, or you can add one by hand on Contacts.",
            action_href="/contacts",
            action_label="Contacts"), None, None, wide=True)
    today = date.today()
    invoice_id = db.create_invoice(
        contacts[0]["id"], today.isoformat(),
        (today + timedelta(days=30)).isoformat(), terms="Net 30")
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_page(invoice_id: int, msg: str = None, err: str = None):
    db.init_db()
    return render_invoice_page(invoice_id, message=msg, error=err)


def _save_invoice_from_form(invoice_id, form):
    """Persist the whole editor in one write. Shared by every button on the
    form so switching rows or adding a line never discards other edits."""
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise invoice_ledger.InvoiceError("That invoice no longer exists.")
    if invoice["sent_at"] or invoice["voided_at"]:
        raise invoice_ledger.InvoiceError(
            "A sent invoice cannot be edited. Use a credit note instead.")
    try:
        contact_id = int(form.get("contact_id") or invoice["contact_id"])
    except (TypeError, ValueError):
        contact_id = invoice["contact_id"]
    db.update_invoice(
        invoice_id,
        contact_id=contact_id,
        issue_date=(form.get("issue_date") or invoice["issue_date"]).strip(),
        due_date=(form.get("due_date") or invoice["due_date"]).strip(),
        terms=(form.get("terms") or "").strip(),
        notes=(form.get("notes") or "").strip(),
    )
    return _parse_invoice_lines(form)


@app.post("/invoices/{invoice_id}/save")
async def invoice_save(invoice_id: int, request: Request):
    db.init_db()
    form = await request.form()
    try:
        lines = _save_invoice_from_form(invoice_id, form)
    except invoice_ledger.InvoiceError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    db.replace_invoice_lines(invoice_id, lines)
    return _redirect_invoice(invoice_id, msg="Draft saved.")


@app.post("/invoices/{invoice_id}/add-line")
async def invoice_add_line(invoice_id: int, request: Request):
    db.init_db()
    form = await request.form()
    try:
        lines = _save_invoice_from_form(invoice_id, form)
    except invoice_ledger.InvoiceError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    lines.append({"description": "", "quantity_milli": 1000,
                  "unit_price_cents": 0, "tax_rate_bp": 0})
    db.replace_invoice_lines(invoice_id, lines)
    return _redirect_invoice(invoice_id)


@app.post("/invoices/{invoice_id}/remove-line")
async def invoice_remove_line(invoice_id: int, request: Request):
    db.init_db()
    form = await request.form()
    try:
        lines = _save_invoice_from_form(invoice_id, form)
    except invoice_ledger.InvoiceError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    # Parse again keeping blanks: the index the user clicked refers to the
    # row they were looking at, and dropping blanks first would shift it.
    del lines
    try:
        target = int(form.get("remove_line"))
    except (TypeError, ValueError):
        return _redirect_invoice(invoice_id)
    positioned = _parse_invoice_lines(form, keep_blanks=True)
    kept = [line for index, line in enumerate(positioned) if index != target]
    db.replace_invoice_lines(
        invoice_id, [line for line in kept if not _is_blank_line(line)])
    return _redirect_invoice(invoice_id, msg="Line removed.")


@app.post("/invoices/{invoice_id}/send")
async def invoice_send(invoice_id: int, request: Request):
    db.init_db()
    form = await request.form()
    try:
        lines = _save_invoice_from_form(invoice_id, form)
        db.replace_invoice_lines(invoice_id, lines)
        result = invoice_ledger.send_invoice(invoice_id)
    except invoice_ledger.InvoiceError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    except ledger.LedgerValidationError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))

    # The posting is the authoritative act and has now happened. A payment
    # link and the email are best-effort on top of it: a Stripe or mail
    # failure warns, it does not roll back a sent, numbered, posted invoice.
    # Link first so the email and PDF carry it.
    _ensure_payment_link(db.get_invoice(invoice_id))
    sent_email, reason = _email_invoice(db.get_invoice(invoice_id))

    if result["basis"] == "cash":
        note = (f"Invoice {result['number']} sent. Nothing posted yet — on "
                "cash basis, income is recorded when the money arrives.")
    else:
        gross = ui.money(Decimal(result["totals"]["gross"]) / 100)
        note = (f"Invoice {result['number']} sent and {gross} recorded in "
                "Accounts Receivable.")
    if sent_email:
        return _redirect_invoice(invoice_id, msg=f"{note} Emailed to {reason}.")
    return _redirect_invoice(
        invoice_id,
        msg=f"{note} It was not emailed — {reason}. You can download the PDF "
            "or email it once that is sorted.")


@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int):
    db.init_db()
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return _redirect_invoices_list(err="That invoice no longer exists.")
    try:
        filename, pdf = _invoice_pdf(invoice)
    except accountant_export.ExportError as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/invoices/{invoice_id}/payment-link")
def invoice_payment_link(invoice_id: int):
    """Create (or surface) the Stripe payment link on demand — for an
    invoice sent before Stripe was connected, or where link creation failed
    at send time."""
    db.init_db()
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return _redirect_invoices_list(err="That invoice no longer exists.")
    ok, detail = _ensure_payment_link(invoice)
    if ok:
        return _redirect_invoice(invoice_id, msg="Payment link ready.")
    return _redirect_invoice(invoice_id, err=f"No payment link — {detail}.")


@app.post("/invoices/{invoice_id}/email")
def invoice_email(invoice_id: int):
    """Send (or resend) the invoice email. Separate from the send action so
    a sent invoice whose email bounced or whose customer was fixed can be
    re-emailed without touching the ledger."""
    db.init_db()
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return _redirect_invoices_list(err="That invoice no longer exists.")
    if not invoice["sent_at"]:
        return _redirect_invoice(invoice_id,
                                 err="Send the invoice before emailing it.")
    sent_email, reason = _email_invoice(invoice)
    if sent_email:
        return _redirect_invoice(invoice_id, msg=f"Emailed to {reason}.")
    return _redirect_invoice(invoice_id, err=f"Not emailed — {reason}.")


@app.post("/invoices/{invoice_id}/payment")
async def invoice_payment(invoice_id: int, request: Request):
    db.init_db()
    form = await request.form()
    amount = _decimal_or_zero(form.get("amount"))
    fee = _decimal_or_zero(form.get("fee"))
    paid_on = (form.get("paid_on") or date.today().isoformat()).strip()
    if amount <= 0:
        return _redirect_invoice(invoice_id, err="Enter an amount received.")
    payment_id = db.record_invoice_payment(
        invoice_id, paid_on,
        int((amount * 100).quantize(Decimal("1"))),
        fee_cents=int((fee * 100).quantize(Decimal("1"))))
    try:
        invoice_ledger.post_payment(invoice_id, payment_id)
    except (invoice_ledger.InvoiceError, ledger.LedgerValidationError) as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    return _redirect_invoice(invoice_id, msg="Payment recorded.")


@app.post("/invoices/{invoice_id}/write-off")
def invoice_write_off(invoice_id: int):
    db.init_db()
    try:
        invoice_ledger.write_off(invoice_id)
    except (invoice_ledger.InvoiceError, ledger.LedgerValidationError) as exc:
        return _redirect_invoice(invoice_id, err=str(exc))
    return _redirect_invoice(invoice_id, msg="Written off as uncollectible.")


# ------------------------------------------------------ recurring invoices
# Templates live under Invoices; the queue they feed lives in Review. The
# split is deliberate: authoring a schedule and deciding this month's
# invoice are different jobs done at different times.


def _recurring_template_from_form(form):
    """The fields a template accepts, parsed and bounded.

    Returns (fields, error). Monthly captures its anchor day from the
    chosen date so a 31st template can recover the 31st after February
    (see recurring_invoices.next_due_after).
    """
    recurrence = (form.get("recurrence") or "monthly").strip()
    if recurrence not in db.RECURRENCES:
        return None, "Pick a valid repeat schedule."

    raw_date = (form.get("next_due_date") or form.get("first_due_date")
                or "").strip()
    try:
        due = date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        return None, "Enter a valid date for the next invoice."

    interval_days = None
    if recurrence == "custom":
        try:
            interval_days = int(form.get("interval_days") or 0)
        except (TypeError, ValueError):
            interval_days = 0
        if interval_days < 1:
            return None, ("An 'every N days' schedule needs an interval of at "
                          "least one day.")

    try:
        due_in_days = int(form.get("due_in_days") or 30)
    except (TypeError, ValueError):
        due_in_days = 30

    return {
        "contact_id": int(form.get("contact_id") or 0),
        "name": (form.get("name") or "").strip()[:120],
        "recurrence": recurrence,
        "interval_days": interval_days,
        "anchor_day": due.day if recurrence == "monthly" else None,
        "next_due_date": due.isoformat(),
        "terms": (form.get("terms") or "").strip()[:200],
        "due_in_days": max(0, due_in_days),
    }, None


@app.post("/invoices/recurring/create")
async def recurring_template_create(request: Request):
    db.init_db()
    form = await request.form()
    fields, error = _recurring_template_from_form(form)
    if error:
        return _redirect_recurring(err=error)
    if not db.get_contact(fields["contact_id"]):
        return _redirect_recurring(err="Pick a customer to bill.")
    template_id = db.create_recurring_template(**fields)
    return _make_redirect(f"/invoices/recurring/{template_id}")(
        msg="Created. Add the line items it should bill.")


@app.get("/invoices/recurring/{template_id}", response_class=HTMLResponse)
def recurring_template_page(template_id: int, msg: str = None, err: str = None):
    db.init_db()
    template = db.get_recurring_template(template_id)
    if template is None:
        return render_shell("Invoices", "", ui.empty_state(
            "No such recurring invoice",
            "It may have been deleted.",
            action_href="/invoices?tab=recurring",
            action_label="All recurring invoices"), msg, err, wide=True)
    return render_shell("Invoices", "",
                        _recurring_template_editor(template), msg, err,
                        wide=True)


@app.post("/invoices/recurring/{template_id}/save")
async def recurring_template_save(template_id: int, request: Request):
    db.init_db()
    if db.get_recurring_template(template_id) is None:
        return _redirect_recurring(err="That recurring invoice no longer exists.")
    form = await request.form()
    fields, error = _recurring_template_from_form(form)
    redirect = _make_redirect(f"/invoices/recurring/{template_id}")
    if error:
        return redirect(err=error)
    if not db.get_contact(fields["contact_id"]):
        return redirect(err="Pick a customer to bill.")
    db.update_recurring_template(template_id, **fields)
    db.replace_recurring_template_lines(
        template_id, _parse_invoice_lines(form))
    return redirect(msg="Saved.")


@app.post("/invoices/recurring/{template_id}/add-line")
async def recurring_template_add_line(template_id: int, request: Request):
    db.init_db()
    if db.get_recurring_template(template_id) is None:
        return _redirect_recurring(err="That recurring invoice no longer exists.")
    form = await request.form()
    lines = _parse_invoice_lines(form)
    lines.append({"description": "", "quantity_milli": 1000,
                  "unit_price_cents": 0, "tax_rate_bp": 0})
    db.replace_recurring_template_lines(template_id, lines)
    return _make_redirect(f"/invoices/recurring/{template_id}")()


@app.post("/invoices/recurring/{template_id}/remove-line")
async def recurring_template_remove_line(template_id: int, request: Request):
    db.init_db()
    if db.get_recurring_template(template_id) is None:
        return _redirect_recurring(err="That recurring invoice no longer exists.")
    form = await request.form()
    # keep_blanks so the index the user clicked still lines up with the
    # row they saw, the same reason the invoice editor does it.
    lines = _parse_invoice_lines(form, keep_blanks=True)
    try:
        index = int(form.get("remove_line"))
    except (TypeError, ValueError):
        return _make_redirect(f"/invoices/recurring/{template_id}")(
            err="Could not tell which line to remove.")
    if 0 <= index < len(lines):
        lines.pop(index)
    db.replace_recurring_template_lines(
        template_id, [line for line in lines if not _is_blank_line(line)])
    return _make_redirect(f"/invoices/recurring/{template_id}")(
        msg="Line removed.")


@app.post("/invoices/recurring/{template_id}/toggle")
def recurring_template_toggle(template_id: int):
    db.init_db()
    template = db.get_recurring_template(template_id)
    if template is None:
        return _redirect_recurring(err="That recurring invoice no longer exists.")
    now_active = 0 if template["active"] else 1
    db.update_recurring_template(template_id, active=now_active)
    return _redirect_recurring(
        msg="Resumed. It will queue again when its next date arrives."
            if now_active else
            "Paused. It queues nothing until you resume it.")


@app.post("/invoices/recurring/{template_id}/delete")
def recurring_template_delete(template_id: int):
    db.init_db()
    template = db.get_recurring_template(template_id)
    if template is None:
        return _redirect_recurring(err="That recurring invoice no longer exists.")
    db.delete_recurring_template(template_id)
    return _redirect_recurring(
        msg=f'Deleted "{template["name"] or "Untitled"}".')


@app.post("/review/recurring/{instance_id}/approve")
def review_recurring_approve(instance_id: int):
    db.init_db()
    try:
        result = recurring_invoices.approve(instance_id)
    except (recurring_invoices.RecurringInvoiceError,
            invoice_ledger.InvoiceError, ledger.LedgerValidationError) as exc:
        return _redirect_review_recurring(err=str(exc))
    gross = Decimal(result["totals"]["gross"]) / 100
    return _redirect_review_recurring(
        msg=f'Sent {result["number"]} for {ui.money(gross)}.')


@app.post("/review/recurring/{instance_id}/skip")
def review_recurring_skip(instance_id: int):
    db.init_db()
    try:
        recurring_invoices.skip(instance_id)
    except recurring_invoices.RecurringInvoiceError as exc:
        return _redirect_review_recurring(err=str(exc))
    return _redirect_review_recurring(
        msg="Skipped. The schedule keeps running as normal.")


@app.post("/review/payment/{match_id}/apply")
def review_payment_apply(match_id: int):
    db.init_db()
    try:
        invoice_reconcile.apply_match(match_id)
    except (invoice_ledger.InvoiceError, ledger.LedgerValidationError) as exc:
        return _redirect_payments(err=str(exc))
    return _redirect_payments(msg="Payment applied.")


@app.post("/review/payment/{match_id}/dismiss")
def review_payment_dismiss(match_id: int):
    db.init_db()
    try:
        invoice_reconcile.dismiss_match(match_id)
    except invoice_ledger.InvoiceError as exc:
        return _redirect_payments(err=str(exc))
    return _redirect_payments(
        msg="Dismissed. A bank deposit will post as ordinary income on the "
        "next Ledger sync.")


@app.post("/review/inventory/{event_id}/post")
def review_inventory_post(event_id: int,
                          funding_account_code: str = Form("")):
    db.init_db()
    try:
        inventory.reconcile_acquisition(event_id, funding_account_code)
    except (inventory.InventoryError, ledger.LedgerValidationError) as exc:
        return _redirect_review_inventory(err=str(exc))
    return _redirect_review_inventory(msg="Inventory acquisition posted.")


@app.post("/review/inventory/{event_id}/unresolvable")
def review_inventory_unresolvable(event_id: int, note: str = Form("")):
    db.init_db()
    try:
        inventory.acknowledge_unresolvable(event_id, note)
    except inventory.InventoryError as exc:
        return _redirect_review_inventory(err=str(exc))
    return _redirect_review_inventory(
        msg="Acquisition acknowledged as unresolvable and excluded from COGS.")


# --------------------------------------------------------------- Contacts
# The unified business graph. Klearing already knows that this order
# belongs to this customer and cost this much to fulfil; these screens are
# where that shows. All reads — crm.py posts nothing, and the only thing
# stored here that is not derivable from elsewhere is the pipeline stage,
# which is a judgment rather than a fact.

CONTACT_TABS = (
    ("all", "All contacts"),
    ("pipeline", "Pipeline"),
)

CONTACT_ROW_LIMIT = 200

# Recurring templates card-reflow on a phone, so their headers are
# declared once and each cell's data-label comes from this same list.
# The trailing actions column has no header, which the stylesheet reads
# as "no label on this cell".
RECURRING_COLUMNS = (
    ("Name", "text", ""),
    ("Cadence", "text", ""),
    ("Next due", "date", "num"),
    ("Amount", "num", "num"),
    ("Status", "text", ""),
    ("", None, ""),
)


def _contacts_nav(active):
    return ui.sub_nav([(key, label, f"/contacts?tab={key}")
                       for key, label in CONTACT_TABS],
                      active, "Contact views")


def _stage_pill(stage):
    """A stage as a tag, including the unassigned case.

    Unassigned reads as a real state rather than an empty cell, because
    it is one: every Shopify-seeded contact starts there and a blank
    would look like missing data instead of an unmade decision.
    """
    label = crm.stage_label(stage)
    cls = "tag" if stage else "tag off"
    return f'<span class="{cls}">{escape(label)}</span>'


def _margin_cell(row):
    """Margin, or an honest dash when nothing is costed.

    A contact whose orders all lack COGS has no margin — not a margin of
    zero. Rendering 0.00 there would be a number the books never said.
    """
    if row["margin_cents"] is None:
        return '<td class="num">&mdash;</td>'
    partial = ('<div class="meta">'
               f'{row["orders_costed"]} of {row["orders_with_revenue"]} orders'
               "</div>") if row["margin_is_partial"] else ""
    return (f'<td class="num" data-sort-value="{row["margin_cents"]}">'
            f'{money(row["margin"])}{partial}</td>')


def _contacts_all_content():
    summaries = crm.contact_summaries()
    if not summaries:
        return ui.empty_state(
            "No contacts yet",
            "Contacts arrive with your Shopify customer sync, or you can "
            "add one below. Every contact carries its own order history, "
            "invoices and margin once there is something to show.",
            action_href="/settings?tab=connections",
            action_label="Connections") + _contact_add_form()

    shown = summaries[:CONTACT_ROW_LIMIT]
    overflow = ""
    if len(summaries) > CONTACT_ROW_LIMIT:
        overflow = (f'<p class="hint">Showing the {CONTACT_ROW_LIMIT} '
                    f"highest-revenue contacts of {len(summaries)}.</p>")

    rows = ""
    for row in shown:
        contact = row["contact"]
        email = contact.get("email") or ""
        sub = f'<div class="meta">{escape(email)}</div>' if email else ""
        rows += f"""
        <tr>
          <td data-sort-value="{escape(contact["display_name"], quote=True)}">
            <a href="/contacts/{contact["id"]}">{escape(contact["display_name"])}</a>
            {sub}</td>
          <td class="mixed">{_stage_pill(row["stage"])}</td>
          <td class="num" data-sort-value="{row["orders"]}">{row["orders"]}</td>
          <td class="num" data-sort-value="{row["revenue_cents"]}">
            {money(row["revenue"])}</td>
          {_margin_cell(row)}
        </tr>"""

    unattributed = db.unattributed_shopify_order_count()
    guest_note = ""
    if unattributed:
        guest_note = f"""
        <div class="flag flag-info">{unattributed} order{"s" if
          unattributed != 1 else ""} carry no Shopify customer id, so they
          belong to no contact here. They are real revenue and they are in
          your books &mdash; they are simply not attributable to a person.</div>"""

    return f"""
    <div class="sub">Everyone you do business with, and what each one is
      actually worth once cost of goods is taken off.</div>
    {guest_note}
    <div class="section">
      <div class="section-head"><h2>Contacts</h2>
        <span class="tag">{len(summaries)}</span></div>
      {overflow}
      <table class="table-hover">
        <thead><tr>
          <th data-sort="text">Name</th>
          <th data-sort="text">Stage</th>
          <th class="num" data-sort="num">Orders</th>
          <th class="num" data-sort="num">Lifetime revenue</th>
          <th class="num" data-sort="num">Margin</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p class="hint">Margin covers only orders whose cost of goods has
        posted. An order with a line Klearing could not cost is left out
        rather than costed with a guess, and the count under a partial
        figure says how many are included.</p>
    </div>
    {_contact_add_form()}"""


def _contacts_pipeline_content():
    """Contacts grouped by stage, one column per stage.

    Read-only on purpose: a stage is a judgment, set on the contact's own
    page through the existing form, so there is no drag and drop and no
    new way to change one. Each card shows facts Klearing already holds
    (orders and lifetime revenue) beside the stage rather than folded
    into it.
    """
    summaries = crm.contact_summaries()
    if not summaries:
        return ui.empty_state(
            "No contacts to place yet",
            "Once contacts exist you can mark which ones matter. Stages are "
            "a judgment you make; order counts and revenue are already "
            "known and are shown beside them rather than folded into them.",
            action_href="/contacts?tab=all", action_label="All contacts")

    columns = ""
    for key, label, rows in crm.pipeline_board(summaries):
        if rows:
            cards = ""
            for row in rows:
                contact = row["contact"]
                orders = row["orders"]
                cards += (
                    f'<li class="board-card">'
                    f'<a href="/contacts/{contact["id"]}">'
                    f'{escape(contact["display_name"])}</a>'
                    f'<div class="meta">{orders} order'
                    f'{"" if orders == 1 else "s"}</div>'
                    f'<div class="board-amount">{money(row["revenue"])}</div>'
                    f"</li>")
        else:
            cards = ('<li class="none-note">Nobody here yet. Set a stage on '
                     "a contact's own page to place them.</li>")
        columns += f"""
        <section class="board-col" aria-label="{escape(label, quote=True)}">
          <div class="board-head"><h2>{escape(label)}</h2>
            <span class="tag">{len(rows)}</span></div>
          <ul class="board-list">{cards}</ul>
        </section>"""

    return f"""
    <div class="sub">Where each contact sits, as a judgment you made. Order
      counts and revenue are facts Klearing already holds &mdash; they sit
      beside the stage rather than deciding it.</div>
    <div class="board">{columns}</div>"""


def _contact_add_form():
    return """
    <div class="section">
      <div class="section-head"><h2>Add a contact</h2></div>
      <p class="hint">For someone who is not a Shopify customer &mdash; a
        wholesale buyer, a sponsor, anyone you invoice directly. Shopify
        customers arrive on their own with the sync.</p>
      <form method="post" action="/contacts/create" class="field-row">
        <label class="grow">Name
          <input type="text" name="display_name" maxlength="200" required></label>
        <label class="grow">Email
          <input type="email" name="email" maxlength="254"></label>
        <label>Phone
          <input type="text" name="phone" maxlength="50" class="w-md"></label>
        <button class="approve" type="submit">Add</button>
      </form>
    </div>"""


def _contact_stage_form(contact_id, stage):
    options = "".join(
        f'<option value="{key}"{" selected" if key == stage else ""}>'
        f"{escape(label)}</option>"
        for key, label in crm.STAGE_LABELS)
    clear = ""
    if stage:
        clear = (f'<form method="post" action="/contacts/{contact_id}/stage" '
                 'class="inline-form"><input type="hidden" name="stage" '
                 'value=""><button class="check" type="submit">'
                 "Clear stage</button></form>")
    return f"""
    <div class="section">
      <div class="section-head"><h2>Stage</h2>{_stage_pill(stage)}</div>
      <p class="hint">A judgment about how much this contact matters.
        Klearing already counts their orders and revenue, so the stage
        deliberately does not restate those &mdash; it records what the
        data cannot say for itself.</p>
      <form method="post" action="/contacts/{contact_id}/stage"
            class="field-row">
        <label class="grow">Set stage
          <select name="stage">{options}</select></label>
        <button class="approve" type="submit">Save</button>
      </form>
      {clear}
    </div>"""


def _contact_orders_section(graph):
    orders = graph["orders"]
    if not orders:
        note = ("No Shopify orders are linked to this contact."
                if graph["has_shopify_link"] else
                "This contact is not linked to a Shopify customer, so there "
                "is no order history to show.")
        return f"""
        <div class="section">
          <div class="section-head"><h2>Orders</h2></div>
          <div class="none-note">{note}</div>
        </div>"""

    rows = ""
    for order in orders:
        revenue = (money(_decimal_cents(order["revenue_cents"]))
                   if order["revenue_cents"] is not None
                   else '<span class="meta">not posted</span>')
        if order["margin_cents"] is None:
            margin = "&mdash;"
            margin_sort = ""
        else:
            margin = money(_decimal_cents(order["margin_cents"]))
            margin_sort = f' data-sort-value="{order["margin_cents"]}"'
        rows += f"""
        <tr>
          <td class="mixed"><a href="/shopify/order/{escape(order["order_id"], quote=True)}">
            #{escape(order["order_id"])}</a></td>
          <td class="num" data-sort="date">{escape(order["created_at"])}</td>
          <td class="mixed">{escape(order["financial_status"])}</td>
          <td class="num">{revenue}</td>
          <td class="num"{margin_sort}>{margin}</td>
        </tr>"""

    totals = graph["totals"]
    uncosted_note = ""
    if totals["uncosted"]:
        uncosted_note = (
            f'<p class="hint">{len(totals["uncosted"])} of '
            f'{totals["orders_with_revenue"]} orders have no cost of goods '
            "posted, so they are excluded from margin. Record what the "
            "stock cost on Inventory and they will be included.</p>")

    return f"""
    <div class="section">
      <div class="section-head"><h2>Orders</h2>
        <span class="tag">{len(orders)}</span></div>
      <table class="table-hover">
        <thead><tr>
          <th data-sort="text">Order</th>
          <th class="num" data-sort="date">Date</th>
          <th data-sort="text">Status</th>
          <th class="num" data-sort="num">Revenue</th>
          <th class="num" data-sort="num">Margin</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {uncosted_note}
    </div>"""


def _contact_invoices_section(graph):
    invoices = graph["invoices"]
    if not invoices:
        return f"""
        <div class="section">
          <div class="section-head"><h2>Invoices</h2></div>
          <div class="none-note">No invoices for this contact yet.
            <a href="/invoices/new">Create one</a>.</div>
        </div>"""

    rows = "".join(
        f'<tr><td class="mixed"><a href="/invoices/{row["id"]}">'
        f'{escape(row["number"])}</a></td>'
        f'<td class="num" data-sort="date">{escape(row["issue_date"])}</td>'
        f'<td class="num" data-sort="date">{escape(row["due_date"])}</td>'
        f'<td class="mixed">{escape(row["status"])}</td>'
        f'<td class="num">{money(row["total"])}</td>'
        f'<td class="num" data-sort-value="{row["balance_cents"]}">'
        f'{money(row["balance"])}</td></tr>'
        for row in invoices)

    return f"""
    <div class="section">
      <div class="section-head"><h2>Invoices</h2>
        <span class="tag">{len(invoices)}</span>
        <span class="tag">{money(graph["invoiced_outstanding"])} outstanding</span>
      </div>
      <table class="table-hover">
        <thead><tr>
          <th data-sort="text">Number</th>
          <th class="num" data-sort="date">Issued</th>
          <th class="num" data-sort="date">Due</th>
          <th data-sort="text">Status</th>
          <th class="num" data-sort="num">Total</th>
          <th class="num" data-sort="num">Balance</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _decimal_cents(cents):
    """Cents to Decimal for display. crm.py already does this for its own
    totals; per-order figures stay in cents until here so sorting can use
    the integer."""
    return Decimal(cents) / 100


def render_contact_detail(contact_id, message=None, error=None):
    graph = crm.contact_graph(contact_id)
    if graph is None:
        return render_shell("Contacts", "", ui.empty_state(
            "No such contact",
            "That contact does not exist. It may have been deleted.",
            action_href="/contacts", action_label="All contacts"),
            message, error, wide=True)

    contact = graph["contact"]
    totals = graph["totals"]

    margin_stat = ui.stat(
        "Margin", money(totals["margin"]) if totals["margin"] is not None
        else "&mdash;",
        sub=(f'{totals["orders_costed"]} of {totals["orders_with_revenue"]} '
             "orders costed" if totals["margin"] is not None
             else "no cost of goods posted yet"))

    identity = []
    if contact.get("email"):
        identity.append(f'<span class="tag">{escape(contact["email"])}</span>')
    if contact.get("phone"):
        identity.append(f'<span class="tag">{escape(contact["phone"])}</span>')
    if contact.get("shopify_customer_id"):
        identity.append('<span class="tag">Shopify customer</span>')

    aliases = graph["aliases"]
    alias_block = ""
    if aliases:
        alias_rows = "".join(
            f'<tr><td>{escape(row["alias_text"])}</td>'
            f'<td class="mixed">{escape(row["source"])}</td></tr>'
            for row in aliases)
        alias_block = ui.disclosure(
            f"Known by {len(aliases)} other name"
            f'{"s" if len(aliases) != 1 else ""}',
            '<table class="table-hover"><thead><tr><th>Name</th>'
            "<th>Where from</th></tr></thead><tbody>"
            f"{alias_rows}</tbody></table>",
            note="used to match bank descriptors back to this contact")

    about_tags = (" ".join(identity) if identity
                  else '<span class="hint">No email or phone saved.</span>')
    content = f"""
    <p class="hint"><a href="/contacts">All contacts</a></p>
    <div class="section">
      <div class="section-head"><h2>{escape(contact["display_name"])}</h2>
        {_stage_pill(graph["stage"])}</div>
      <div class="stat-grid mt-4">
        {ui.stat("Orders", str(totals["orders"]))}
        {ui.stat("Revenue", money(totals["revenue"]))}
        {margin_stat}
        {ui.stat("Invoiced outstanding", money(graph["invoiced_outstanding"]))}
      </div>
    </div>
    <div class="detail-grid">
      <div class="detail-main">
        {_contact_orders_section(graph)}
        {_contact_invoices_section(graph)}
        <div class="section">
          <div class="section-head"><h2>Notes</h2></div>
          <form method="post" action="/contacts/{contact_id}/notes">
            <label class="grow">Notes
              <textarea name="notes" rows="4" maxlength="4000"
                >{escape(contact.get("notes") or "")}</textarea></label>
            <p class="mt-4"><button class="approve" type="submit">Save notes</button></p>
          </form>
        </div>
      </div>
      <aside class="detail-side" aria-label="About this contact">
        <div class="section">
          <div class="section-head"><h2>About</h2></div>
          <div class="detail-tags">{about_tags}</div>
          {alias_block}
        </div>
        {_contact_stage_form(contact_id, graph["stage"])}
      </aside>
    </div>"""

    return render_shell("Contacts", "", content, message, error, wide=True)


def render_contacts_page(tab="all", message=None, error=None):
    active = tab if tab in dict(CONTACT_TABS) else "all"
    body = (_contacts_all_content() if active == "all"
            else _contacts_pipeline_content())
    return render_shell("Contacts", "", _contacts_nav(active) + body,
                        message, error, wide=True)


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page(tab: str = "all", msg: str = None, err: str = None):
    db.init_db()
    return render_contacts_page(tab, message=msg, error=err)


@app.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_detail_page(contact_id: int, msg: str = None, err: str = None):
    db.init_db()
    return render_contact_detail(contact_id, message=msg, error=err)


@app.post("/contacts/create")
def contact_create(display_name: str = Form(""), email: str = Form(""),
                   phone: str = Form("")):
    db.init_db()
    name = (display_name or "").strip()
    if not name:
        return _redirect_contacts(err="A contact needs a name.")
    address = (email or "").strip() or None
    if address and db.get_contact_by_email(address):
        return _redirect_contacts(
            err=f"A contact with the email {address} already exists.")
    contact_id = db.create_contact(name, email=address,
                                   phone=(phone or "").strip() or None)
    db.add_contact_alias(contact_id, name, "manual")
    return _redirect_contact(contact_id)(msg=f"Added {name}.")


@app.post("/contacts/{contact_id}/stage")
def contact_set_stage(contact_id: int, stage: str = Form("")):
    db.init_db()
    if not db.get_contact(contact_id):
        return _redirect_contacts(err="That contact no longer exists.")
    redirect = _redirect_contact(contact_id)
    if not stage:
        db.clear_contact_stage(contact_id)
        return redirect(msg="Stage cleared.")
    try:
        db.set_contact_stage(contact_id, stage)
    except ValueError as exc:
        return redirect(err=str(exc))
    return redirect(msg=f"Stage set to {crm.stage_label(stage)}.")


@app.post("/contacts/{contact_id}/notes")
def contact_save_notes(contact_id: int, notes: str = Form("")):
    db.init_db()
    if not db.get_contact(contact_id):
        return _redirect_contacts(err="That contact no longer exists.")
    db.update_contact(contact_id, notes=notes)
    return _redirect_contact(contact_id)(msg="Notes saved.")


@app.get("/review", response_class=HTMLResponse)
def review_page(tab: str = "transactions", msg: str = None, err: str = None,
                dup: int = None, cat: str = None, review: int = None):
    db.init_db()
    warn = None
    if dup is not None:
        warn = ("This transaction looks like a duplicate — choose Skip or "
                "Post anyway on its row.")
    # ?review=<id> reopens the drawer on the item that was being decided
    # — so a failed post lands the user back where they were with the
    # error, rather than on a flat table having lost their place.
    return render_review_page(tab, message=msg or warn, error=err,
                              dup_item_id=dup, dup_category=cat,
                              open_item_id=review)


# Old paths kept as permanent redirects. These have been bookmarked and
# typed for months; a redesign that reorganises navigation has no business
# turning a URL somebody knows into a 404.
_MOVED = {
    "/pending": "/review?tab=transactions",
    "/followups": "/review?tab=followups",
    # "/invoices" is deliberately NOT here any more. It used to redirect to
    # the Wave reminder queue; it is now Klearing's own invoicing page. The
    # queue it used to point at is still reachable at its real URL and from
    # the Review sub-nav, so nothing became a 404 -- the path was reused for
    # a feature that has a much better claim to it.
    "/connections": "/settings?tab=connections",
    "/automation": "/settings?tab=automation",
    "/logs": "/settings?tab=logs",
    "/tools": "/tools/data-health",
}


def _moved_to(target):
    def handler(request: Request):
        # Carry the original query string across, so a link like
        # /pending?msg=Approved still shows its banner after the hop.
        query = request.url.query
        sep = "&" if "?" in target else "?"
        url = f"{target}{sep}{query}" if query else target
        return RedirectResponse(url, status_code=308)
    return handler


for _old_path, _new_path in _MOVED.items():
    app.get(_old_path, include_in_schema=False)(_moved_to(_new_path))


def _clean_range(value):
    return value if value in ("8w", "12w", "12mo") else "8w"


@app.get("/summary", response_class=HTMLResponse)
def summary(start: str = None, end: str = None, range: str = "8w",
            msg: str = None, err: str = None):
    db.init_db()
    default_start, default_end = default_range()
    start_date = clean_date(start, default_start)
    end_date = clean_date(end, default_end)

    # Approved totals belong to the active business (the switcher in
    # the header changes this); pending items aren't attributed yet.
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    approved = db.list_items_in_range("approved", start_date, end_date,
                                      business_id=active_id)
    pending = db.list_items_in_range("pending", start_date, end_date)

    # The immediately-preceding period of equal length, for the approved
    # totals' vs-last-period comparison. Only surfaced when that prior
    # period actually had activity (an empty prior period would compare
    # every figure against $0 and read as a meaningless "New").
    s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    span_days = (e - s).days
    prior_end = s - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span_days)
    prior_approved = db.list_items_in_range(
        "approved", prior_start.isoformat(), prior_end.isoformat(),
        business_id=active_id)
    prior_summary = summarize(prior_approved)
    prior_label = f"{prior_start.strftime('%b %d')} – {prior_end.strftime('%b %d')}"
    if not summary_has_rows(prior_summary):
        prior_summary = None

    # Gusto payroll for the range (optional): rendered as its own row in
    # the expense table, never mixed into the Money-out totals — those
    # come from approved local transactions, and payroll comes live from Gusto.
    gusto_payroll = None
    gusto_note = ""
    if not gusto_client.is_configured():
        gusto_note = ('<div class="none-note">Connect Gusto on the '
                      '<a href="/settings?tab=connections">Connections page</a> to '
                      "include payroll here automatically.</div>")
    else:
        try:
            runs = gusto_client.payrolls_in_range(start_date, end_date)
        except Exception:
            runs = None
            gusto_note = ('<div class="none-note">Gusto couldn\'t be '
                          "reached — payroll isn't shown for this "
                          'range. <a href="/settings?tab=connections">Re-test '
                          "connection</a>.</div>")
        if runs:
            gusto_payroll = {
                "total": sum(r["gross"] + r["employer_taxes"]
                             for r in runs),
                "count": len(runs)}
            gusto_note = ('<div class="none-note">Payroll comes live '
                          "from Gusto (gross + employer taxes), shown "
                          "for the full picture — informational, not "
                          "recorded in your books, and not counted in the "
                          "Money out total above.</div>")

    return render_summary_page(
        start_date, end_date, summarize(approved), summarize(pending),
        trend_range=_clean_range(range),
        prior_summary=prior_summary, prior_label=prior_label,
        gusto_payroll=gusto_payroll, gusto_note=gusto_note,
        message=msg, error=err,
    )


@app.get("/summary/category", response_class=HTMLResponse)
def summary_category_detail(category: str, direction: str, status: str,
                            start: str = None, end: str = None,
                            msg: str = None, err: str = None):
    db.init_db()
    if direction not in ("income", "expense", "transfer") or status not in ("approved", "pending"):
        return RedirectResponse("/summary", status_code=303)
    default_start, default_end = default_range()
    start_date = clean_date(start, default_start)
    end_date = clean_date(end, default_end)
    items, total_count = category_items(category, direction, status,
                                        start_date, end_date)
    return render_category_detail_page(category, direction, status,
                                       start_date, end_date, items,
                                       total_count, message=msg, error=err)


@app.get("/export/csv")
def export_csv(start: str = None, end: str = None):
    """Approved transactions in the range as a downloadable CSV."""
    db.init_db()
    default_start, default_end = default_range()
    start_date = clean_date(start, default_start)
    end_date = clean_date(end, default_end)
    items = approved_for_export(start_date, end_date)
    csv_text = build_transactions_csv(items)
    filename = f"klearing-transactions-{start_date}-to-{end_date}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/accountant")
def export_accountant(start: str = None, end: str = None):
    """One timestamped zip for the accountant: the transactions CSV plus
    a P&L-style summary text file for the selected range."""
    db.init_db()
    default_start, default_end = default_range()
    start_date = clean_date(start, default_start)
    end_date = clean_date(end, default_end)
    items = approved_for_export(start_date, end_date)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_text = build_transactions_csv(items)
    summary_text = build_summary_txt(start_date, end_date, items, generated_at)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"transactions-{start_date}-to-{end_date}.csv",
                        csv_text)
        bundle.writestr(f"summary-{start_date}-to-{end_date}.txt",
                        summary_text)
    filename = f"klearing-accountant-{stamp}.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(msg: str = None, err: str = None):
    db.init_db()
    rows, threshold_pct = vendor_stats()
    active = business_registry.active_business()
    subscriptions = recurring.detect_subscriptions(
        business_id=active["id"] if active else None)
    return render_vendors_page(rows, threshold_pct,
                               subscriptions=subscriptions,
                               message=msg, error=err)


@app.get("/vendors/detail", response_class=HTMLResponse)
def vendor_detail_page(vendor: str, msg: str = None, err: str = None):
    db.init_db()
    items, total_count = vendor_expense_items(vendor)
    return render_vendor_detail_page(vendor, items, total_count,
                                     message=msg, error=err)


@app.post("/vendors/detail/annotate/{item_id}")
def vendor_detail_annotate(item_id: int, vendor: str = Form(...),
                           notes: str = Form(""),
                           evidence_type: str = Form("")):
    """Edit the note/evidence tag on an already-approved transaction —
    the one place besides the Pending approve form where these fields
    can be set, since Vendor Detail is the only existing per-transaction
    view of approved items. Survives untouched otherwise: this never
    changes status, category, or any posted ledger entry."""
    db.init_db()
    item = db.get_item(item_id)
    redirect_url = f"/vendors/detail?vendor={quote(vendor)}"
    if item is None or item["status"] != "approved":
        return RedirectResponse(
            f"{redirect_url}&err={quote('That transaction is no longer approved.')}",
            status_code=303)
    chosen_evidence = (evidence_type or "").strip()
    if chosen_evidence not in accountant_export.EVIDENCE_LABELS:
        chosen_evidence = None
    db.set_notes(item_id, (notes or "").strip()[:500])
    db.set_evidence_type(item_id, chosen_evidence)
    return RedirectResponse(
        f"{redirect_url}&msg={quote('Saved.')}", status_code=303)


@app.get("/followups", response_class=HTMLResponse)
def followups_page(msg: str = None, err: str = None):
    db.init_db()
    items = db.list_quote_followups(status="pending_review")
    return render_followups_page(items, message=msg, error=err)


@app.post("/followups/send/{followup_id}")
def send_followup(followup_id: int, subject: str = Form(""), body: str = Form("")):
    item = db.get_quote_followup(followup_id)
    if item is None or item["status"] != "pending_review":
        return _redirect_followups(err="That follow-up is no longer pending.")

    subject = (subject or "").strip() or (item["draft_subject"] or "")
    body = (body or "").strip() or (item["draft_body"] or "")
    if not subject or not body:
        return _redirect_followups(err="Subject and draft can't be empty.")

    if not email_utils.send_email(subject, body, to=item["email"]):
        return _redirect_followups(err=EMAIL_NOT_CONFIGURED)

    # Persist exactly what was sent (including any edits made on the page).
    db.set_quote_followup_status(followup_id, "sent",
                                 draft_subject=subject, draft_body=body)
    return _redirect_followups(msg=f'Follow-up sent to {item["email"]}.')


@app.post("/followups/skip/{followup_id}")
def skip_followup(followup_id: int, subject: str = Form(""), body: str = Form("")):
    item = db.get_quote_followup(followup_id)
    if item is None or item["status"] != "pending_review":
        return _redirect_followups(err="That follow-up is no longer pending.")

    # Keep any draft edits so nothing is lost if it's later revisited.
    db.set_quote_followup_status(
        followup_id, "skipped",
        draft_subject=(subject or "").strip() or None,
        draft_body=(body or "").strip() or None,
    )
    return _redirect_followups(msg=f'Skipped follow-up for {item["email"]}. No email sent.')


# The invoice-reminder page that used to live here was folded into
# /review?tab=invoices by the Phase 4 navigation consolidation, and its
# handler was left behind calling a render function that no longer existed
# -- unreachable (the /invoices redirect was registered first) but a
# NameError waiting for anyone who changed route order. Removed rather than
# repaired: the queue it served is rendered by _review_invoices_content now.
# The POST endpoints below are still live and still used by that queue.


@app.post("/invoices/send/{reminder_id}")
def send_invoice_reminder(reminder_id: int, subject: str = Form(""),
                          body: str = Form("")):
    item = db.get_invoice_reminder(reminder_id)
    if item is None or item["status"] != "pending_review":
        return _redirect_invoice_reminders(err="That reminder is no longer pending.")

    subject = (subject or "").strip() or (item["draft_subject"] or "")
    body = (body or "").strip() or (item["draft_body"] or "")
    if not subject or not body:
        return _redirect_invoice_reminders(err="Subject and draft can't be empty.")

    if not email_utils.send_email(subject, body, to=item["customer_email"]):
        return _redirect_invoice_reminders(err=EMAIL_NOT_CONFIGURED)

    db.set_invoice_reminder_status(
        reminder_id, "sent", draft_subject=subject, draft_body=body,
        last_sent_at=datetime.now(timezone.utc).isoformat(),
    )
    return _redirect_invoice_reminders(msg=f'Reminder sent to {item["customer_email"]}.')


@app.post("/invoices/skip/{reminder_id}")
def skip_invoice_reminder(reminder_id: int, subject: str = Form(""),
                          body: str = Form("")):
    item = db.get_invoice_reminder(reminder_id)
    if item is None or item["status"] != "pending_review":
        return _redirect_invoice_reminders(err="That reminder is no longer pending.")

    # last_sent_at doubles as the "last handled" timestamp so the
    # 7-day redraft clock applies to skips too.
    db.set_invoice_reminder_status(
        reminder_id, "skipped",
        draft_subject=(subject or "").strip() or None,
        draft_body=(body or "").strip() or None,
        last_sent_at=datetime.now(timezone.utc).isoformat(),
    )
    return _redirect_invoice_reminders(
        msg=f'Skipped. It will be redrafted next week if Invoice '
            f'#{item["invoice_number"]} is still overdue.'
    )


@app.get("/insights", response_class=HTMLResponse)
def insights_page(msg: str = None, err: str = None, range: str = "8w",
                  tab: str = "overview"):
    db.init_db()
    snapshot_row = db.get_latest_ga4_snapshot()
    financial = (financial_insights.financial_snapshot()
                 if tab == "financial" else None)
    metrics = None
    metrics_failed = False
    if tab not in {"financial", "marketing"}:
        try:
            metrics = insight_metrics.get_insight_metrics()
        except Exception:
            metrics_failed = True
    return render_insights_page(snapshot_row, message=msg, error=err,
                                trend_range=_clean_range(range), tab=tab,
                                financial=financial, metrics=metrics,
                                metrics_failed=metrics_failed)


@app.get("/api/insights/cash")
def insights_cash():
    """Combine selected-account cash with approved-ledger burn, if available."""
    db.init_db()
    config = cash_alert.get_config()
    if not config["cash_account_ids"]:
        return {
            "state": "unconfigured", "balance_cents": None,
            "runway_days": None,
            "note": "Select business-cash accounts in Settings to add runway.",
        }
    try:
        balance_cents = round(cash_alert.current_cash_balance(
            config["cash_account_ids"]) * 100)
    except Exception:
        return {
            "state": "error", "balance_cents": None, "runway_days": None,
            "note": "Cash accounts could not be checked right now. Approved-ledger insights are still available.",
        }
    daily_burn = financial_insights.financial_snapshot()["daily_burn_cents"]
    runway = max(0, balance_cents) / daily_burn if daily_burn > 0 else None
    return {
        "state": "ok", "balance_cents": balance_cents,
        "runway_days": runway,
        "note": "Cash uses selected accounts; burn uses the trailing 90 days of native Ledger expenses.",
    }


@app.get("/static/insights/financial.js")
def financial_insights_javascript():
    return FileResponse(paths.resource_path("frontend/insights/financial.js"),
                        media_type="application/javascript")


# ============================================================= Growth tab
#
# Revenue and profitability views. All deterministic: monthly income
# trends, GA4 conversion-by-channel, and a directional margin. No AI, no
# new external calls beyond the read-only GA4 snapshot already taken.

GROWTH_MONTHS = 6
GROWTH_MARGIN_DAYS = 90


def monthly_income_series(months, business_id):
    """(labels, [(category, [monthly totals])]) of approved income over
    the trailing calendar months, biggest earner first."""
    windows = _month_windows(months)
    labels = [w[0] for w in windows]
    categories = {}
    for idx, (_label, start, end) in enumerate(windows):
        for item in db.list_items_in_range("approved", start, end,
                                            business_id=business_id):
            if item["direction"] != "income":
                continue
            arr = categories.setdefault(item["suggested_category"],
                                        [0.0] * len(windows))
            arr[idx] += item["amount"]
    ordered = sorted(categories.items(), key=lambda kv: -sum(kv[1]))
    return labels, ordered


def directional_margin(days, business_id):
    """A rough income-minus-materials gross margin over the trailing
    window, or None if there's no income. NOT per-sale COGS accounting —
    labelled directional in the UI."""
    today = date.today()
    lo = (today - timedelta(days=days - 1)).isoformat()
    hi = today.isoformat()
    items = db.list_items_in_range("approved", lo, hi, business_id=business_id)
    income = sum(i["amount"] for i in items if i["direction"] == "income")
    cogs = sum(i["amount"] for i in items
               if i["direction"] == "expense"
               and category_kind(i["suggested_category"]) == "material")
    if income <= 0:
        return None
    return {"income": income, "cogs": cogs,
            "margin": (income - cogs) / income * 100, "days": days}


def conversion_by_source(snapshot):
    """(rows, primary_event) where each row is a channel with sessions,
    conversions, and `rate` = conversions per 100 sessions (GA4 eventCount
    isn't session-scoped, so this is a per-100-sessions ratio that can
    exceed 100 — labelled honestly as such in the UI, never as a bounded
    'share of sessions'). (None, None) when the snapshot has no channel
    breakdown (old snapshot / GA4 off)."""
    by_source = snapshot.get("windows", {}).get("30", {}).get("by_source")
    if not by_source:
        return None, None
    rows = []
    for source in by_source:
        sessions = source.get("sessions", 0)
        conversions = source.get("conversions", 0)
        rows.append({
            "channel": source.get("channel", "(unassigned)"),
            "sessions": sessions, "conversions": conversions,
            "rate": (conversions / sessions * 100) if sessions > 0 else 0.0,
        })
    rows.sort(key=lambda r: -r["rate"])
    return rows, snapshot.get("primary_conversion_event")


def _growth_focus(cat_series, conv_rows, primary_event, margin):
    """2-3 deterministic pattern observations (never AI advice)."""
    focus = []
    # Fastest-growing income category (first non-zero month vs last).
    best, best_delta = None, None
    for category, values in cat_series:
        first = next((v for v in values if v > 0), 0)
        if first <= 0 or len([v for v in values if v > 0]) < 2:
            continue
        delta = (values[-1] - first) / first * 100
        if best_delta is None or delta > best_delta:
            best, best_delta = category, delta
    if best and best_delta is not None and best_delta > 5:
        focus.append(f"{escape(best)} grew fastest over the last "
                     f"{GROWTH_MONTHS} months ({best_delta:+.0f}%).")
    # Highest-converting channel, flagged if it also has the least traffic.
    with_sessions = [r for r in (conv_rows or []) if r["sessions"] > 0
                     and r["conversions"] > 0]
    if with_sessions:
        top = with_sessions[0]  # already sorted by rate desc
        statement = (f"{escape(top['channel'])} converts best on "
                     f"{escape(primary_event or 'conversions')} "
                     f"({top['rate']:.1f} per 100 sessions).")
        least = min(with_sessions, key=lambda r: r["sessions"])
        if top is least and len(with_sessions) > 1:
            statement += " It also has the least traffic — may be under-invested."
        focus.append(statement)
    # Directional margin note.
    if margin is not None:
        focus.append(f"Directional gross margin is {margin['margin']:.0f}% "
                     f"over the last {margin['days']} days.")
    return focus


def _shopify_products(orders):
    products = {}
    for order in orders:
        try:
            items = json.loads(order["line_items_json"])
        except (TypeError, ValueError):
            items = []
        for item in items:
            product_id = str(item.get("product_id") or "")
            key = product_id or item.get("title") or "Unknown product"
            entry = products.setdefault(key, {
                "id": product_id,
                "title": item.get("title") or "Unknown product",
                "units": 0,
                "revenue": Decimal(0),
                "refund": Decimal(0),
            })
            quantity = int(item.get("quantity") or 0)
            try:
                price = Decimal(str(item.get("price") or 0))
            except (InvalidOperation, TypeError, ValueError):
                price = Decimal(0)
            line_revenue = price * quantity
            entry["units"] += quantity
            entry["revenue"] += line_revenue
            entry["refund"] += order["refund_total"] * (
                line_revenue / max(order["subtotal_price"], Decimal("0.01")))
    return products


def _shopify_range(period):
    today = date.today()
    if period == "last_month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    elif period == "last_90_days":
        end, start = today + timedelta(days=1), today - timedelta(days=89)
    else:
        start, end = today.replace(day=1), today + timedelta(days=1)
    return start.isoformat(), end.isoformat(), (end - start).days


def render_shopify_page(period="this_month", status="all", source="all", message=None, error=None):
    db.init_db()
    if not shopify_client.is_configured():
        content = '<div class="empty"><p><strong>Shopify is not connected.</strong></p><p>Connect your read-only Admin API token to sync storefront data locally.</p><a class="btn-link primary" href="/settings?tab=connections">Open Connections</a></div>'
        return render_shell("Shopify", "", content, message, error)
    start, end, days = _shopify_range(period)
    orders = db.list_shopify_orders(start, end)
    if status != "all":
        if status == "refunded": orders = [row for row in orders if row["refund_total"] > 0]
        else: orders = [row for row in orders if row["financial_status"] == status]
    if source != "all": orders = [row for row in orders if row["source"] == source]
    prior_start = (date.fromisoformat(start) - timedelta(days=days)).isoformat()
    prior = db.list_shopify_orders(prior_start, start)
    gross = sum(row["total_price"] for row in orders); refunds = sum(row["refund_total"] for row in orders); net = gross - refunds
    prior_net = sum(row["total_price"] - row["refund_total"] for row in prior)
    product_rows = sorted(_shopify_products(orders).values(), key=lambda row: -row["revenue"])[:10]
    product_table = "".join(f'<tr><td><a href="/shopify/product/{quote(row["id"])}">{escape(row["title"][:30])}</a></td><td class="num">{row["units"]}</td><td class="num">{money(row["revenue"])}</td><td class="num">{row["revenue"] / gross * 100 if gross else 0:.0f}%</td><td class="num">{row["refund"] / row["revenue"] * 100 if row["revenue"] else 0:.0f}%</td></tr>' for row in product_rows) or '<tr><td colspan="5" class="mixed">No product sales in this range.</td></tr>'
    customers = db.list_shopify_customers(); repeat = sum(1 for row in orders if int(row.get("customer_id") or 0) and any(c["customer_id"] == row["customer_id"] and c["orders_count"] > 1 for c in customers))
    unfulfilled = [row for row in orders if row["fulfillment_status"] in ("", "unfulfilled", "partial")]
    daily = []
    for offset in range(days - 1, -1, -1):
        day = (date.fromisoformat(end) - timedelta(days=offset + 1)).isoformat()
        daily.append(sum(row["total_price"] - row["refund_total"] for row in db.list_shopify_orders(day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())))
    filters = f'''<form class="range-form" method="get" action="/shopify"><select name="period"><option value="this_month">This month</option><option value="last_month">Last month</option><option value="last_90_days">Last 90 days</option></select><select name="status"><option value="all">All statuses</option><option value="paid">Paid</option><option value="pending">Pending</option><option value="refunded">Refunded</option></select><select name="source"><option value="all">All sources</option><option value="online_store">Online store</option><option value="pos">POS</option><option value="draft_order">Draft order</option></select><button class="check" type="submit">Apply</button><a class="btn-link" href="/shopify/export?period={period}">Export CSV</a></form>'''
    order_rows = "".join(f'<tr><td><a href="/shopify/order/{quote(row["order_id"])}">#{escape(row["order_id"])}</a></td><td>{escape(row["created_at"][:10])}</td><td>{escape(row["customer_name"] or row["customer_email"] or "Guest")}</td><td class="num">{money(row["total_price"] - row["refund_total"])}</td><td>{escape(row["financial_status"])} / {escape(row["fulfillment_status"] or "unfulfilled")}</td></tr>' for row in orders[:20]) or '<tr><td colspan="5" class="mixed">No orders in this range.</td></tr>'
    content = f'''<div class="sub">Storefront data is synced locally during the daily check. <form method="post" action="/shopify/sync" class="inline-form"><button class="btn-link" type="submit">Sync now</button></form></div>{filters}
    <div class="section"><div class="section-head"><h2>Revenue overview</h2></div><div class="totals"><div><div class="label">Gross sales</div><div class="value in">{money(gross)}</div></div><div><div class="label">Refunds</div><div class="value out">{money(refunds)}</div></div><div><div class="label">Net revenue</div><div class="value">{money(net)}</div><div class="meta">{escape(pct_change_text(net, prior_net))} vs prior period</div></div><div><div class="label">Orders</div><div class="value">{len(orders)}</div></div></div>{charts.sparkline(daily, zero_baseline=True, aria="Daily Shopify net revenue")}</div>
    <div class="section"><div class="section-head"><h2>Product performance</h2><span class="tag">sortable</span></div><table class="table-hover"><thead><tr><th data-sort="text">Product</th><th class="num" data-sort="num">Units</th><th class="num" data-sort="num">Revenue</th><th class="num" data-sort="num">Share</th><th class="num" data-sort="num">Refund rate</th></tr></thead><tbody>{product_table}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Customer analytics</h2></div><div class="totals"><div><div class="label">Customers</div><div class="value">{len({row["customer_id"] for row in orders if row["customer_id"]})}</div></div><div><div class="label">Returning orders</div><div class="value">{repeat}</div></div></div><table><thead><tr><th>Top customers</th><th class="num" data-sort="num">Lifetime spend</th></tr></thead><tbody>{''.join(f'<tr><td>{escape((row["first_name"] + " " + row["last_name"]).strip() or row["email"] or "Customer")}</td><td class="num">{money(row["total_spent"])}</td></tr>' for row in customers[:10]) or '<tr><td colspan="2" class="mixed">No customer data yet.</td></tr>'}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Fulfillment</h2></div><div class="none-note">{len(unfulfilled)} unfulfilled order(s){' — review orders older than 3 days.' if any((date.today() - date.fromisoformat(row["created_at"][:10])).days > 3 for row in unfulfilled) else ''}</div></div>
    <div class="section"><div class="section-head"><h2>Recent orders</h2></div><table><thead><tr><th data-sort="text">Order</th><th data-sort="date">Date</th><th data-sort="text">Customer</th><th class="num" data-sort="num">Net</th><th data-sort="text">Status</th></tr></thead><tbody>{order_rows}</tbody></table></div>'''
    return render_shell("Shopify", "", content, message, error, wide=True)


@app.get("/shopify", response_class=HTMLResponse)
def shopify_page(period: str = "this_month", status: str = "all", source: str = "all", msg: str = None, err: str = None):
    return render_shopify_page(period, status, source, msg, err)


@app.post("/shopify/sync")
def shopify_sync():
    try:
        synced = shopify_client.sync(); conn_mark_tested("shopify")
        message = (f"Synced {synced['orders']} orders, {synced['products']} products, "
                   f"and {synced['customers']} customers.")
        return RedirectResponse(f"/shopify?msg={quote(message)}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f'/shopify?err={quote(str(exc))}', status_code=303)


@app.get("/shopify/order/{order_id}", response_class=HTMLResponse)
def shopify_order(order_id: str):
    """One order, as a document rather than a JSON dump.

    This page used to print line_items_json raw inside a <pre> — the only
    place in the app that showed a user the shape of the database. It is
    reachable from a table link, so people do land here.
    """
    row = db.get_shopify_order(order_id)
    if not row:
        return RedirectResponse("/shopify?err=Order+not+found", status_code=303)

    customer = row["customer_name"] or row["customer_email"] or "Guest"
    net = row["total_price"] - row["refund_total"]
    status = (row["financial_status"] or "").lower()
    tone = {"paid": "ok", "refunded": "bad", "voided": "bad",
            "pending": "warn", "partially_refunded": "warn"}.get(status, "")
    fulfilled = row["fulfillment_status"] or "unfulfilled"

    try:
        line_items = json.loads(row["line_items_json"] or "[]")
    except (TypeError, ValueError):
        line_items = []

    if line_items:
        item_rows = ""
        for item in line_items:
            qty = item.get("quantity") or 0
            price = float(item.get("price") or 0)
            title = item.get("title") or item.get("name") or "Item"
            variant = item.get("variant_title")
            label = f"{title} ({variant})" if variant else title
            item_rows += (
                f'<tr><td class="vendor-name">{escape(str(label))}</td>'
                f'<td class="num">{qty}</td>'
                f'<td class="num">{money(price)}</td>'
                f'<td class="num">{money(price * qty)}</td></tr>')
        items_html = f"""
        <table class="table-hover">
          <thead><tr><th data-sort="text">Item</th>
            <th class="num" data-sort="num">Qty</th>
            <th class="num" data-sort="num">Unit</th>
            <th class="num" data-sort="num">Line total</th></tr></thead>
          <tbody>{item_rows}</tbody>
        </table>"""
    else:
        items_html = ('<div class="none-note">No line items were stored '
                      "with this order.</div>")

    money_rows = [("Subtotal", row["subtotal_price"]),
                  ("Discounts", -(row["total_discounts"] or 0)),
                  ("Tax", row["total_tax"]),
                  ("Total", row["total_price"])]
    if row["refund_total"]:
        money_rows.append(("Refunded", -row["refund_total"]))
        money_rows.append(("Net", net))
    totals = "".join(
        f'<div class="stat-cell"><span class="stat-label">{escape(label)}</span>'
        f'<span class="amount">{money(value or 0)}</span></div>'
        for label, value in money_rows)

    content = f"""
    <div class="subnav-back mb-4"><a href="/shopify">All orders</a></div>
    <div class="section">
      <div class="section-head"><h2>Order #{escape(order_id)}</h2>
        <span class="tag {tone}">{escape(row["financial_status"] or "unknown")}</span>
        <span class="tag">{escape(fulfilled)}</span></div>
      <div class="dash-note">{escape(customer)} &middot;
        <span class="mono">{escape((row["created_at"] or "")[:10])}</span>
        &middot; {escape(row["source"] or "online")}</div>
      <div class="stat-grid mt-4">{totals}</div>
    </div>
    <div class="section">
      <div class="section-head"><h2>Line items</h2>
        <span class="tag">{len(line_items)}</span></div>
      {items_html}
    </div>"""
    return render_shell("Shopify", "", content, wide=True)


@app.get("/shopify/product/{product_id}", response_class=HTMLResponse)
def shopify_product(product_id: str):
    """One product, with the orders that actually contain it.

    Previously three lines and a count. The count was the interesting
    part and it was the one thing you could not act on, so the orders
    behind it are listed now.
    """
    product = db.get_shopify_product(product_id)
    if not product:
        return RedirectResponse("/shopify?err=Product+not+found",
                                status_code=303)

    matching, units, revenue = [], 0, 0.0
    for row in db.list_shopify_orders():
        try:
            items = json.loads(row["line_items_json"] or "[]")
        except (TypeError, ValueError):
            continue
        lines = [i for i in items
                 if str(i.get("product_id") or "") == product_id]
        if not lines:
            continue
        qty = sum(int(i.get("quantity") or 0) for i in lines)
        value = sum(float(i.get("price") or 0) * int(i.get("quantity") or 0)
                    for i in lines)
        units += qty
        revenue += value
        matching.append((row, qty, value))

    matching.sort(key=lambda m: m[0]["created_at"] or "", reverse=True)
    order_rows = "".join(
        f'<tr><td class="vendor-name">'
        f'<a href="/shopify/order/{escape(str(row["order_id"]), quote=True)}">'
        f'#{escape(str(row["order_id"]))}</a></td>'
        f'<td class="num mixed">{escape((row["created_at"] or "")[:10])}</td>'
        f'<td>{escape(row["customer_name"] or row["customer_email"] or "Guest")}</td>'
        f'<td class="num">{qty}</td><td class="num">{money(value)}</td></tr>'
        for row, qty, value in matching[:100])

    if matching:
        orders_html = f"""
        <table class="table-hover">
          <thead><tr><th data-sort="text">Order</th>
            <th class="num" data-sort="date">Date</th>
            <th data-sort="text">Customer</th>
            <th class="num" data-sort="num">Qty</th>
            <th class="num" data-sort="num">Value</th></tr></thead>
          <tbody>{order_rows}</tbody>
        </table>"""
        if len(matching) > 100:
            orders_html += (f'<div class="none-note">Showing the 100 most '
                            f"recent of {len(matching)}.</div>")
    else:
        orders_html = ('<div class="none-note">No locally synced order '
                       "includes this product yet.</div>")

    content = f"""
    <div class="subnav-back mb-4"><a href="/shopify">All products</a></div>
    <div class="section">
      <div class="section-head"><h2>{escape(product["title"])}</h2>
        <span class="tag">{escape(product["product_type"] or "Product")}</span></div>
      <div class="dash-note">{escape(product["vendor"] or "")}</div>
      <div class="stat-grid mt-4">
        <div class="stat-cell"><span class="stat-label">Orders</span>
          <span class="stat-value">{len(matching)}</span></div>
        <div class="stat-cell"><span class="stat-label">Units</span>
          <span class="stat-value">{units}</span></div>
        <div class="stat-cell"><span class="stat-label">Revenue</span>
          <span class="stat-value">{money(revenue)}</span></div>
      </div>
    </div>
    <div class="section">
      <div class="section-head"><h2>Orders including this product</h2></div>
      {orders_html}
    </div>"""
    return render_shell("Shopify", "", content, wide=True)


@app.get("/shopify/export")
def shopify_export(period: str = "this_month"):
    start, end, _ = _shopify_range(period); out = io.StringIO(); writer = csv.writer(out)
    writer.writerow(["order_id", "created_at", "customer_email", "customer_name", "source", "financial_status", "fulfillment_status", "subtotal_price", "total_discounts", "total_tax", "total_price", "refund_total", "net_revenue"])
    for row in db.list_shopify_orders(start, end): writer.writerow([row[key] for key in ("order_id", "created_at", "customer_email", "customer_name", "source", "financial_status", "fulfillment_status", "subtotal_price", "total_discounts", "total_tax", "total_price", "refund_total")] + [row["total_price"] - row["refund_total"]])
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=shopify-orders.csv"})


# Ledger — Klearing's canonical double-entry books (ledger.py / ledger_sync.py).
# Tax statements, projections, exports, planning costs, and financial insights
# all read this same source; the retired Wave Tax queue is archive-only.
#
# Tabs are registered as their screens land, in the order Sync, Trial balance,
# Profit & Loss, Balance sheet, Accounts. A tab that has no view yet is not
# listed — a nav link to a placeholder reads as a bug. The sub-nav hides
# itself entirely while only one view exists.
LEDGER_TABS = (
    ("sync", "Sync"),
    ("trial-balance", "Trial balance"),
    ("pnl", "Profit & Loss"),
    ("balance-sheet", "Balance sheet"),
    ("accounts", "Accounts"),
)

# Statement order, not the alphabetical or seeded order: balance-sheet
# accounts first, then the income statement.
LEDGER_ACCOUNT_TYPES = (
    ("asset", "Assets"), ("liability", "Liabilities"), ("equity", "Equity"),
    ("revenue", "Revenue"), ("expense", "Expenses"),
)

# Exactly the files ui.py's @font-face rules ask for. An allowlist rather
# than a path check because the set is fixed and small. Commit Mono ships
# as two static weights (not a variable font), hence the fourth entry.
BUNDLED_FONTS = frozenset({
    "instrument-sans.woff2", "instrument-serif.woff2",
    "commit-mono.woff2", "commit-mono-bold.woff2",
})

LEDGER_STALE_HOURS = 48


def _ledger_nav(active):
    if len(LEDGER_TABS) < 2:
        return ""
    return ui.sub_nav([(key, label, f"/ledger?tab={key}")
                       for key, label in LEDGER_TABS],
                      active, "Ledger sections")


def _ledger_stamp_line(label, stamp, never_text):
    """One freshness line, reusing the Connections .conn-tested treatment.

    Ledger timestamps are written UTC-aware (ledger.py and db.py both use
    datetime.now(timezone.utc)), so the age comparison is done in UTC rather
    than through _conn_tested_line, which compares naive datetimes and would
    be off by the local UTC offset here.
    """
    if not stamp:
        return f'<div class="conn-tested never">{escape(never_text)}</div>'
    shown = str(stamp).replace("T", " ")[:16]
    try:
        moment = datetime.fromisoformat(str(stamp))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age_hours = ((datetime.now(timezone.utc) - moment).total_seconds()
                     / 3600)
    except ValueError:
        age_hours = None
    stale = (age_hours is not None and age_hours >= LEDGER_STALE_HOURS)
    cls = " stale" if stale else ""
    suffix = " &middot; over 48 hours ago" if stale else ""
    return (f'<div class="conn-tested{cls}">{escape(label)}: '
            f'<span class="mono">{escape(shown)}</span>{suffix}</div>')


def _plural(count, singular, plural=None):
    return singular if count == 1 else (plural or singular + "s")


def _ledger_source_tile(title, posted, note, attention=None):
    """One standing-state tile: entries posted, plus what to know about it.

    A .section rather than an a.dash-card because there is no per-source
    drill-down to link to, and a card-shaped anchor would promise a click
    that does not exist (see the .row-link note in ui.py's table rules).
    """
    if attention is None:
        status = ""
    elif attention:
        status = (f'<div class="dash-flag">{attention} '
                  f'{_plural(attention, "record")} '
                  f'{_plural(attention, "needs", "need")} attention</div>')
    else:
        status = '<div class="dash-ok">Nothing skipped</div>'
    return f"""
    <div class="section">
      <div class="dash-kicker"><span>{escape(title)}</span></div>
      <div class="dash-value">{posted:,}</div>
      <div class="dash-sub">{_plural(posted, "entry", "entries")} posted</div>
      {status}
      <div class="dash-note">{note}</div>
    </div>"""


def _ledger_reason_label(reason):
    """Name a skip the same way scripts/ledger_verify.py does, so the CLI
    report and this screen never disagree about what a reason is called."""
    return str(reason).replace("_", " ")


def _ledger_exceptions_table(skips):
    if not skips:
        return ('<div class="none-note">Nothing skipped. Every source '
                "record posted.</div>")
    rows = ""
    for skip in skips:
        shown = str(skip["updated_at"] or "").replace("T", " ")[:16]
        rows += (
            f'<tr><td class="vendor-name">{escape(skip["source_reference"])}</td>'
            f'<td>{escape(str(skip["source"]).title())}</td>'
            f'<td>{escape(_ledger_reason_label(skip["reason"]))}</td>'
            f'<td>{escape(skip["detail"])}</td>'
            f'<td class="num">{escape(shown)}</td></tr>'
        )
    return f"""
    <table class="table-hover">
      <thead><tr><th data-sort="text">Source record</th><th data-sort="text">Source</th><th data-sort="text">Reason</th>
        <th>Detail</th><th class="num" data-sort="num">Last seen</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _ledger_sync_content():
    counts = db.ledger_import_counts()
    skips = db.list_ledger_source_skips()
    skip_counts = db.ledger_skip_counts()
    freshness = db.ledger_source_freshness()

    # Count only the synced sources, not counts["total"]. Invoicing posts
    # through the same source_reference idempotency guard, so its entries
    # also land in ledger_source_imports and inflate the total -- which
    # would replace this screen's empty state with four zeroed tiles for a
    # user who has written invoices but never run a sync. This screen is
    # about source sync specifically.
    source_total = (counts["plaid"] + counts["shopify_orders"]
                    + counts["shopify_payouts"] + counts["shopify_cogs"])
    if not source_total and not skips:
        return """
        <div class="sub">Post local Plaid and Shopify records to the
          double-entry ledger.</div>
        <div class="empty">
          <p><strong>Nothing posted yet</strong></p>
          <p>Sync reads the Plaid and Shopify records already stored on this
            machine and writes them as balanced journal entries. It makes no
            API calls and posts each source record only once.</p>
          <p>Use <strong>Sync now</strong> above to start.</p>
        </div>"""

    tiles = (
        _ledger_source_tile(
            "Plaid transactions", counts["plaid"],
            "Transfers and zero-amount records are skipped without a record, "
            "so they are not counted here.")
        + _ledger_source_tile(
            "Shopify orders", counts["shopify_orders"],
            "Refunded and voided orders are skipped without a record.")
        + _ledger_source_tile(
            "Shopify payouts", counts["shopify_payouts"],
            "A payout posts only when one bank transaction matches it exactly.",
            attention=skip_counts["shopify_payouts"])
        + _ledger_source_tile(
            "Shopify COGS", counts["shopify_cogs"],
            "Cost of goods uses only cost snapshots captured before the order.",
            attention=skip_counts["shopify_cogs"])
    )

    return f"""
    <div class="sub">Post local Plaid and Shopify records to the double-entry
      ledger. Sync makes no API calls and posts each source record once.</div>
    <div class="dash-grid">{tiles}</div>
    <div class="section">
      <div class="section-head"><h2>Needs attention</h2>
        <span class="tag">newest first</span></div>
      <p class="hint">Source records that were deliberately not posted. Until
        these are resolved, every ledger report is incomplete.</p>
      {_ledger_exceptions_table(skips)}
    </div>
    <div class="section">
      <div class="section-head"><h2>Freshness</h2></div>
      {_ledger_stamp_line("Plaid records pulled", freshness["plaid"],
                          "No Plaid records stored yet")}
      {_ledger_stamp_line("Shopify payouts pulled", freshness["shopify"],
                          "No Shopify payouts stored yet")}
      {_ledger_stamp_line("Entries last posted", counts["last_imported_at"],
                          "Nothing posted to the ledger yet")}
    </div>"""


def _ledger_money(value):
    """Money for ledger statements, signed the way an accountant reads it.

    ui.money() renders a negative as "$-45.00". scripts/ledger_verify.py
    already prints "-$45.00" for the same figures, so this screen matches the
    CLI report rather than giving the same number two spellings.
    """
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    return f"{'-' if amount < 0 else ''}${abs(amount):,.2f}"


def _ledger_empty_state(what):
    return f"""
    <div class="empty">
      <p><strong>Nothing posted yet</strong></p>
      <p>{what} once Plaid and Shopify records have been posted as journal
        entries.</p>
      <p><a href="/ledger?tab=sync">Go to Sync &rarr;</a></p>
    </div>"""


def _ledger_trial_balance_content():
    """Every account carrying activity, with debits and credits footed.

    Zero-activity accounts are left out and counted instead: the chart of
    accounts seeds 21 accounts, so on a young ledger listing them all buries
    the handful that moved under a screen of $0.00. Omitting a zero row
    changes neither total, and the count keeps the omission visible.
    """
    rows = ledger.trial_balance()
    active = [row for row in rows if row["debit_total"] or row["credit_total"]]
    if not active:
        return ('<div class="sub">Debits and credits by account, from the '
                "double-entry ledger.</div>"
                + _ledger_empty_state("Account balances appear here"))

    body = ""
    for row in active:
        indent = " acct-sub" if row["parent_account_id"] else ""
        body += (
            f'<tr><td class="num mixed">{escape(row["code"])}</td>'
            f'<td class="acct-name{indent}">{escape(row["name"])}</td>'
            f'<td>{escape(str(row["type"]).title())}</td>'
            f'<td class="num">{_ledger_money(row["debit_total"])}</td>'
            f'<td class="num">{_ledger_money(row["credit_total"])}</td>'
            f'<td class="num">{_ledger_money(row["normal_balance"])}</td></tr>'
        )

    zero = Decimal("0")
    debit_total = sum((row["debit_total"] for row in active), zero)
    credit_total = sum((row["credit_total"] for row in active), zero)
    balanced = debit_total == credit_total
    footing = ('<span class="tag ok">debits equal credits</span>' if balanced
               else '<span class="tag bad">out of balance</span>')
    omitted = len(rows) - len(active)
    omitted_note = ""
    if omitted:
        omitted_note = (f'<div class="none-note">{omitted} '
                        f'{_plural(omitted, "account")} with no activity '
                        f'{_plural(omitted, "is", "are")} not shown.</div>')

    return f"""
    <div class="sub">Debits and credits by account, from the canonical
      double-entry Ledger also used by Tax statements and projections.</div>
    <div class="section">
      <div class="section-head"><h2>Trial balance</h2>{footing}</div>
      <table class="table-hover">
        <thead><tr><th class="num" data-sort="num">Code</th><th data-sort="text">Account</th><th data-sort="text">Type</th>
          <th class="num" data-sort="num">Debit</th><th class="num" data-sort="num">Credit</th>
          <th class="num" data-sort="num">Balance</th></tr></thead>
        <tbody>{body}</tbody>
        <tfoot><tr><td></td><td>Total</td><td></td>
          <td class="num">{_ledger_money(debit_total)}</td>
          <td class="num">{_ledger_money(credit_total)}</td>
          <td class="num"></td></tr></tfoot>
      </table>
      {omitted_note}
    </div>
    <p class="hint">Balance uses each account type's normal sign: assets and
      expenses are debit-positive, liabilities, equity, and revenue are
      credit-positive.</p>"""


def _ledger_month_range(start, end):
    """The requested range, defaulting to the current month to date."""
    today = date.today()
    default_start = today.replace(day=1).isoformat()
    start = clean_date(start, default_start)
    end = clean_date(end, today.isoformat())
    if start > end:
        start, end = end, start
    return start, end


def _ledger_range_form(tab, start, end):
    return f"""
    <form class="range-form" method="get" action="/ledger">
      <input type="hidden" name="tab" value="{tab}">
      <label>From <input type="date" name="start" value="{escape(start)}"></label>
      <label>To <input type="date" name="end" value="{escape(end)}"></label>
      <button class="check" type="submit">View</button>
    </form>"""


def _stmt_group(label, total, accounts, note=""):
    """One expandable statement line: the figure by default, accounts on
    request. A group with nothing in it stays visible at $0.00 rather than
    disappearing, so the statement's shape does not change with the data."""
    if not accounts:
        return (f'<div class="stmt-row"><span class="stmt-label">{escape(label)}'
                f'</span>{_stmt_amount(total)}</div>'
                + note)
    lines = ""
    for account in accounts:
        lines += (f'<div class="stmt-account"><span>'
                  f'<span class="code mono">{escape(account["code"])}</span>'
                  f'{escape(account["name"])}</span>'
                  f'{_stmt_amount(account["amount"])}</div>')
    return f"""
    <details class="stmt-group">
      <summary><span class="stmt-label">{escape(label)}
        <span class="stmt-caret">{len(accounts)}
          {_plural(len(accounts), "account")}</span></span>
        {_stmt_amount(total)}</summary>
      <div class="stmt-accounts">{lines}</div>
    </details>{note}"""


def _stmt_total(label, amount, net=False):
    cls = "stmt-row stmt-total net" if net else "stmt-row stmt-total"
    return (f'<div class="{cls}"><span class="stmt-label">{escape(label)}</span>'
            f'{_stmt_amount(amount)}</div>')


# Revenue and transfer classification were fixed in ledger_sync.py after
# this caveat was written (Plaid deposits now route to Refunds Received /
# Interest Income / Uncategorized Income rather than Sales Revenue, and
# transfers post through a 1090 Transfer Clearing account instead of being
# skipped) — see docs/superpowers/specs/2026-07-25-ledger-ui-design.md's
# known-issues history. No caveat needed here now: the Revenue group's own
# account breakdown already shows Uncategorized Income as its own line for
# anyone checking what Plaid could not classify.
#
# COGS is honest about coverage: _sync_shopify_cogs posts only when a
# Ledger-backed, dated FIFO acquisition covers every unit in the sale. Any
# unresolved sale is logged as a skip and disclosed on the Cost of goods sold
# group below; costed sales are included normally.
def _cogs_caveat(count):
    return f"""
    <div class="flag flag-warn">{count} Shopify sale(s) still have no posted
      cost of goods. Klearing held them back rather than guessing at a dated
      acquisition or cost — review the skipped records on
      <a href="/ledger?tab=sync">Sync</a>. Gross profit is incomplete for
      those sales.</div>"""


def _stmt_active(accounts):
    """Only accounts that moved in the period.

    profit_and_loss keeps every account in its bucket with zero totals — by
    design, so a caller can see the full chart — which means emptiness cannot
    be tested by list length, and an untouched account would otherwise be
    listed inside a group and counted in its caret. Same rule as the trial
    balance: a zero row changes no total.
    """
    return [account for account in accounts
            if account["debit_total"] or account["credit_total"]]


def _ledger_pnl_content(start=None, end=None):
    start, end = _ledger_month_range(start, end)
    report = ledger.profit_and_loss(start, end)
    form = _ledger_range_form("pnl", start, end)
    revenue = _stmt_active(report["revenue"])
    cogs = _stmt_active(report["cogs"])
    operating = _stmt_active(report["operating_expenses"])
    unresolved_cogs = db.list_unresolved_shopify_cogs(start, end)
    if not (revenue or cogs or operating):
        return (f'<div class="sub">Revenue and expenses for a date range, '
                f"from the double-entry ledger.</div>{form}"
                '<div class="section"><div class="none-note">No ledger '
                "activity in this range. Try a wider range, or post new "
                'records from <a href="/ledger?tab=sync">Sync</a>.</div>'
                "</div>")

    return f"""
    <div class="sub">Revenue and expenses for a date range, from the
      canonical double-entry Ledger also used by Tax statements, projections,
      exports, and planning costs.</div>
    {form}
    <div class="section">
      <div class="section-head"><h2>Profit &amp; Loss</h2>
        <span class="tag">{escape(start)} to {escape(end)}</span></div>
      <div class="statement">
        {_stmt_group("Revenue", report["revenue_total"], revenue)}
        {_stmt_group("Cost of goods sold", report["cogs_total"], cogs,
                    note=_cogs_caveat(len(unresolved_cogs))
                    if unresolved_cogs else "")}
        {_stmt_total("Gross profit", report["gross_profit"])}
        {_stmt_group("Operating expenses",
                    report["operating_expenses_total"], operating)}
        {_stmt_total("Net income", report["net_income"], net=True)}
      </div>
    </div>
    <p class="hint">Cost of goods sold is account 5000 and its sub-accounts;
      every other expense account is an operating expense. Accrual basis:
      Shopify revenue is recognised when the order is placed, not when the
      payout settles.</p>"""


def _stmt_amount(value):
    """A statement figure, marked when it is negative.

    Colour is never the only signal here — the minus sign carries the meaning
    on its own — but in a normal-sign statement a negative means an account is
    running backwards from its nature, which is worth catching at a glance.
    Reuses .amount.expense rather than inventing a colour for it.
    """
    amount = Decimal(str(value))
    cls = "amount expense" if amount < 0 else "amount"
    return f'<span class="{cls}">{_ledger_money(amount)}</span>'


def _stmt_row(label, amount, hint=""):
    """A plain statement line: neither expandable nor a subtotal."""
    hint_html = f'<div class="hint stmt-hint">{hint}</div>' if hint else ""
    return (f'<div class="stmt-row"><span class="stmt-label">{escape(label)}'
            f'</span>{_stmt_amount(amount)}</div>'
            + hint_html)


def _ledger_asof_form(tab, as_of):
    return f"""
    <form class="range-form" method="get" action="/ledger">
      <input type="hidden" name="tab" value="{tab}">
      <label>As of <input type="date" name="as_of" value="{escape(as_of)}"></label>
      <button class="check" type="submit">View</button>
    </form>"""


# _sync_shopify_cogs still refuses to post when a dated acquisition cannot be
# tracked, rather than crediting Inventory with nothing to debit it. Those
# sales remain visible through the conditional COGS caveat above; inventory
# itself is not decremented for them, so the balance sheet stays honest.


def _ledger_balance_sheet_content(as_of=None):
    as_of = clean_date(as_of, date.today().isoformat())
    report = ledger.balance_sheet(as_of)
    form = _ledger_asof_form("balance-sheet", as_of)
    assets = _stmt_active(report["assets"])
    liabilities = _stmt_active(report["liabilities"])
    equity = _stmt_active(report["equity_accounts"])
    intro = ('<div class="sub">What the business owns and owes as of a date, '
             "from the canonical double-entry Ledger also used by Tax "
             "statements and projections.</div>")

    if not (assets or liabilities or equity or report["current_earnings"]):
        return (intro + form + '<div class="section"><div class="none-note">'
                "No ledger activity on or before this date. Try a later date, "
                'or post new records from <a href="/ledger?tab=sync">Sync</a>.'
                "</div></div>")

    balanced = report["is_balanced"]
    footing = ('<span class="tag ok">balances</span>' if balanced
               else '<span class="tag bad">out of balance</span>')
    liabilities_and_equity = report["liabilities_total"] + report["equity_total"]

    return f"""
    {intro}
    {form}
    <div class="section">
      <div class="section-head"><h2>Balance sheet</h2>
        <span class="tag">as of {escape(as_of)}</span>{footing}</div>
      <div class="statement">
        {_stmt_group("Assets", report["assets_total"], assets)}
        {_stmt_total("Total assets", report["assets_total"])}
        {_stmt_group("Liabilities", report["liabilities_total"], liabilities)}
        {_stmt_group("Equity", report["equity_accounts_total"], equity)}
        {_stmt_row("Current earnings", report["current_earnings"],
                   hint="Revenue less expenses for all time. Not yet closed "
                        "into equity, because the ledger has no closing-entry "
                        "workflow.")}
        {_stmt_total("Total liabilities and equity", liabilities_and_equity,
                     net=True)}
      </div>
    </div>
    <p class="hint">Assets {_ledger_money(report["assets_total"])} against
      liabilities and equity {_ledger_money(liabilities_and_equity)}, a
      difference of {_ledger_money(report["difference"])}. Every journal entry
      is checked for balance as it is written, so this confirms the books
      rather than auditing them.</p>"""


def _ledger_accounts_content():
    """The chart of accounts, grouped by type and showing what has been used.

    Reads trial_balance rather than list_accounts because it carries lifetime
    debit and credit totals for every account, which is what "in use" means
    here. Balances themselves are deliberately not repeated — that is the
    trial balance's job, and this screen answers a different question: what
    accounts exist, how they nest, and which have never been touched.
    """
    rows = ledger.trial_balance()
    if not rows:
        return ('<div class="sub">The accounts this ledger posts to.</div>'
                '<div class="empty"><p><strong>No accounts</strong></p>'
                "<p>The chart of accounts is created by the ledger migrations "
                "when the database is first opened.</p></div>")

    used_total = 0
    profit_and_loss_accounts = [
        row for row in rows if row["type"] in tax_categories.ACCOUNT_CATEGORIES
    ]
    mapped_total = sum(bool(row["tax_category"])
                       for row in profit_and_loss_accounts)
    used_unmapped = sum(
        bool(row["debit_total"] or row["credit_total"])
        and not row["tax_category"]
        for row in profit_and_loss_accounts
    )
    if used_unmapped:
        mapping_state = (
            f'<span class="tag warn">{used_unmapped} used '
            f'{_plural(used_unmapped, "account needs", "accounts need")} '
            "categorization</span>"
        )
        mapping_note = f"""
      <div class="flag flag-warn"><strong>Categorization needed.</strong>
        {used_unmapped} {_plural(used_unmapped, "account has", "accounts have")}
        activity but no Schedule C category. These must be resolved before
        the Ledger can produce complete tax projections or exports.</div>"""
    else:
        mapping_state = '<span class="tag ok">current activity categorized</span>'
        mapping_note = ""
    groups = ""
    for account_type, label in LEDGER_ACCOUNT_TYPES:
        in_type = [row for row in rows if row["type"] == account_type]
        if not in_type:
            continue
        body = ""
        for row in in_type:
            used = bool(row["debit_total"] or row["credit_total"])
            used_total += 1 if used else 0
            status = ('<span class="tag">in use</span>' if used
                      else '<span class="tag off">not yet used</span>')
            indent = " acct-sub" if row["parent_account_id"] else ""
            choices = tax_categories.choices_for_account_type(row["type"])
            if choices:
                options = ['<option value="">Needs categorization</option>']
                for category in choices:
                    selected = " selected" if row["tax_category"] == category else ""
                    options.append(
                        f'<option value="{escape(category)}"{selected}>'
                        f'{escape(tax_categories.CATEGORY_LABELS[category])}</option>'
                    )
                mapping = f"""
                  <form class="tax-map-form" method="post"
                        action="/ledger/accounts/{row['id']}/tax-category">
                    <select name="tax_category"
                            aria-label="Schedule C category for {escape(row['code'])} {escape(row['name'])}">
                      {''.join(options)}
                    </select>
                    <button class="check" type="submit">Save</button>
                  </form>"""
            else:
                mapping = '<span class="tag off">not used on Schedule C</span>'
            body += (f'<tr><td class="num mixed">{escape(row["code"])}</td>'
                     f'<td class="acct-name{indent}">{escape(row["name"])}</td>'
                     f"<td>{status}</td><td>{mapping}</td></tr>")
        groups += f"""
        <h3>{escape(label)} <span class="tag">{len(in_type)}</span></h3>
        <table class="table-hover">
          <thead><tr><th class="num" data-sort="num">Code</th><th data-sort="text">Account</th>
            <th data-sort="text">Status</th><th>Schedule C category</th></tr></thead>
          <tbody>{body}</tbody>
        </table>"""

    unused = len(rows) - used_total
    summary = (f'{len(rows)} accounts, {used_total} in use'
               + (f", {unused} not yet used" if unused else ""))

    type_options = "".join(
        f'<option value="{account_type}">{escape(label)}</option>'
        for account_type, label in LEDGER_ACCOUNT_TYPES)
    parent_options = ['<option value="">No parent (top level)</option>']
    for row in rows:
        if row["parent_account_id"]:
            continue  # one level of nesting, same as the seeded chart
        parent_options.append(
            f'<option value="{escape(row["code"])}">{escape(row["code"])} '
            f'{escape(row["name"])}</option>')

    return f"""
    <div class="sub">The accounts this ledger posts to, grouped as they
      appear on the statements. Sub-accounts are indented under their
      parent.</div>
    <div class="section ledger-account-section">
      <div class="section-head"><h2>Chart of accounts</h2>
        <span class="tag">{escape(summary)}</span></div>
      <div class="mapping-summary">
        <span>{mapped_total} of {len(profit_and_loss_accounts)} revenue and
          expense accounts categorized</span>
        {mapping_state}
      </div>
      {mapping_note}
      {groups}
    </div>
    {ui.disclosure("Add an account", f'''
      <div class="hint mb-4">Klearing starts with a small chart. Add your
        own categories when the starter set does not describe how this
        business actually spends and earns. The code is assigned for you so
        it cannot collide with an account Klearing adds later.</div>
      <form method="post" action="/ledger/accounts/new" class="stack">
        <div class="cred-field">
          <label class="q" for="new-account-name">Account name</label>
          <input type="text" id="new-account-name" name="name" required
                 maxlength="100" placeholder="Packaging supplies">
        </div>
        <div class="cred-field">
          <label class="q" for="new-account-type">Type</label>
          <select id="new-account-type" name="account_type">{type_options}</select>
        </div>
        <div class="cred-field">
          <label class="q" for="new-account-parent">Group under</label>
          <select id="new-account-parent" name="parent_code">
            {''.join(parent_options)}</select>
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Add account</button>
        </div>
      </form>''', note="extend the starter chart")}
    <p class="hint">Account codes are assigned by Klearing and stay
      read-only. Tax categories may be changed here without rewriting
      journal entries. Klearing seeds only clear matches; generic or
      mixed-purpose accounts wait for you or your accountant rather than
      being guessed, and a new account starts uncategorized for the same
      reason.</p>"""


def _ledger_sync_message(summary):
    created = summary["created"]
    attention = (summary["shopify_payouts"]["unmatched"]
                 + summary["shopify_cogs"]["skipped"])
    if created:
        message = (f"Posted {created} new journal "
                   f"{_plural(created, 'entry', 'entries')}.")
    else:
        message = "No new records to post."
    if attention:
        message += (f" {attention} source {_plural(attention, 'record')} "
                    f"still {_plural(attention, 'needs', 'need')} attention.")
    return message


def render_ledger_page(message=None, error=None, tab="sync", start=None,
                       end=None, as_of=None):
    """Render the Ledger workspace over the native double-entry books."""
    renderers = {"sync": _ledger_sync_content,
                 "trial-balance": _ledger_trial_balance_content,
                 "pnl": lambda: _ledger_pnl_content(start, end),
                 "balance-sheet": lambda: _ledger_balance_sheet_content(as_of),
                 "accounts": _ledger_accounts_content}
    active = tab if tab in renderers else "sync"
    header = ('<form method="post" action="/ledger/sync">'
              '<button class="approve" type="submit">Sync now</button></form>')
    content = _ledger_nav(active) + renderers[active]()
    return render_shell("Ledger", header, content, message, error, wide=True)


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(tab: str = "sync", start: str = None, end: str = None,
                as_of: str = None, msg: str = None, err: str = None):
    db.init_db()
    return render_ledger_page(msg, err, tab, start, end, as_of)


@app.post("/ledger/sync")
def ledger_sync_run():
    db.init_db()
    try:
        summary = ledger_sync.sync_ledger_from_sources()
    except ledger.LedgerValidationError as exc:
        return _redirect_ledger(err=str(exc))
    except Exception as exc:  # a local, network-free run: report, don't 500
        return _redirect_ledger(err=f"Ledger sync failed: {exc}")
    return _redirect_ledger(msg=_ledger_sync_message(summary))


@app.post("/ledger/accounts/{account_id}/tax-category")
def save_ledger_account_tax_category(account_id: int,
                                     tax_category: str = Form("")):
    """Persist one validated account mapping, then return to Accounts."""
    try:
        account = ledger.set_account_tax_category(account_id, tax_category)
    except ledger.LedgerValidationError as exc:
        return _redirect_ledger_accounts(err=str(exc))
    category = account["tax_category"]
    if category:
        message = (f'{account["code"]} {account["name"]} categorized as '
                   f'{tax_categories.CATEGORY_LABELS[category]}.')
    else:
        message = (f'{account["code"]} {account["name"]} now needs '
                   "categorization before complete tax output.")
    return _redirect_ledger_accounts(msg=message)


@app.post("/ledger/accounts/new")
def create_ledger_account(name: str = Form(""),
                          account_type: str = Form(""),
                          parent_code: str = Form("")):
    """Add one user-defined account to the Chart of Accounts.

    No tax category is set here. A new account starts unresolved and the
    Accounts tab already surfaces that as needing categorization, which is
    the same refusal-to-guess the seeded chart follows.
    """
    try:
        account = ledger.create_account(
            name, account_type, parent_code=(parent_code or None))
    except ledger.LedgerValidationError as exc:
        return _redirect_ledger_accounts(err=str(exc))
    return _redirect_ledger_accounts(
        msg=f'Added {account["code"]} {account["name"]}. Set its Schedule C '
            "category below so tax output stays complete.")


TAX_TABS = (
    ("archive", "Legacy archive"), ("statements", "Statements"),
    ("projections", "Projections"), ("planning", "Planning"),
)


def _tax_nav(active):
    return ui.sub_nav([(key, label, f"/tax?tab={key}")
                       for key, label in TAX_TABS],
                      active, "Tax sections")


def _tax_archive_content():
    return '''<div class="sub">Read-only records from Klearing's retired Wave Tax queue. These records no longer feed canonical statements, projections, Ledger exports, or planning.</div>
    <div id="tax-archive" class="tax-workspace" data-view="archive">
      <div class="tax-toolbar">
        <div class="tax-toolbar-group">
          <label>Legacy status<select id="tax-archive-status-filter">
            <option value="approved">Approved</option><option value="pending">Pending</option>
            <option value="rejected">Rejected</option><option value="all">All</option>
          </select></label>
        </div>
        <div class="tax-toolbar-group">
          <a id="tax-legacy-csv-export" class="btn-link" href="/api/tax/exports/legacy-transactions.csv">Export approved legacy CSV</a>
        </div>
      </div>
      <div id="tax-archive-status" class="none-note" aria-live="polite">Loading legacy records...</div>
      <div id="tax-archive-counts" class="planning-results" aria-label="Legacy Wave Tax archive counts"></div>
      <div class="section unposted tax-table-section">
        <div class="section-head"><h2>Legacy Wave Tax records</h2><span class="tag">Read only</span></div>
        <div class="tax-table-wrap">
          <table id="tax-legacy-table">
            <thead><tr><th>Date and merchant</th><th data-sort="text">Wave category</th><th data-sort="text">Stored tax category</th><th>Status</th><th class="num" data-sort="num">Amount</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div id="tax-archive-empty" class="none-note" hidden>No legacy Wave Tax records match this status.</div>
      </div>
    </div><script src="/static/tax/workspace.js?v=5"></script>'''


def _tax_statements_content():
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    return f'''<div class="sub">See income, expenses, margins, and balances from the native double-entry Ledger for any period.</div>
    <div id="tax-statements" class="tax-workspace" data-view="statements">
      <div class="tax-toolbar">
        <div class="tax-toolbar-group">
          <label>Period<select id="tax-statement-period"><option value="this_month">This month</option><option value="last_month">Last month</option><option value="ytd">Year to date</option><option value="custom">Custom</option></select></label>
          <label>From<input id="tax-statement-start" type="date" value="{start}"></label>
          <label>To<input id="tax-statement-end" type="date" value="{end}"></label>
          <button id="tax-statements-apply" class="check" type="button">Apply</button>
        </div>
        <div class="tax-toolbar-group">
          <a id="tax-income-export" class="btn-link" aria-disabled="true">Income statement PDF</a>
          <a id="tax-schedule-export" class="btn-link" aria-disabled="true">Schedule C PDF</a>
          <a id="tax-statement-csv" class="btn-link" aria-disabled="true">Ledger activity CSV</a>
        </div>
      </div>
      <div id="tax-statements-status" class="none-note" aria-live="polite">Loading statement...</div>
      <div id="tax-statement-results" class="planning-results"></div>
      <div class="tax-results-grid">
        <div class="section"><div class="section-head"><h2>Operating expenses</h2></div><div class="tax-table-wrap"><table><thead><tr><th data-sort="text">Category</th><th class="num" data-sort="num">Amount</th></tr></thead><tbody id="tax-expense-rows"></tbody></table></div><div id="tax-expense-empty" class="none-note" hidden>No approved operating expenses in this period.</div></div>
        <div class="section"><div class="section-head"><h2>Balance position</h2></div><div id="tax-balance-results" class="planning-results"></div><p id="tax-balance-note" class="planning-note"></p></div>
      </div>
    </div><script src="/static/tax/workspace.js?v=4"></script>'''


def _tax_projections_content():
    return f'''<div class="sub">Estimate federal, self-employment, and Iowa taxes from categorized native Ledger income. Planning estimates are not a filed return.</div>
    <div id="tax-projections" class="tax-workspace" data-view="projections">
      <div class="tax-toolbar">
        <div class="tax-toolbar-group">
          <label>As of<input id="tax-projection-date" type="date" value="{date.today().isoformat()}"></label>
          <label>Section 179 ($)<input id="tax-section-179" type="number" min="0" step="0.01" value="0" inputmode="decimal"></label>
          <label>Home office sq ft<input id="tax-home-office" type="number" min="0" max="300" step="1" value="0" inputmode="numeric"></label>
          <button id="tax-projection-apply" class="check" type="button">Recalculate</button>
        </div>
        <a id="tax-projection-export" class="btn-link" aria-disabled="true">Projection PDF</a>
      </div>
      <div id="tax-projection-status" class="none-note" aria-live="polite">Calculating projection...</div>
      <div id="tax-projection-blocked" class="section unposted tax-projection-blocked" role="status" hidden>
        <div class="section-head"><h2 id="tax-projection-blocked-title">Categorization needed</h2></div>
        <p id="tax-projection-blocked-message"></p>
        <ul id="tax-projection-blocked-accounts" class="tax-warning-list"></ul>
        <a id="tax-projection-blocked-action" class="btn-link" href="/ledger?tab=accounts">Categorize Ledger accounts</a>
      </div>
      <div id="tax-projection-output" hidden>
        <div id="tax-projection-results" class="planning-results"></div>
        <div class="tax-results-grid">
          <div class="section"><div class="section-head"><h2>Deductions</h2></div><div id="tax-deduction-results" class="planning-results"></div></div>
          <div class="section"><div class="section-head"><h2>Quarterly estimates</h2></div><div class="tax-table-wrap"><table><thead><tr><th>Due date</th><th class="num" data-sort="num">Estimated payment</th><th data-sort="text">Status</th></tr></thead><tbody id="tax-quarterly-rows"></tbody></table></div></div>
        </div>
        <div class="section unposted"><div class="section-head"><h2>Estimate limits</h2></div><ul id="tax-projection-warnings" class="tax-warning-list"></ul></div>
      </div>
    </div><script src="/static/tax/workspace.js?v=4"></script>'''


def _tax_planning_content():
    return '''<div class="sub">Plan sales volume and pricing using Shopify or manual unit inputs with period-matched costs from categorized Ledger accounts.</div>
    <div id="tax-planning" class="tax-planning" aria-live="polite">
      <div class="planning-controls">
        <div class="planning-source" aria-label="Planning source">
          <button type="button" data-source="shopify">Shopify</button>
          <button type="button" data-source="manual">Manual</button>
        </div>
        <div class="planning-ranges" aria-label="Ledger cost period">
          <button type="button" data-range="7">7d</button>
          <button type="button" data-range="30">30d</button>
          <button type="button" data-range="90">90d</button>
        </div>
      </div>
      <div class="section" data-shopify-only>
        <div class="section-head"><h2>SKU performance</h2></div>
        <div id="planning-sku-empty" class="none-note" hidden>No paid Shopify order line items in this range.</div>
        <table id="planning-sku-table"><thead><tr><th data-sort="text">Product</th><th class="num" data-sort="num">Units sold</th><th class="num" data-sort="num">Avg SP</th><th class="num" data-sort="num">Net revenue</th></tr></thead><tbody></tbody></table>
        <div id="planning-blended" class="planning-note"></div>
      </div>
      <div class="section" data-manual-only hidden>
        <div class="section-head"><h2>Manual inputs</h2></div>
        <div class="planning-manual">
          <label>Average selling price ($)<input id="planning-manual-sp" type="number" min="0" step="0.01" inputmode="decimal"></label>
          <label>Units sold<input id="planning-manual-q" type="number" min="0" step="1" inputmode="numeric"></label>
          <label>Variable cost / unit ($)<input id="planning-manual-vc" type="number" min="0" step="0.01" inputmode="decimal"></label>
          <button id="planning-manual-save" class="check" type="button">Save</button>
        </div>
      </div>
      <div class="section planning-target">
        <div class="section-head"><h2>Target</h2></div>
        <label>Target net income ($)<input id="planning-target-income" type="number" min="0" step="0.01" inputmode="decimal"></label>
      </div>
      <div class="section"><div class="section-head"><h2>Planning results</h2></div>
        <div id="planning-status" class="none-note" aria-live="polite">Loading planning data...</div>
        <div id="planning-results" class="planning-results"></div>
        <p id="planning-cost-basis" class="planning-note"></p>
        <p class="planning-note">Figures are estimates based on local Shopify or manual unit inputs and categorized Ledger costs. Not financial advice.</p>
      </div>
    </div><script src="/static/tax/planning.js"></script>'''


def render_tax_page(message=None, error=None, tab="archive"):
    """Render the four-view Tax workspace; data loads from local Tax APIs."""
    renderers = {
        "archive": _tax_archive_content,
        "statements": _tax_statements_content,
        "projections": _tax_projections_content,
        "planning": _tax_planning_content,
    }
    if tab == "transactions":
        tab = "archive"
    active = tab if tab in renderers else "archive"
    content = _tax_nav(active) + renderers[active]()
    return render_shell("Tax", "", content, message, error, wide=True)


@app.get("/tax", response_class=HTMLResponse)
def tax_page(tab: str = "archive", msg: str = None, err: str = None):
    return render_tax_page(msg, err, tab)


@app.get("/static/app/table.js")
def app_table_javascript():
    """The shared interaction layer: table sorting, row selection, and
    the confirmation dialog. Loaded on every page by render_shell."""
    return FileResponse(paths.resource_path("frontend/app/table.js"),
                        media_type="application/javascript")


@app.get("/static/fonts/{filename}")
def bundled_font(filename: str):
    """Serve a bundled typeface.

    Same pattern as the /static/tax/*.js routes, but parameterised
    because there are three files and they are pure static assets. The
    allowlist is the point: this route takes a path segment from the
    URL, so it must never be able to reach outside frontend/fonts.
    Immutable caching because the filenames are stable and the contents
    never change within a release.
    """
    if filename not in BUNDLED_FONTS:
        return Response(status_code=404)
    path = paths.resource_path(f"frontend/fonts/{filename}")
    if not path.exists():
        # A missing font is survivable by design — every --font-* token
        # carries a fallback stack and every @font-face uses swap.
        return Response(status_code=404)
    return FileResponse(path, media_type="font/woff2", headers={
        "Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/static/tax/planning.js")
def tax_planning_javascript():
    return FileResponse(paths.resource_path("frontend/tax/planning.js"),
                        media_type="application/javascript")


@app.get("/static/tax/workspace.js")
def tax_workspace_javascript():
    return FileResponse(paths.resource_path("frontend/tax/workspace.js"),
                        media_type="application/javascript")


def render_growth_page(message=None, error=None):
    db.init_db()
    active = business_registry.active_business()
    active_id = active["id"] if active else None

    # 1. Revenue by category, trended.
    labels, cat_series = monthly_income_series(GROWTH_MONTHS, active_id)
    revenue_rows = ""
    for category, values in cat_series[:6]:
        if sum(values) <= 0:
            continue
        spark = charts.sparkline(
            values, labels=labels, last_label=money(values[-1]),
            aria=f"{category} monthly income")
        revenue_rows += (
            f'<div class="growth-row"><div class="growth-row-head">'
            f'<span class="vendor-name">{escape(category)}</span>'
            f'<span class="hf-detail"><strong>{money(values[-1])} in '
            f'{escape(labels[-1])}</strong> &middot; {money(sum(values))} '
            f'over {GROWTH_MONTHS} months</span></div>{spark}</div>')
    # Headline total row: every category summed per month (not just the
    # top 6 rendered below), so the section leads with overall revenue
    # and its month-over-month direction. Reuses cat_series — no new
    # queries.
    total_row = ""
    if cat_series:
        monthly_totals = [
            sum(values[i] for _cat, values in cat_series)
            for i in range(len(labels))]
        if sum(monthly_totals) > 0:
            mom = ""
            if len(monthly_totals) >= 2:
                mom = (f" &middot; "
                       f"{escape(pct_change_text(monthly_totals[-1], monthly_totals[-2]))}"
                       f" vs {escape(labels[-2])}")
            total_spark = charts.sparkline(
                monthly_totals, labels=labels,
                last_label=money(monthly_totals[-1]),
                aria="All income, monthly")
            total_row = (
                f'<div class="growth-row"><div class="growth-row-head">'
                f'<span class="vendor-name">All income</span>'
                f'<span class="hf-detail"><strong>'
                f'{money(monthly_totals[-1])} in {escape(labels[-1])}'
                f"</strong>{mom} &middot; {money(sum(monthly_totals))} "
                f"over {GROWTH_MONTHS} months</span></div>{total_spark}</div>")
    # Stripe revenue, month by month, alongside locally approved rows —
    # card sales the bank feed only ever shows as lumped payouts.
    stripe_row = ""
    if not stripe_client.is_configured():
        stripe_row = ('<div class="none-note">Connect Stripe on the '
                      '<a href="/settings?tab=connections">Connections page</a> to see '
                      "Stripe card revenue trended here too.</div>")
    else:
        try:
            series = stripe_client.monthly_revenue(GROWTH_MONTHS)
        except Exception:
            series = None
        if series is None:
            stripe_row = ('<div class="none-note">Stripe couldn\'t be '
                          "reached — its revenue trend is unavailable "
                          'right now. <a href="/settings?tab=connections">Re-test '
                          "connection</a>.</div>")
        elif sum(v for _label, v in series) > 0:
            s_labels = [label for label, _v in series]
            s_values = [v for _label, v in series]
            s_mom = ""
            if len(s_values) >= 2:
                s_mom = (f" &middot; "
                         f"{escape(pct_change_text(s_values[-1], s_values[-2]))}"
                         f" vs {escape(s_labels[-2])}")
            s_spark = charts.sparkline(
                s_values, labels=s_labels, last_label=money(s_values[-1]),
                aria="Stripe revenue, monthly")
            stripe_row = (
                f'<div class="growth-row"><div class="growth-row-head">'
                f'<span class="vendor-name">Stripe revenue</span>'
                f'<span class="hf-detail"><strong>'
                f"{money(s_values[-1])} in {escape(s_labels[-1])}</strong>"
                f"{s_mom} &middot; {money(sum(s_values))} over "
                f"{GROWTH_MONTHS} months &middot; net of refunds"
                f"</span></div>{s_spark}</div>")
        else:
            stripe_row = ('<div class="none-note">Stripe is connected but '
                          "recorded no charges in the last "
                          f"{GROWTH_MONTHS} months.</div>")
    revenue_section = ((total_row + revenue_rows) or (
        '<div class="none-note">No income recorded yet — approve some income '
        'transactions on the <a href="/review?tab=transactions">Pending tab</a> to see '
        "category trends here.</div>")) + stripe_row

    # 2. Traffic source -> conversion efficiency (honestly separate from
    # revenue: GA4 conversions are website events, not linked to booked sales).
    conv_rows, primary_event = None, None
    if not ga4_client.is_configured():
        conv_section = ('<div class="none-note">GA4 isn\'t connected '
                        '(optional). Connect it on the '
                        '<a href="/settings?tab=connections">Connections page</a> to '
                        'see which channels convert best.</div>')
    else:
        row = db.get_latest_ga4_snapshot()
        snapshot = json.loads(row["snapshot_json"]) if row else {}
        conv_rows, primary_event = conversion_by_source(snapshot)
        if not conv_rows:
            conv_section = ('<div class="none-note">No channel breakdown '
                            'stored yet. It\'s captured on the next daily '
                            'snapshot — check back after the next morning '
                            'check, or run it now from '
                            '<a href="/settings?tab=automation">Settings &rarr; '
                            'Automation</a>.</div>')
        else:
            bar = charts.horizontal_bar_chart(
                [{"label": r["channel"], "value": r["rate"],
                  "value_label": f'{r["rate"]:.1f} / 100 sess '
                                 f'({r["conversions"]:,}/{r["sessions"]:,})'}
                 for r in conv_rows if r["sessions"] > 0],
                aria="Conversions per 100 sessions by channel")
            # This is eventCount / sessions (GA4 events are not session-
            # scoped, so it's "per 100 sessions", not a bounded % of
            # sessions — and can exceed 100). Labelled honestly as such.
            conv_section = (
                f'<div class="hint mb-3">How often '
                f'each channel\'s visitors fired <span class="mono">'
                f'{escape(primary_event or "your conversion event")}</span>, '
                f'as events per 100 sessions (higher converts better; last '
                f'30 days). A website signal only — not linked to booked '
                f'sales.</div>{bar}')

    # 3. Directional expense-to-revenue efficiency.
    margin = directional_margin(GROWTH_MARGIN_DAYS, active_id)
    if margin is None:
        margin_section = ('<div class="none-note">Not enough income yet to '
                          'estimate a margin.</div>')
    else:
        value = margin["margin"]
        pill = "ok" if value >= 40 else ("warn" if value >= 0 else "bad")
        word = "healthy" if value >= 40 else ("thin" if value >= 0
                                              else "negative")
        margin_section = f"""
        <div class="totals">
          <div><div class="label">Income ({margin["days"]}d)</div>
            <div class="value in">+{money(margin["income"])}</div></div>
          <div><div class="label">Material / COGS spend</div>
            <div class="value out">-{money(margin["cogs"])}</div></div>
          <div><div class="label">Directional gross margin</div>
            <div class="value">{value:.0f}%
              <span class="tag {pill}">{word}</span></div></div>
        </div>
        <div class="commentary">Directional only — a rough
          income-minus-materials margin, not per-sale COGS accounting.
          Klearing doesn't match each sale to its cost.</div>"""

    # 3b. Labor cost from Gusto (optional) — payroll is usually the
    # largest expense the bank feed shows only as opaque withdrawals.
    if not gusto_client.is_configured():
        labor_section = ('<div class="none-note">Connect Gusto on the '
                         '<a href="/settings?tab=connections">Connections page</a> to '
                         "trend payroll cost here.</div>")
    else:
        try:
            labor_series, headcount = gusto_client.monthly_payroll(
                GROWTH_MONTHS)
        except Exception:
            labor_series = None
        if labor_series is None:
            labor_section = ('<div class="none-note">Gusto couldn\'t be '
                             "reached — the labor-cost trend is "
                             'unavailable right now. <a href="/settings?tab=connections">'
                             "Re-test connection</a>.</div>")
        elif sum(v for _label, v in labor_series) > 0:
            l_labels = [label for label, _v in labor_series]
            l_values = [v for _label, v in labor_series]
            l_spark = charts.sparkline(
                l_values, labels=l_labels,
                last_label=money(l_values[-1]),
                aria="Payroll cost, monthly")
            head_note = (f" &middot; headcount {headcount} people"
                         if headcount else "")
            labor_section = (
                f'<div class="growth-row"><div class="growth-row-head">'
                f'<span class="vendor-name">Payroll cost</span>'
                f'<span class="hf-detail"><strong>'
                f"{money(l_values[-1])} in {escape(l_labels[-1])}</strong>"
                f"{head_note} &middot; gross + employer taxes"
                f"</span></div>{l_spark}</div>")
        else:
            labor_section = ('<div class="none-note">Gusto is connected '
                             "but recorded no payroll in the last "
                             f"{GROWTH_MONTHS} months.</div>")

    # 4. Where to focus.
    focus = _growth_focus(cat_series, conv_rows, primary_event, margin)
    if focus:
        items = "".join(f"<li>{statement}</li>" for statement in focus[:3])
        focus_section = (
            f'<ul class="focus-list">{items}</ul>'
            f'<div class="health-note">Pattern observations computed from '
            f'your numbers with fixed rules — <strong>not AI-generated '
            f'advice</strong>.</div>')
    else:
        focus_section = ('<div class="none-note">A few more weeks of data and '
                         'specific observations will appear here.</div>')

    content = f"""
    <div class="sub">Deterministic revenue and profitability views from your
      approved transactions and GA4 snapshots. Pattern observations from real
      numbers — never AI.</div>
    <div class="section">
      <div class="section-head"><h2>Revenue by category, trended</h2>
        <span class="tag">last {GROWTH_MONTHS} months</span></div>
      {revenue_section}
    </div>
    <div class="section">
      <div class="section-head"><h2>Which channel converts best</h2>
        <span class="tag">traffic &rarr; conversion</span></div>
      {conv_section}
    </div>
    <div class="section">
      <div class="section-head"><h2>Expense-to-revenue efficiency</h2>
        <span class="tag">directional</span></div>
      {margin_section}
    </div>
    <div class="section">
      <div class="section-head"><h2>Labor cost</h2>
        <span class="tag">from Gusto</span></div>
      {labor_section}
    </div>
    <div class="section">
      <div class="section-head"><h2>Where to focus</h2></div>
      {focus_section}
    </div>"""
    return render_shell("Growth", "", content, message, error, wide=True)


@app.get("/growth", response_class=HTMLResponse)
def growth_page(msg: str = None, err: str = None):
    db.init_db()
    return render_growth_page(message=msg, error=err)


# ============================================================ Business tools
#
# Four read-only / local-calculation tools. None of them call an external
# service, touch the credential vault, or write to the books —
# they only read local approved-transaction history and a small local
# nexus table. All clearly labelled as estimates / tracking aids.


# Heuristic, name-based category classification shared by the unit-economics
# and 1099 tools. Deliberately transparent: the tools always show the
# category so the user can judge. Materials are checked first so a COGS
# line like "Freight & Shipping" counts as material, not service.
MATERIAL_CATEGORY_KEYWORDS = (
    "material", "parts", "resale", "inventory", "product", "cost of goods",
    "cogs", "freight", "shipping", "packaging", "sample", "merchandise",
    "raw ",
)
SERVICE_CATEGORY_KEYWORDS = (
    "contract", "subcontract", "professional fee", "consult", "freelance",
    "1099", "contractor", "labor", "design", "legal", "accounting",
    "bookkeeping", "marketing", "photograph", "videograph", "agency",
)


def category_kind(name):
    """"material", "service", or "other" for a category name."""
    lowered = (name or "").lower()
    if any(keyword in lowered for keyword in MATERIAL_CATEGORY_KEYWORDS):
        return "material"
    if any(keyword in lowered for keyword in SERVICE_CATEGORY_KEYWORDS):
        return "service"
    return "other"


def _active_expense_items():
    """Approved expense items for the active business (or all, if no
    business is active) — the shared basis for the local tools."""
    active = business_registry.active_business()
    active_id = active["id"] if active else None
    items = []
    for item in db.list_items(status="approved"):
        if item["direction"] != "expense":
            continue
        if (active_id is not None
                and item.get("business_id") not in (None, active_id)):
            continue
        items.append(item)
    return items


def _parse_money(raw):
    """Lenient dollar parse: strips $ and commas, returns a non-negative
    float or None."""
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


# render_tools_hub removed by the 2026-07-26 redesign: it was six
# static cards with no live data, a menu pretending to be a dashboard.
# /tools now redirects to the first real tool and the six pages reach
# each other through the shared sub-nav.

DATA_HEALTH_PREFIX = "data_health_"


def _health_prefix(source, account_id):
    return f"{DATA_HEALTH_PREFIX}{source}_{account_id}_"


def _month_window(today):
    start = today.replace(day=1)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    prior_day = min(today.day, calendar.monthrange(
        prior_end.year, prior_end.month)[1])
    return start.isoformat(), today.isoformat(), prior_start.isoformat(), \
        prior_end.replace(day=prior_day).isoformat()


def _format_date(value):
    if not value:
        return "Not recorded"
    try:
        parsed = date.fromisoformat(value[:10])
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    except ValueError:
        return value[:16].replace("T", " ")


def _gap_label(first, last):
    first_date = date.fromisoformat(first[:10])
    last_date = date.fromisoformat(last[:10])
    first_label = first_date.strftime("%b %d").replace(" 0", " ")
    last_label = last_date.strftime("%b %d").replace(" 0", " ")
    if first_date.year != last_date.year:
        first_label += f", {first_date.year}"
        last_label += f", {last_date.year}"
    gap_days = len(_business_days_between(first_date, last_date))
    return f"{first_label} &ndash; {last_label} ({gap_days} days)"


def _business_days_between(first, last):
    """Return weekdays in the inclusive date range."""
    days = []
    current = first
    while current <= last:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _activity_gaps(dates):
    """Find four-plus missing business days for normally daily accounts.

    A date-only gap is not enough: weekly and weekday-only accounts should
    stay calm. The account must have at least 10 active business dates and
    activity on at least 70% of the business days in its observed history.
    Weekends are never counted as missing business activity.
    """
    parsed = sorted({date.fromisoformat(value[:10]) for value in dates})
    if len(parsed) < 2:
        return []
    business_history = _business_days_between(parsed[0], parsed[-1])
    active_business = {value for value in parsed if value.weekday() < 5}
    if len(active_business) < 10:
        return []
    if len(active_business) / len(business_history) < 0.70:
        return []

    gaps = []
    missing = []
    for current in business_history:
        if current not in active_business:
            missing.append(current)
            continue
        if len(missing) >= 4:
            gaps.append((missing[0].isoformat(), missing[-1].isoformat()))
        missing = []
    if len(missing) >= 4:
        gaps.append((missing[0].isoformat(), missing[-1].isoformat()))
    return gaps


def _data_health_snapshot():
    adapter = banks.active_adapter()
    if adapter is None or not adapter.is_configured():
        return None, []
    try:
        accounts = adapter.list_accounts(adapter.stored_credentials())
    except Exception:
        return adapter, []
    items = db.list_items()
    today = date.today()
    month_start, month_end, prior_start, prior_end = _month_window(today)
    snapshots = []
    for index, account in enumerate(accounts):
        account_id = str(account["id"])
        account_items = [item for item in items
                         if item.get("source") in (adapter.id, "manual")
                         and (item.get("account_id") == account_id
                              or (not item.get("account_id") and len(accounts) == 1))]
        dates = sorted({item["date"] for item in account_items if item.get("date")})
        current_count = sum(month_start <= item["date"] <= month_end
                            for item in account_items)
        prior_count = sum(prior_start <= item["date"] <= prior_end
                          for item in account_items)
        prefix = _health_prefix(adapter.id, account_id)
        expected_raw = db.get_setting(prefix + "expected_monthly_count")
        expected = int(expected_raw or prior_count or current_count or 0)
        last_tx = max(dates, default=db.get_setting(prefix + "last_transaction_date"))
        sync_stamp = db.get_setting(prefix + "last_sync_date")
        gaps = _activity_gaps(dates)
        recent_cutoff = today - timedelta(days=7)
        recent = [item for item in account_items
                  if item.get("date", "") >= recent_cutoff.isoformat()]
        normally_active = prior_count > 0 or expected > 0
        warnings = []
        if not sync_stamp:
            warnings.append("No successful sync has been recorded")
        if expected and current_count < expected * 0.7:
            warnings.append(f"{current_count} this month vs {expected} expected")
        if normally_active and not recent:
            warnings.append("No transactions in the last 7 days")
        if gaps:
            warnings.append(f"Gap in transaction dates: {_gap_label(*gaps[-1])}")
        statement_raw = db.get_setting(prefix + "statement_balance")
        statement = float(statement_raw) if statement_raw else None
        balance_delta = None if statement is None else float(account.get("balance") or 0) - statement
        balance_flag = balance_delta is not None and abs(balance_delta) > 1
        if balance_flag:
            warnings.append(f"Balance differs from statement by {money(abs(balance_delta))}")
        missing_flag = (
            (expected and current_count < expected * 0.7)
            or (normally_active and not recent)
        )
        status = "Gap Detected" if gaps else ("Warning" if warnings else "Clean")
        snapshots.append({
            "account": account, "items": account_items, "last_tx": last_tx,
            "sync": sync_stamp, "current": current_count, "expected": expected,
            "prior": prior_count, "gaps": gaps, "warnings": warnings,
            "status": status, "statement": statement, "delta": balance_delta,
            "balance_flag": balance_flag, "missing_flag": missing_flag,
            "verified": db.get_setting(prefix + "statement_verified_at"),
            "index": index,
        })
    return adapter, snapshots


def _health_status(label):
    css = {"Clean": "tag ok", "Warning": "tag warn",
           "Gap Detected": "tag bad"}.get(label, "tag")
    return f'<span class="{css}">{escape(label)}</span>'


INVENTORY_TABS = (
    ("stock", "Stock on hand"),
    ("history", "Shopify history"),
)


def _inventory_nav(active):
    return ui.sub_nav([(key, label, f"/inventory?tab={key}")
                       for key, label in INVENTORY_TABS],
                      active, "Inventory sections")


def _inventory_stock_content(message=None):
    """Stock on hand, cost basis, and the manual acquisition form.

    Cost of goods can only post for a variant whose acquisitions are
    recorded here and posted to the Ledger — the Shopify cost sync knows
    what a unit is worth but not that any were bought, and crediting
    Inventory without a matching debit is what made it drift negative before.
    """
    position = inventory.inventory_position()
    titles = db.shopify_variant_titles()

    rows = ""
    for item in position["items"]:
        layers = item["layers"]
        layer_note = ""
        if len(layers) > 1:
            layer_note = (f'<div class="meta">{len(layers)} cost layers, '
                          f'oldest {ui.money(layers[0]["unit_cost"])}</div>')
        avg = (ui.money(item["average_cost"]) if item["average_cost"] is not None
               else "&mdash;")
        latest = (ui.money(item["latest_known_cost"])
                  if item["latest_known_cost"] is not None else "&mdash;")
        tone = "" if item["on_hand"] > 0 else " class=\"dash-flag\""
        rows += f"""
        <tr>
          <td class="vendor-name"
              data-sort-value="{escape(item["title"], quote=True)}">
            {escape(item["title"])}{layer_note}</td>
          <td class="num" data-sort-value="{item["on_hand"]}"><span{tone}>
            {item["on_hand"]}</span></td>
          <td class="num">{avg}</td>
          <td class="num">{latest}</td>
          <td class="num" data-sort-value="{item["value"]}">
            {ui.money(item["value"])}</td>
        </tr>"""

    if position["items"]:
        table = f"""
        <table class="table-hover">
          <thead><tr>
            <th data-sort="text">Item</th>
            <th class="num" data-sort="num">On hand</th>
            <th class="num" data-sort="num">Avg cost</th>
            <th class="num" data-sort="num">Latest Shopify cost</th>
            <th class="num" data-sort="num">Value</th>
          </tr></thead>
          <tbody>{rows}</tbody>
          <tfoot><tr><td>Total</td><td class="num"></td><td class="num"></td>
            <td class="num"></td>
            <td class="num">{ui.money(position["total_value"])}</td></tr></tfoot>
        </table>"""
    else:
        table = ui.empty_state(
            "No inventory recorded yet",
            "Record what you bought and what it cost, and cost of goods "
            "starts posting for sales of it. Until then those sales are "
            "held back rather than costed with a guess.")

    options = "".join(
        f'<option value="{escape(variant_id, quote=True)}">'
        f"{escape(title)}</option>"
        for variant_id, title in sorted(titles.items(), key=lambda kv: kv[1]))
    if not options:
        options = '<option value="">No Shopify variants synced yet</option>'
    funding_options = "".join(
        f'<option value="{escape(account["code"], quote=True)}">'
        f'{escape(account["code"])} {escape(account["name"])}</option>'
        for account in ledger.list_accounts()
        if account["type"] in {"asset", "liability"}
        and account["code"] != "1200")

    return f"""
    <div class="sub">What you hold, what it cost, and what is left. Cost of
      goods uses the cost the units were actually bought at, oldest first
      &mdash; never today's price applied backwards.</div>
    <div class="section">
      <div class="section-head"><h2>Stock on hand</h2>
        <span class="tag">as of {escape(position["as_of"])}</span>
        <span class="tag">{position["tracked_variants"]} tracked</span></div>
      {table}
    </div>
    <div class="section">
      <div class="section-head"><h2>Record what you bought</h2></div>
      <p class="hint">Adding stock here is what lets cost of goods post for
        later sales of it. Use the date you actually received it &mdash;
        a sale is only ever costed from stock acquired on or before it.</p>
      <form method="post" action="/inventory/acquire" class="field-row">
        <label class="grow">Item
          <select name="variant_id" required>{options}</select></label>
        <label>Quantity
          <input type="number" name="quantity" min="1" step="1" required
                 class="w-xs"></label>
        <label>Unit cost
          <input type="text" name="unit_cost" inputmode="decimal" required
                 placeholder="0.00" class="w-sm"></label>
        <label>Received
          <input type="date" name="effective_date" required></label>
        <label>Paid from
          <select name="funding_account_code" required>
            <option value="">Choose bank or payable</option>{funding_options}
          </select></label>
        <button class="approve" type="submit">Record</button>
      </form>
    </div>
    <div class="section">
      <div class="section-head"><h2>Correct a count</h2></div>
      <p class="hint">Breakage, shrinkage, or a miscount put right. An
        adjustment moves stock without inventing a cost for it, so it never
        makes cost of goods postable on its own.</p>
      <form method="post" action="/inventory/adjust" class="field-row">
        <label class="grow">Item
          <select name="variant_id" required>{options}</select></label>
        <label>Change by
          <input type="number" name="quantity" step="1" required
                 placeholder="-1" class="w-xs"></label>
        <label>Date
          <input type="date" name="effective_date" required></label>
        <label class="grow">Reason
          <input type="text" name="note" maxlength="200"
                 placeholder="e.g. two damaged in transit"></label>
        <button class="check" type="submit">Adjust</button>
      </form>
    </div>"""


def _inventory_history_content():
    """Historical Shopify figures, imported from Admin CSV exports.

    Labelled as reference throughout: these are aggregate numbers from
    Shopify's own reports covering the period before the ledger started,
    and they are deliberately never reconciled against ledger figures.
    """
    summary = shopify_analytics.summary()
    daily = shopify_analytics.trend("total_sales", "day")

    # The same warning in both states, deliberately. It matters most on
    # the empty one, which is what somebody reads immediately before
    # importing for the first time.
    reference_note = """
    <div class="flag flag-info">Shopify historical reference. These numbers
      come from Shopify's own reports, not from your ledger, and the two
      measure different things over different periods. Do not expect them
      to agree with Profit &amp; Loss.</div>"""

    if not summary:
        return ('<div class="sub">Aggregate figures exported from Shopify, '
                "covering the period before your ledger started. Reference "
                "only &mdash; these are never posted to your books.</div>"
                + reference_note
                + ui.empty_state(
                    "No history imported yet",
                    "Export a report from Shopify Admin &rarr; Analytics "
                    "&rarr; Reports, then upload the CSV below.")
                + _inventory_import_form())

    rows = "".join(
        f'<tr><td>{escape(row["metric_type"].replace("_", " ").title())}</td>'
        f'<td>{escape(row["period"])}</td>'
        f'<td class="num mixed">{escape(row["first_period"])}</td>'
        f'<td class="num mixed">{escape(row["last_period"])}</td>'
        f'<td class="num">{row["rows"]}</td></tr>'
        for row in summary)

    chart = ""
    if len(daily) > 1:
        values = [float(row["value"]) for row in daily]
        labels = [f'{row["period_start_date"]}: {ui.money(row["value"])}'
                  for row in daily]
        chart = charts.sparkline(
            values, labels=labels, last_label=ui.money(daily[-1]["value"]),
            aria="Historical Shopify total sales")

    return f"""
    <div class="sub">Aggregate figures exported from Shopify, covering the
      period before your ledger started. Reference only &mdash; these are
      not journal entries and are never reconciled against your books.</div>
    {reference_note}
    <div class="section">
      <div class="section-head"><h2>Total sales over time</h2></div>
      {chart or '<div class="none-note">Import daily total sales to see a trend.</div>'}
    </div>
    <div class="section">
      <div class="section-head"><h2>What has been imported</h2></div>
      <table class="table-hover">
        <thead><tr><th data-sort="text">Metric</th><th data-sort="text">Period</th>
          <th class="num" data-sort="date">From</th>
          <th class="num" data-sort="date">To</th>
          <th class="num" data-sort="num">Rows</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    {_inventory_import_form()}"""


def _inventory_import_form():
    return """
    <div class="section">
      <div class="section-head"><h2>Import a Shopify report</h2></div>
      <p class="hint">In Shopify Admin go to Analytics &rarr; Reports, open
        the report you want, set the date range, and use Export. Upload the
        CSV here. Re-importing an overlapping range is safe &mdash; rows
        replace rather than add up.</p>
      <form method="post" action="/inventory/import-history"
            enctype="multipart/form-data" class="field-row">
        <label>Report
          <select name="metric_type">
            <option value="total_sales">Total sales over time</option>
            <option value="order_count">Orders over time</option>
            <option value="sales_by_product">Sales by product</option>
          </select></label>
        <label>Period
          <select name="period">
            <option value="day">Daily</option>
            <option value="week">Weekly</option>
            <option value="month">Monthly</option>
          </select></label>
        <label>Period start
          <input type="date" name="period_start" class="w-md">
          <span class="hint">only for Sales by product</span></label>
        <label class="grow">CSV
          <input type="file" name="report" accept=".csv,text/csv" required></label>
        <button class="check" type="submit">Import</button>
      </form>
    </div>"""


def render_inventory_page(tab="stock", message=None, error=None):
    renderers = {"stock": _inventory_stock_content,
                 "history": _inventory_history_content}
    active = tab if tab in renderers else "stock"
    body = (_inventory_stock_content(message) if active == "stock"
            else _inventory_history_content())
    return render_shell("Inventory", "", _inventory_nav(active) + body,
                        message, error, wide=True)


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(tab: str = "stock", msg: str = None, err: str = None):
    db.init_db()
    return render_inventory_page(tab, message=msg, error=err)


@app.post("/inventory/acquire")
def inventory_acquire(variant_id: str = Form(""), quantity: str = Form(""),
                      unit_cost: str = Form(""), effective_date: str = Form(""),
                      funding_account_code: str = Form("")):
    db.init_db()
    try:
        inventory.record_acquisition(variant_id, quantity, unit_cost,
                                     effective_date, source="manual",
                                     funding_account_code=funding_account_code)
    except inventory.InventoryError as exc:
        return _redirect_inventory(err=str(exc))
    except (TypeError, ValueError):
        return _redirect_inventory(err="Check the quantity, cost, and funding account.")
    title = db.shopify_variant_titles().get(variant_id, variant_id)
    return _redirect_inventory(
        msg=f"Recorded {quantity} of {title}. Sales on or after "
            f"{effective_date} can now be costed.")


@app.post("/inventory/adjust")
def inventory_adjust(variant_id: str = Form(""), quantity: str = Form(""),
                     effective_date: str = Form(""), note: str = Form("")):
    db.init_db()
    try:
        inventory.record_adjustment(variant_id, quantity, effective_date, note)
    except inventory.InventoryError as exc:
        return _redirect_inventory(err=str(exc))
    except (TypeError, ValueError):
        return _redirect_inventory(err="Check the quantity.")
    return _redirect_inventory(msg="Count adjusted.")


@app.post("/inventory/import-history")
async def inventory_import_history(metric_type: str = Form("total_sales"),
                                   period: str = Form("day"),
                                   period_start: str = Form(""),
                                   report: UploadFile = File(...)):
    """Import one Shopify Admin CSV export.

    Writes only to shopify_analytics_history. Nothing here reaches the
    ledger — see shopify_analytics.py.
    """
    db.init_db()
    raw = await report.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return _redirect_inventory(
                err="That file could not be read as text.", tab="history")
    try:
        result = shopify_analytics.import_csv(
            text, metric_type, period, period_start=period_start or None)
    except ValueError as exc:
        return _redirect_inventory(err=str(exc), tab="history")

    message = (f"Imported {result['imported']} "
               f"{_plural(result['imported'], 'row')} from "
               f"{escape(report.filename or 'the file')}.")
    if result["errors"]:
        shown = "; ".join(result["errors"][:3])
        more = (f" and {len(result['errors']) - 3} more"
                if len(result["errors"]) > 3 else "")
        return _redirect_inventory(
            err=f"{message} {len(result['errors'])} rows were skipped: "
                f"{shown}{more}", tab="history")
    return _redirect_inventory(msg=message, tab="history")


TOOLS_SUBPAGES = [
    ("/tools/data-health", "Data Health"),
    ("/tools/accountant-export", "Accountant Export"),
    ("/tools/unit-economics", "Unit Economics"),
    ("/tools/nexus", "Nexus"),
    ("/tools/contractors", "Contractors"),
    ("/tools/audit", "Audit"),
]


def _tools_sibling_nav(current_path):
    """Compact cross-links between the 6 Tools sub-pages, same visual
    weight as the existing "All tools" back-link (.hint) — not a full
    nav bar, just enough to move between them without detouring through
    /tools each time."""
    return ui.sub_nav([(path, label, path) for path, label in TOOLS_SUBPAGES],
                      current_path, "Tools sections")


def render_data_health(message=None, error=None):
    adapter, rows = _data_health_snapshot()
    sibling_nav = _tools_sibling_nav("/tools/data-health")
    if adapter is None:
        body = ('<div class="empty"><p><strong>No bank accounts connected.</strong></p>'
                '<p>Connect a bank on <a href="/settings?tab=connections">Connections</a> '
                'to start checking data completeness.</p></div>')
        return render_shell("Tools", "", sibling_nav +
                            '<div class="sub">One calm answer: is your data complete?</div>' + body,
                            message, error, wide=True)
    if not rows:
        body = ('<div class="empty"><p><strong>No accounts found.</strong></p>'
                '<p>Re-test your <a href="/settings?tab=connections">bank connection</a> '
                'and reload this page.</p></div>')
        return render_shell("Tools", "", sibling_nav + body, message, error, wide=True)

    issues = []
    for row in rows:
        for warning in row["warnings"]:
            issues.append(f'{escape(row["account"]["name"])}: {escape(warning)}')
    coverage = (f'<div class="none-note">All accounts synced and balanced.</div>'
                if not issues else
                '<ol class="focus-list">' +
                "".join(f"<li>{issue}</li>" for issue in issues) + "</ol>")
    coverage_status = ('<span class="tag ok">All clear</span>'
                       if not issues else
                       f'<span class="tag bad">{len(issues)} issue(s) detected</span>')

    sync_rows = "".join(
        f'<tr><td class="vendor-name">{escape(row["account"]["name"])}</td>'
        f'<td>{_format_date(row["sync"])}</td><td>{_format_date(row["last_tx"])}</td>'
        f'<td class="num">{row["current"]} / {row["expected"] or "—"}</td>'
        f'<td>{_health_status(row["status"])}'
        + (f' <div class="meta">Gap: {_gap_label(*row["gaps"][-1])}</div>'
           if row["gaps"] else '')
        + (f' <a href="/settings?tab=connections">Re-test connection</a>'
           if row["warnings"] else "") + '</td></tr>'
        for row in rows)
    balance_rows = "".join(
        f'<tr><td class="vendor-name">{escape(row["account"]["name"])}</td>'
        f'<td class="num">{money(float(row["account"].get("balance") or 0))}</td>'
        + (f'<td class="num">{money(row["statement"])}</td>'
           if row["statement"] is not None
           else '<td class="num mixed">Not recorded</td>')
        + f'<td class="num">{money(row["delta"]) if row["delta"] is not None else "—"}</td>'
        f'<td>{_health_status("Warning" if row["balance_flag"] else "Clean")} '
        f'<span class="meta">verified {_format_date(row["verified"])}</span>'
        + (' <a href="/settings?tab=connections">Review connection</a>'
           if row["balance_flag"] else '') + '</td></tr>'
        for row in rows)
    missing_rows = "".join(
        f'<tr><td class="vendor-name">{escape(row["account"]["name"])}</td>'
        f'<td class="num">{row["current"]}</td><td class="num">{row["prior"]}</td>'
        f'<td>{_format_date(row["last_tx"])}</td>'
        f'<td>{_health_status("Warning" if row["missing_flag"] else "Clean")}'
        + (' <a href="/settings?tab=connections">Review connection</a>'
           if row["missing_flag"] else '') + '</td></tr>'
        for row in rows)
    import_forms = "".join(
        f'<form method="post" action="/tools/data-health/import" enctype="multipart/form-data" class="range-form">'
        f'<input type="hidden" name="account_id" value="{escape(str(row["account"]["id"]))}">'
        f'<label>{escape(row["account"]["name"])} <input type="file" name="statement" accept=".ofx,.qfx,.csv" required></label>'
        f'<button class="check" type="submit">Import statement</button></form>'
        for row in rows)
    content = f'''
    {sibling_nav}
    <div class="sub">One calm answer: is your data complete? Checks use local transaction history and the latest bank balances.</div>
    <div class="section"><div class="section-head"><h2>Coverage summary</h2>{coverage_status}</div>{coverage}</div>
    <div class="section"><div class="section-head"><h2>Sync health</h2></div><table><thead><tr><th data-sort="text">Account</th><th data-sort="date">Last sync</th><th data-sort="date">Last transaction</th><th class="num" data-sort="num">This month / expected</th><th data-sort="text">Status</th></tr></thead><tbody>{sync_rows}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Balance check</h2></div><table><thead><tr><th data-sort="text">Account</th><th class="num" data-sort="num">Bank balance</th><th class="num" data-sort="num">Last statement</th><th class="num" data-sort="num">Difference</th><th data-sort="text">Status</th></tr></thead><tbody>{balance_rows}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Missing transaction detection</h2></div><table><thead><tr><th data-sort="text">Account</th><th class="num" data-sort="num">This month</th><th class="num" data-sort="num">Same period last month</th><th data-sort="date">Last transaction</th><th data-sort="text">Status</th></tr></thead><tbody>{missing_rows}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Manual fallback</h2></div><div class="none-note">OFX, QFX, and CSV imports are deduplicated and go to Pending for approval.</div>{import_forms}</div>
    <p class="hint"><a href="/tools/data-health">All tools</a></p>'''
    return render_shell("Tools", "", content, message, error, wide=True)


def _statement_rows(raw, filename):
    text = raw.decode("utf-8-sig", errors="replace")
    lower = filename.lower()
    rows, statement_balance = [], None
    if lower.endswith((".ofx", ".qfx")):
        balance_match = re.search(r"<BALAMT>([^<\r\n]+)", text, re.I)
        if balance_match:
            statement_balance = float(balance_match.group(1).strip())
        for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>|</BANKTRANLIST))", text, re.I | re.S):
            def tag(name):
                match = re.search(rf"<{name}>([^<\r\n]+)", block, re.I)
                return match.group(1).strip() if match else ""
            rows.append({"date": tag("DTPOSTED")[:8], "amount": tag("TRNAMT"),
                         "description": tag("NAME") or tag("MEMO")})
    else:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            normalized = {str(key).strip().lower().replace(" ", "_"): (value or "").strip()
                          for key, value in row.items()}
            rows.append({"date": normalized.get("date") or normalized.get("transaction_date"),
                         "amount": normalized.get("amount") or normalized.get("signed_amount"),
                         "description": normalized.get("description") or normalized.get("vendor") or normalized.get("name")})
    return rows, statement_balance


@app.get("/tools/data-health", response_class=HTMLResponse)
def tools_data_health(msg: str = None, err: str = None):
    db.init_db()
    return render_data_health(message=msg, error=err)


@app.post("/tools/data-health/import")
async def tools_data_health_import(account_id: str = Form(""), statement: UploadFile = File(...)):
    db.init_db()
    adapter, rows = _data_health_snapshot()
    account = next((row["account"] for row in rows if str(row["account"]["id"]) == account_id), None)
    if adapter is None or account is None:
        return _redirect(err="That bank account is no longer available.")
    if not (statement.filename or "").lower().endswith((".ofx", ".qfx", ".csv")):
        return _redirect(err="Import an OFX, QFX, or CSV statement.")
    try:
        parsed, statement_balance = _statement_rows(await statement.read(), statement.filename)
        added = 0
        for row in parsed:
            raw_date = row.get("date") or ""
            txn_date = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                        if re.fullmatch(r"\d{8}", raw_date) else raw_date[:10])
            vendor = (row.get("description") or "").strip()[:200]
            amount = float(str(row.get("amount") or "").replace(",", "").replace("$", ""))
            if not txn_date or not vendor or not amount or db.transaction_match_exists(txn_date, abs(amount), vendor):
                continue
            direction, category, confidence, category_source = (
                check_bank.categorize_pending(
                    vendor, abs(amount), "expense" if amount < 0 else "income"
                )
            )
            db.insert_pending_item(
                "manual", vendor, abs(amount), txn_date, category,
                direction=direction, account_id=account_id,
                category_confidence=confidence if category else None,
                category_source=category_source if category else None,
            )
            added += 1
        prefix = _health_prefix(adapter.id, account_id)
        if statement_balance is not None:
            db.set_setting(prefix + "statement_balance", str(statement_balance))
            db.set_setting(prefix + "statement_verified_at", datetime.now(timezone.utc).isoformat())
    except (ValueError, IndexError, TypeError) as exc:
        return _redirect(err=f"Could not read that statement: {exc}")
    return _redirect(msg=f"Imported {added} transaction(s) to Pending.")


# ------------------------------------------------------- accountant export


def render_accountant_export_page(period, message=None, error=None,
                                  notion_url=None):
    data = accountant_export.gather_month_data(period)
    covered, total = accountant_export.notes_coverage(period)
    coverage_note = (
        f"{covered} of {total} transaction(s) have notes or evidence attached."
        if total else
        "No approved or pending transactions to document yet."
    )

    preview = f"""
    <div class="totals">
      <div><div class="label">Period</div>
        <div class="value mixed">{data["start_date"]} to {data["end_date"]}</div></div>
      <div><div class="label">Approved</div>
        <div class="value">{len(data["approved"])}</div></div>
      <div><div class="label">Pending (at close)</div>
        <div class="value">{len(data["pending"])}</div></div>
      <div><div class="label">Ledger revenue</div>
        <div class="value in">{money(data["total_in"])}</div></div>
      <div><div class="label">Ledger expenses</div>
        <div class="value out">{money(data["total_out"])}</div></div>
    </div>
    <div class="none-note">{escape(coverage_note)}</div>"""

    if accountant_export.PDF_AVAILABLE:
        pdf_action = (f'<a class="btn-link primary" '
                     f'href="/tools/accountant-export/pdf?period={period}">'
                     f"Download PDF Close Package</a>")
    else:
        pdf_action = (
            '<div class="none-note">PDF export needs the reportlab '
            "package, which isn't installed on this machine — run "
            "<span class=\"mono\">pip install reportlab</span> and "
            "reload this page.</div>")

    if notion_client.is_configured():
        notion_action = f"""
        <form method="post" action="/tools/accountant-export/notion"
              class="inline-form">
          <input type="hidden" name="period" value="{escape(period)}">
          <button class="btn-link" type="submit">Export to Notion</button>
        </form>"""
    else:
        notion_action = ('<div class="none-note">Connect Notion on '
                         '<a href="/settings?tab=connections">Connections</a> to export '
                         'this close package there.</div>')

    notion_link = (f'<div class="none-note">Notion page created: '
                   f'<a href="{escape(notion_url)}" target="_blank" '
                   f'rel="noreferrer">Open in Notion</a></div>'
                   if notion_url else "")

    existing_close = db.get_monthly_close(period)
    close_note = ""
    if existing_close:
        stamp = existing_close["closed_at"].replace("T", " ")[:16]
        close_note = (f'<div class="snapshot-meta">Last generated for this '
                     f"period: {escape(stamp)}.</div>")

    content = f"""
    {_tools_sibling_nav("/tools/accountant-export")}
    <div class="sub">A monthly close package for your accountant, built
      entirely from data already tracked here — nothing is uploaded
      anywhere. Pick a month, review the preview below, then download
      what you need.</div>
    <form class="range-form" method="get" action="/tools/accountant-export">
      <label>Month <input type="month" name="period" value="{period}"></label>
      <button class="check" type="submit">Update preview</button>
    </form>
    <div class="section">
      <div class="section-head"><h2>Preview</h2></div>
      {preview}
    </div>
    <div class="section">
      <div class="section-head"><h2>Export</h2></div>
      <div class="wizard-actions">
        {pdf_action}
        <a class="btn-link" href="/tools/accountant-export/csv-qb?period={period}">
          Download CSV (QuickBooks)</a>
        <a class="btn-link" href="/tools/accountant-export/csv-all?period={period}">
          Download CSV (all transactions)</a>
        {notion_action}
      </div>
      {notion_link}
      {close_note}
    </div>
    <p class="hint mt-4"><a href="/tools/data-health">All
      tools</a></p>"""
    return render_shell("Tools", "", content, message, error)


@app.get("/tools/accountant-export", response_class=HTMLResponse)
def accountant_export_page(period: str = None, msg: str = None, err: str = None,
                           notion_url: str = None):
    db.init_db()
    period = period or accountant_export.previous_complete_month()
    try:
        accountant_export.month_bounds(period)
    except accountant_export.ExportError:
        period = accountant_export.previous_complete_month()
    return render_accountant_export_page(
        period, message=msg, error=err, notion_url=notion_url)


@app.get("/tools/accountant-export/pdf")
def accountant_export_pdf(period: str):
    db.init_db()
    try:
        pdf_bytes = accountant_export.build_close_pdf(period)
    except accountant_export.ExportError as exc:
        return _redirect_accountant_export(err=str(exc))
    db.record_monthly_close(period, pdf_generated=True)
    filename = f"Klearing_Close_{period}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tools/accountant-export/csv-qb")
def accountant_export_csv_qb(period: str):
    db.init_db()
    try:
        csv_text = accountant_export.build_qb_csv(period)
    except accountant_export.ExportError as exc:
        return _redirect_accountant_export(err=str(exc))
    db.record_monthly_close(period, csv_generated=True)
    filename = f"Klearing_QB_{period}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/tools/accountant-export/csv-all")
def accountant_export_csv_all(period: str):
    db.init_db()
    try:
        csv_text = accountant_export.build_all_transactions_csv(period)
    except accountant_export.ExportError as exc:
        return _redirect_accountant_export(err=str(exc))
    db.record_monthly_close(period, csv_generated=True)
    filename = f"Klearing_AllTransactions_{period}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/tools/accountant-export/notion")
def accountant_export_notion(period: str = Form("")):
    db.init_db()
    try:
        result = notion_client.create_close_page(period)
    except accountant_export.ExportError as exc:
        return _redirect_accountant_export(err=str(exc))
    except Exception as exc:
        return _redirect_accountant_export(err=f"Notion export failed: {exc}")
    url = result["url"]
    return RedirectResponse(
        f"/tools/accountant-export?period={quote(period)}&notion_url={quote(url)}"
        f"&msg={quote('Close package exported to Notion.')}",
        status_code=303,
    )


# ---------------------------------------------------------- unit economics


def material_spend_summary():
    """Total / count / average of approved material-category spend, for
    context only. Klearing tracks totals by category, not units, so this
    is NOT a true per-unit cost — the tool says so and asks for one."""
    total = Decimal(0)
    count = 0
    for item in _active_expense_items():
        if category_kind(item["suggested_category"]) == "material":
            total += item["amount"]
            count += 1
    return {"total": total, "count": count,
            "avg": (total / count if count else Decimal(0))}


def render_unit_economics(unit_cost_raw=None, price_raw=None,
                          message=None, error=None):
    materials = material_spend_summary()
    unit_cost = _parse_money(unit_cost_raw)
    price = _parse_money(price_raw)

    if materials["count"]:
        context = (f'Your tracked material spend is '
                   f'<strong>{money(materials["total"])}</strong> across '
                   f'{materials["count"]} approved transaction(s) '
                   f'(avg {money(materials["avg"])} each). Klearing tracks '
                   f'totals by category, not units, so it can\'t derive a '
                   f'true per-unit cost — enter yours below.')
    else:
        context = ("No material-category spend is tracked yet, so there's "
                   "nothing to pull a per-unit cost from. Enter your unit "
                   "cost below.")

    result_html = ""
    if (unit_cost is None) != (price is None):
        # Exactly one field filled — say so instead of silently
        # rendering nothing after a Calculate click.
        missing = ("a target retail price" if price is None
                   else "a material cost per unit")
        result_html = (f'<div class="commentary">Enter {missing} as well '
                       f"to see a margin.</div>")
    elif unit_cost is not None and price is not None:
        if price <= 0:
            result_html = ('<div class="commentary">Enter a target retail '
                           'price above $0 to see a margin.</div>')
        else:
            profit = price - unit_cost
            margin = profit / price * 100
            tone = "in" if profit >= 0 else "out"
            result_html = f"""
            <div class="section">
              <div class="section-head"><h2>Estimated gross margin</h2>
                <span class="tag">estimate only</span></div>
              <div class="totals">
                <div><div class="label">Unit cost</div>
                  <div class="value">-{money(unit_cost)}</div></div>
                <div><div class="label">Retail price</div>
                  <div class="value">{money(price)}</div></div>
                <div><div class="label">Gross profit / unit</div>
                  <div class="value {tone}">{'+' if profit >= 0 else '-'}{money(abs(profit))}</div></div>
                <div><div class="label">Gross margin</div>
                  <div class="value">{margin:.1f}%</div></div>
              </div>
              <div class="commentary">Doesn't include shipping, fees,
                labor, or overhead.</div>
            </div>"""

    uc_val = "" if unit_cost_raw is None else escape(str(unit_cost_raw))
    pr_val = "" if price_raw is None else escape(str(price_raw))
    content = f"""
    {_tools_sibling_nav("/tools/unit-economics")}
    <div class="sub"><strong>Estimate only</strong> — based on your tracked
      material costs and an entered price. {context}</div>
    <form class="range-form" method="get" action="/tools/unit-economics">
      <label>Material cost per unit
        <div class="prefix-input"><span class="prefix">$</span>
          <input type="number" step="0.01" min="0" name="unit_cost"
                 value="{uc_val}" placeholder="e.g. 12.50"></div></label>
      <label>Target retail price
        <div class="prefix-input"><span class="prefix">$</span>
          <input type="number" step="0.01" min="0" name="price"
                 value="{pr_val}" placeholder="e.g. 28.00"></div></label>
      <button class="check" type="submit">Calculate</button>
    </form>
    {result_html}
    <p class="hint mt-4"><a href="/tools/data-health">All
      tools</a></p>"""
    return render_shell("Tools", "", content, message, error)


@app.get("/tools/unit-economics", response_class=HTMLResponse)
def tools_unit_economics(unit_cost: str = None, price: str = None):
    db.init_db()
    return render_unit_economics(unit_cost_raw=unit_cost, price_raw=price)


# ------------------------------------------------------- sales-tax nexus


# A neutral reference for economic-nexus rules. Thresholds vary by state
# and change, so Klearing never hardcodes dollar figures — it links here
# and lets the user enter the threshold they've confirmed for each state.
NEXUS_REFERENCE_URL = (
    "https://www.salestaxinstitute.com/resources/economic-nexus-state-guidelines"
)
NEXUS_APPROACHING_PCT = 80.0


def _nexus_status(revenue, threshold):
    """(label, css_class) for a state row. No hardcoded dollar figure —
    purely revenue vs the user's own entered threshold."""
    if threshold is None or threshold <= 0:
        return "No threshold set", "tag off"
    pct = revenue / threshold * 100
    if pct >= 100:
        # Reached and approaching are different compliance situations —
        # reached gets the expense-red tag, approaching keeps the amber
        # warn tag, so the two are distinguishable at a glance.
        return f"Threshold reached ({pct:.0f}%)", "tag bad"
    if pct >= NEXUS_APPROACHING_PCT:
        return f"Approaching ({pct:.0f}%)", "jump-tag"
    return f"{pct:.0f}% of threshold", "tag ok"


def render_nexus(message=None, error=None):
    states = db.list_nexus_states()
    if not states:
        rows_html = ('<tr><td colspan="5" class="none-note">No states tracked '
                     'yet. Add one below.</td></tr>')
    else:
        rows_html = ""
        for state in states:
            label, css = _nexus_status(state["revenue"], state["threshold"])
            threshold_val = ("" if state["threshold"] is None
                             else f'{state["threshold"]:.2f}')
            rows_html += f"""
            <tr>
              <form method="post" action="/tools/nexus/update/{state["id"]}"
                    id="nexus-{state["id"]}"></form>
              <td><input type="text" name="state" value="{escape(state["state"])}"
                    form="nexus-{state["id"]}" class="w-sm"></td>
              <td class="num"><input type="number" step="0.01" min="0"
                    name="revenue" value="{state["revenue"]:.2f}"
                    form="nexus-{state["id"]}" class="w-sm"></td>
              <td class="num"><input type="number" step="0.01" min="0"
                    name="threshold" value="{threshold_val}"
                    form="nexus-{state["id"]}" placeholder="not set"
                    class="w-sm"></td>
              <td><span class="{css}">{escape(label)}</span></td>
              <td class="actions">
                <button class="check" type="submit"
                        form="nexus-{state["id"]}">Save</button>
                <form method="post" action="/tools/nexus/delete/{state["id"]}"
                      {confirm_attrs('Remove this state from the tracker?', busy_label='Deleting…')}>
                  <button class="deny" type="submit">Delete</button>
                </form>
              </td>
            </tr>"""

    content = f"""
    {_tools_sibling_nav("/tools/nexus")}
    <div class="sub">A manual tracking aid for economic sales-tax nexus —
      <strong>not tax advice</strong>. Thresholds vary by state and change
      over time, so enter the current threshold you've confirmed for each
      state yourself. Reference:
      <a href="{NEXUS_REFERENCE_URL}" target="_blank" rel="noopener">economic
      nexus state guidelines</a> (verify current figures with each state).</div>
    <div class="section">
      <table class="table-hover">
        <thead><tr><th data-sort="text">State</th><th class="num" data-sort="num">Revenue into state</th>
          <th class="num" data-sort="num">Your threshold</th><th data-sort="text">Status</th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div class="section">
      <div class="section-head"><h2>Add a state</h2></div>
      <form method="post" action="/tools/nexus/add" class="range-form">
        <label>State <input type="text" name="state" required
          placeholder="e.g. Iowa" class="w-md"></label>
        <label>Revenue into state
          <div class="prefix-input"><span class="prefix">$</span>
            <input type="number" step="0.01" min="0" name="revenue"
                   value="0"></div></label>
        <label>Threshold (optional)
          <div class="prefix-input"><span class="prefix">$</span>
            <input type="number" step="0.01" min="0" name="threshold"
                   placeholder="e.g. 100000"></div></label>
        <button class="save-btn" type="submit">Add state</button>
      </form>
    </div>
    <p class="hint"><a href="/tools/data-health">All tools</a></p>"""
    return render_shell("Tools", "", content, message, error)


@app.get("/tools/nexus", response_class=HTMLResponse)
def tools_nexus(msg: str = None, err: str = None):
    db.init_db()
    return render_nexus(message=msg, error=err)


@app.post("/tools/nexus/add")
def tools_nexus_add(state: str = Form(""), revenue: str = Form(""),
                    threshold: str = Form("")):
    db.init_db()
    name = (state or "").strip()[:60]
    if not name:
        return _redirect_nexus(err="Enter a state name.")
    db.add_nexus_state(name, _parse_money(revenue) or 0.0,
                       _parse_money(threshold))
    return _redirect_nexus(msg=f"Added {name}.")


@app.post("/tools/nexus/update/{state_id}")
def tools_nexus_update(state_id: int, state: str = Form(""),
                       revenue: str = Form(""), threshold: str = Form("")):
    db.init_db()
    name = (state or "").strip()[:60]
    if not name:
        return _redirect_nexus(err="A state needs a name.")
    db.update_nexus_state(state_id, name, _parse_money(revenue) or 0.0,
                          _parse_money(threshold))
    return _redirect_nexus(msg=f"Updated {name}.")


@app.post("/tools/nexus/delete/{state_id}")
def tools_nexus_delete(state_id: int):
    db.init_db()
    db.delete_nexus_state(state_id)
    return _redirect_nexus(msg="State removed.")


# ------------------------------------------------------ 1099 / contractors


CONTRACTOR_1099_THRESHOLD = 600.0


def contractor_candidates():
    """Vendors booked to a service/contractor category and paid at least
    $600 in the current calendar year (active business). Informational —
    a real 1099-NEC decision depends on entity type and W-9 details this
    can't see."""
    year = date.today().year
    prefix = f"{year:04d}-"
    agg = {}
    for item in _active_expense_items():
        if not item["date"].startswith(prefix):
            continue
        if category_kind(item["suggested_category"]) != "service":
            continue
        entry = agg.setdefault(
            item["vendor"],
            {"total": 0.0, "count": 0, "categories": set()},
        )
        entry["total"] += item["amount"]
        entry["count"] += 1
        entry["categories"].add(item["suggested_category"])
    rows = [
        {"vendor": vendor, "total": entry["total"], "count": entry["count"],
         "categories": ", ".join(sorted(entry["categories"]))}
        for vendor, entry in agg.items()
        if entry["total"] >= CONTRACTOR_1099_THRESHOLD
    ]
    rows.sort(key=lambda row: -row["total"])
    return rows, year


def _merge_gusto_contractors(rows):
    """Fold Gusto contractor payments into the local 1099 candidate list.
    Dedupe by name similarity (rapidfuzz, same threshold as vendor
    memory) — a contractor in both sources keeps ONE row, flagged as a
    potential double-count (the same payment may have cleared the bank
    into the ledger AND be recorded in Gusto). Returns (rows, note_html)."""
    if not gusto_client.is_configured():
        return rows, ('<div class="none-note">Connect Gusto on the '
                      '<a href="/settings?tab=connections">Connections page</a> to '
                      "sync contractor payments into this list "
                      "automatically.</div>")
    try:
        gusto_rows = gusto_client.contractor_payments_this_year()
    except Exception:
        return rows, ('<div class="none-note">Gusto couldn\'t be reached '
                      "— its contractor payments aren't included right "
                      'now. <a href="/settings?tab=connections">Re-test connection</a>'
                      ".</div>")
    from rapidfuzz import fuzz
    merged = [dict(row) for row in rows]
    for gusto_row in gusto_rows:
        match = None
        for row in merged:
            if fuzz.WRatio(gusto_row["vendor"], row["vendor"]) >= 85:
                match = row
                break
        if match is not None:
            match["double_count"] = True
            match["gusto_total"] = gusto_row["total"]
        elif gusto_row["total"] >= CONTRACTOR_1099_THRESHOLD:
            merged.append({
                "vendor": gusto_row["vendor"],
                "total": gusto_row["total"],
                "count": gusto_row["count"],
                "categories": "Contractor payment",
                "gusto_only": True,
            })
    merged.sort(key=lambda row: -row["total"])
    return merged, ""


def render_contractors(message=None, error=None):
    rows, year = contractor_candidates()
    rows, gusto_note = _merge_gusto_contractors(rows)
    handoff_status = contractor_handoff.checklist_status(len(rows))
    if not rows:
        body = f"""
        <div class="empty">
          <p><strong>No 1099 candidates for {year}.</strong></p>
          <p>Vendors booked to a service/contractor category and paid
            {money(CONTRACTOR_1099_THRESHOLD)} or more this year would
            appear here.</p>
        </div>"""
    else:
        table_rows = ""
        for row in rows:
            # Same drill-down affordance as the Vendors table: the name
            # links to that vendor's itemized approved expenses. Gusto-
            # only rows have no local transaction history behind them, so no link.
            row_cls = "row-link"
            if row.get("gusto_only"):
                name_cell = (f'{escape(row["vendor"])}'
                             f'<span class="tag">from Gusto</span>')
                row_cls = ""
            else:
                name_cell = (f'<a href="/vendors/detail?vendor='
                             f'{quote(row["vendor"])}">'
                             f'{escape(row["vendor"])}</a>')
            if row.get("double_count"):
                name_cell += (f'<span class="jump-tag">&#9650; also in '
                              f'Gusto ({money(row["gusto_total"])}) — '
                              f"check for double-count</span>")
            table_rows += f"""
            <tr class="{row_cls}">
              <td class="vendor-name">{name_cell}</td>
              <td>{escape(row["categories"])}</td>
              <td class="num">{money(row["total"])}</td>
              <td class="num">{row["count"]}</td>
            </tr>"""
        body = f"""
        <div class="section">
          <div class="section-head"><h2>Possible 1099 vendors ({year})</h2>
            <span class="tag">informational</span></div>
          <table class="table-hover">
            <thead><tr><th data-sort="text">Vendor</th><th data-sort="text">Category</th>
              <th class="num" data-sort="num">Paid this year</th>
              <th class="num" data-sort="num">Payments</th></tr></thead>
            <tbody>{table_rows}</tbody>
          </table>
        </div>"""
    content = f"""
    {_tools_sibling_nav("/tools/contractors")}
    <p class="hint mt-4"><strong>{handoff_status}</strong>
      <a href="{contractor_handoff.TRACKER_PATH}">Open the Contractors tracker</a>
      for the candidate details. Klearing cannot determine 1099-NEC eligibility
      without the vendor's entity type and W-9.</p>
    <div class="sub">Vendors booked to a service/contractor category and
      paid <strong>{money(CONTRACTOR_1099_THRESHOLD)}+</strong> in {year}
      (active business). <strong>Informational only, not tax advice</strong> —
      whether a 1099-NEC is actually required depends on the vendor's entity
      type and W-9, which Klearing can't see. Categories are matched by name,
      so confirm each one yourself.</div>
    {body}
    {gusto_note}
    <p class="hint mt-4"><a href="/tools/data-health">All
      tools</a></p>"""
    return render_shell("Tools", "", content, message, error)


@app.get("/tools/contractors", response_class=HTMLResponse)
def tools_contractors(msg: str = None, err: str = None):
    db.init_db()
    return render_contractors(message=msg, error=err)


# --------------------------------------------------------------- audit log


AUDIT_STATUS_LABEL = {
    "pending": ("Pending", "tag"),
    "approved": ("Approved", "tag ok"),
    "denied": ("Denied", "tag off"),
    "skipped": ("Skipped", "tag off"),
}


def build_audit_trail():
    """Every item Klearing has seen, newest activity first, enriched with
    the original posting time when known. Read-only, straight from
    pending_items — no separate audit tracking."""
    posted = {row["mercury_transaction_id"]: row
              for row in db.list_wave_posted()}
    items = sorted(db.list_items(), key=lambda i: i["id"], reverse=True)
    trail = []
    for item in items:
        ext = item.get("external_transaction_id")
        posted_row = posted.get(ext) if ext else None
        trail.append({**item,
                      "posted_at": (posted_row or {}).get("posted_at")})
    return trail


AUDIT_LIMIT = 200


def render_audit(message=None, error=None, start="", end="", q=""):
    # Same cap-with-a-note pattern as the vendor detail page
    # (VENDOR_DETAIL_LIMIT): the trail grows forever, the page shouldn't.
    full_trail = build_audit_trail()
    trail = full_trail[:AUDIT_LIMIT]
    cap_note = ""
    if len(full_trail) > AUDIT_LIMIT:
        cap_note = (f'<div class="none-note">Showing the most recent '
                    f"{len(trail)} of {len(full_trail)} decisions.</div>")

    # Filter within the capped window — the cap keeps render time bounded,
    # the filter only narrows what's shown, it never reaches past the cap.
    start = clean_date(start, "") if start else ""
    end = clean_date(end, "") if end else ""
    q = (q or "").strip()[:100]
    filtered = trail
    if start:
        filtered = [i for i in filtered if i["date"] >= start]
    if end:
        filtered = [i for i in filtered if i["date"] <= end]
    if q:
        needle = q.lower()

        def _haystack(item):
            label, _ = AUDIT_STATUS_LABEL.get(
                item["status"], (item["status"], "tag"))
            return " ".join((item["date"], item["vendor"],
                             item.get("notes") or "", signed_money(item),
                             item["suggested_category"], label)).lower()
        filtered = [i for i in filtered if needle in _haystack(i)]

    filter_form = ""
    if trail:
        filter_form = f"""
    <form class="range-form" method="get" action="/tools/audit">
      <label>From <input type="date" name="start" value="{start}"></label>
      <label>To <input type="date" name="end" value="{end}"></label>
      <label>Search <input type="text" name="q" value="{escape(q)}"
        placeholder="Vendor, category, decision"></label>
      <button class="check" type="submit">View</button>
    </form>"""

    if not trail:
        body = """
        <div class="empty">
          <p><strong>No history yet.</strong></p>
          <p>Once transactions are suggested and you approve or deny them,
            every decision shows up here.</p>
        </div>"""
    elif not filtered:
        body = f"""
        <div class="section">
          {cap_note}
          <div class="none-note">No entries match this range.</div>
        </div>"""
    else:
        rows = ""
        for item in filtered:
            label, css = AUDIT_STATUS_LABEL.get(
                item["status"], (item["status"], "tag"))
            # Approved items store the category actually booked; pending and
            # denied items still carry the original suggestion.
            if item["status"] == "approved":
                category = f'booked as {escape(item["suggested_category"])}'
            else:
                category = (f'suggested '
                             f'{escape(item.get("suggested_category") or "No suggestion")}')
            posted_sub = ""
            if item.get("posted_at"):
                posted_sub = (f'<div class="meta">posted '
                              f'{escape(item["posted_at"][:10])}</div>')
            note = escape(item.get("notes") or "")
            note_sub = f'<div class="meta">note: {note}</div>' if note else ""
            rows += f"""
            <tr>
              <td><span class="mono">{escape(item["date"])}</span>{posted_sub}</td>
              <td class="vendor-name">{escape(item["vendor"])}{note_sub}</td>
              <td class="num">{escape(signed_money(item))}</td>
              <td>{category}</td>
              <td><span class="{css}">{escape(label)}</span></td>
            </tr>"""
        body = f"""
        <div class="section">
          {cap_note}
          <table class="table-hover">
            <thead><tr><th data-sort="date">When</th><th data-sort="text">Vendor</th><th class="num" data-sort="num">Amount</th>
              <th data-sort="text">Category</th><th>Decision</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""
    content = f"""
    {_tools_sibling_nav("/tools/audit")}
    <div class="sub">Every transaction Klearing has seen, newest first, with
      the category and the decision you made. Read-only. Approved items show
      the category they were booked under; pending and denied items show the
      original suggestion. Dates are the transaction date (with the original post
      date shown when recorded).</div>
    {filter_form}
    {body}
    <p class="hint mt-4"><a href="/tools/data-health">All
      tools</a></p>"""
    return render_shell("Tools", "", content, message, error)


@app.get("/tools/audit", response_class=HTMLResponse)
def tools_audit(msg: str = None, err: str = None, start: str = None,
                end: str = None, q: str = None):
    db.init_db()
    return render_audit(message=msg, error=err, start=start or "",
                        end=end or "", q=q or "")


def _record_approval(item, chosen_category, chosen_account, chosen_notes,
                     chosen_evidence, entry=None):
    """Record one approved item's local bookkeeping.

    Records the category and its correction, the
    note, the evidence type, the status, and what the vendor taught us.
    None of it had another home, which is why the function was gutted
    rather than deleted.

    Extracted so bulk approve runs exactly the same code rather than a
    second copy of it. This is the money path; two implementations that
    drift apart is the failure mode worth designing against.
    """
    item_id = item["id"]
    ext_id = item.get("external_transaction_id")
    active = business_registry.active_business()
    business_id = active["id"] if active else ""

    # The money's in/out direction is a bank fact, but whether a deposit is
    # revenue or an owner transfer is decided by the category the user
    # picked -- an equity category makes it a transfer, anything else
    # income. This lets the dropdown correct a wrong transfer suggestion
    # instead of silently keeping the guessed classification.
    direction = item["direction"]
    if direction != "expense" and chosen_account is not None:
        direction = ("transfer" if chosen_account["type"] == "equity"
                     else "income")

    if (item.get("suggested_category")
            and item["suggested_category"] != chosen_category):
        db.record_category_correction(
            categorizer.correction_pattern(item["vendor"]), chosen_category)
    db.set_category(item_id, chosen_category, direction=direction)
    db.set_notes(item_id, chosen_notes)
    db.set_evidence_type(item_id, chosen_evidence)
    # wave_transaction_id stays NULL now: it is a preserved legacy audit field,
    # saying so is the honest record. The 23 rows that carry a real one are
    # history and keep it.
    is_test = item["vendor"].startswith(TEST_VENDOR_PREFIX)
    db.set_status(item_id, "approved", business_id=business_id,
                  wave_transaction_id=TEST_TRANSACTION_AUDIT_ID if is_test
                  else None)
    db.upsert_vendor_memory(item["vendor"],
                            chosen_account["id"] if chosen_account else "",
                            chosen_category, direction=direction,
                            business_id=business_id)
    # Remember we recorded this bank transaction, so a future approve of
    # the same id (here or in a restored copy) trips the duplicate guard.
    # The Ledger's own dedup already makes a second posting impossible; this
    # is what puts the warning in front of the user first.
    if ext_id and entry is not None:
        db.record_wave_post(ext_id, str(entry["id"]), "klearing")
    return True


LEDGER_APPROVAL_CREATED_BY = "review-approval"
CHECKING_ACCOUNT_CODE = "1010"


def _approval_source_reference(item):
    """The dedup key for one approved item's journal entry.

    Bank items deliberately reuse the reference ledger_sync already posts
    under, so ledger_source_imports is the single guard against the two
    writers both claiming one transaction -- no second mechanism, and no
    way for them to disagree about what "already posted" means. Manual
    items have no bank id and ledger_sync never sees them, so they get
    their own namespace.
    """
    ext_id = item.get("external_transaction_id")
    if ext_id:
        if item.get("source") == "simplefin":
            return (f"bank:simplefin:{item.get('account_id') or ''}:"
                    f"{ext_id}")
        return f"plaid:transaction:{ext_id}"
    return f"klearing:pending:{item['id']}"


def _ledger_skip_reason(item):
    """Why this approved item must not post to the Ledger itself, or "".

    Approval is the only writer of bank transactions now, so the two
    suppressions _sync_plaid used to apply have to live here instead --
    otherwise removing it turns them off. Both defer to a poster that
    records the same money more completely, and both reuse the very
    function the sync used, so the two cannot drift apart on what counts
    as an already-claimed deposit.
    """
    claimed_by_invoice = _approval_source_reference(item) in \
        db.active_match_references()
    if claimed_by_invoice:
        # A pending or applied match posts Dr Checking / Cr A/R under its
        # own reference, which ledger_source_imports cannot dedup against
        # ours. Posting both would count the deposit twice. A dismissed
        # match is deliberately absent from that set, so a dismissed
        # deposit still lands here as ordinary income.
        return ("This deposit is matched to an invoice, so it is recorded "
                "as that payment rather than as separate income.")

    payout = ledger_sync.deposit_claimed_by_payout(item)
    if payout is not None:
        return (f'This is Shopify payout #{payout["payout_id"]}, so it is '
                "recorded with its processing fees when payouts are synced.")

    if ledger_sync.claimed_by_transfer_sync(item):
        # A move between two of your own accounts is posted as one entry
        # across both, which Review cannot do from a single side.
        return ("This is a transfer between your own accounts, so it is "
                "recorded as one movement across both of them.")
    return ""


def _post_one_to_ledger(item, chosen_account, chosen_notes):
    """Post one approved item to the native Ledger as a balanced entry.

    Returns the entry, or None when create_sourced_journal_entry declined
    because this source was already posted -- which is not an error.

    Dr/Cr follows the bank's own fact about the money, not the category's
    type. item["direction"] is read before _record_approval reclassifies
    it for display, because a transfer in is still money in. Money out
    debits the chosen account and credits the bank; money in does the
    reverse. Every account type then falls out correctly on its own: an
    owner contribution credits equity, a materials purchase debits
    inventory.
    """
    source = item.get("source") or ""
    bank_account_id = db.ensure_bank_ledger_account(
        source, item.get("account_id"))
    is_manual_cash = source == "manual" and not item.get("account_id")
    if bank_account_id is None and not is_manual_cash:
        account_label = item.get("account_id") or "unknown"
        source_label = source or "unknown source"
        raise ledger.LedgerValidationError(
            f"Bank account {account_label} from {source_label} has not been "
            "mapped to a Ledger account. Map it before approving this "
            "transaction.")
    if is_manual_cash:
        # A true manual entry has no source account to map. Its explicit cash
        # entry convention is distinct from guessing for a connected bank row.
        bank_account_id = ledger.account_by_code(CHECKING_ACCOUNT_CODE)["id"]
    amount = str(item["amount"])
    money_out = item["direction"] == "expense"
    if money_out:
        debit_id, credit_id = chosen_account["id"], bank_account_id
    else:
        debit_id, credit_id = bank_account_id, chosen_account["id"]

    memo = item["vendor"]
    if chosen_notes:
        memo = f"{memo} - {chosen_notes}"
    return ledger.create_sourced_journal_entry(
        item["date"],
        memo[:200],
        [
            {"account_id": debit_id, "debit_amount": amount},
            {"account_id": credit_id, "credit_amount": amount},
        ],
        source_reference=_approval_source_reference(item),
        created_by=LEDGER_APPROVAL_CREATED_BY,
    )


def _post_one_approved(item, chosen_category, chosen_account, chosen_notes,
                       chosen_evidence):
    """Record one approved item everywhere it belongs. Returns a note when
    the money was recorded by a different poster, otherwise "".

    The single and bulk approve paths both call only this, so the money
    path cannot grow a second implementation -- the failure mode this
    module is designed against.

    The Ledger write is fatal. Nothing sits behind it, so an approval
    that cannot reach the books must fail and stay pending -- otherwise it
    reports success having recorded nothing anywhere.

    Raises RuntimeError with a human-readable reason; both handlers already
    catch it and reopen the item with its drawer intact.
    """
    is_test = item["vendor"].startswith(TEST_VENDOR_PREFIX)
    if chosen_account is None and not is_test:
        # A free-text or legacy category with no matching account. Posting
        # would mean choosing an account by name resemblance.
        raise RuntimeError(
            f'"{chosen_category}" is not one of your categories, so there is '
            "nowhere to record it. Pick one from the list.")

    entry = None
    deferred = "" if is_test else _ledger_skip_reason(item)
    if not deferred and not is_test:
        try:
            entry = _post_one_to_ledger(item, chosen_account, chosen_notes)
        except ledger.LedgerValidationError as exc:
            error_alerts.record_failure(
                f"Ledger post for pending item {item['id']}", exc)
            raise RuntimeError(f"This could not be recorded: {exc}") from exc

    _record_approval(item, chosen_category, chosen_account, chosen_notes,
                     chosen_evidence, entry=entry)
    return deferred


def _redirect_review_item(item_id, err):
    """Back to the queue with this item's drawer reopened.

    A failed post should not cost the user their place. Everything they
    had typed is re-rendered from the item, and the drawer opens on it
    with the error above — the brief's "failed posting with preserved
    user context".
    """
    return RedirectResponse(
        f"/review?tab=transactions&review={item_id}&err={quote(err)}",
        status_code=303,
    )


@app.post("/approve/{item_id}")
def approve(item_id: int, category: str = Form(None), notes: str = Form(""),
            evidence_type: str = Form(""), force: str = Form(""),
            review_return: str = Form("")):
    item = db.get_item(item_id)
    if item is None or item["status"] != "pending":
        return _redirect(err="That item is no longer pending.")

    # A submitted choice always wins. Medium-confidence suggestions must be
    # actively selected; high-confidence rules may safely pre-select.
    submitted_category = (category or "").strip()
    suggestions_enabled = db.get_setting("auto_categorize_enabled") != "0"
    if submitted_category:
        chosen_category = submitted_category
    elif item.get("category_confidence") == "medium":
        chosen_category = ""
    elif suggestions_enabled:
        chosen_category = (item.get("suggested_category") or "").strip()
    else:
        chosen_category = ""
    chosen_account, chosen_category = resolve_submitted_category(chosen_category)
    if not chosen_category:
        return _redirect_review_item(
            item_id, "Pick a category before recording this one.")
    chosen_notes = (notes or "").strip()[:500]
    chosen_evidence = (evidence_type or "").strip()
    if chosen_evidence not in accountant_export.EVIDENCE_LABELS:
        chosen_evidence = None

    # Duplicate-post guard: if this bank transaction was already posted
    # by this app or, via a restored backup, an earlier one,
    # don't silently create a second copy — send the user to a warning
    # row that offers Skip or an explicit override. Only bank-sourced
    # items have an external id; manual/test items skip this entirely.
    ext_id = item.get("external_transaction_id")
    if ext_id and force != "1" and db.get_wave_posted(ext_id):
        return RedirectResponse(
            f"/review?tab=transactions&dup={item_id}"
            f"&cat={quote(chosen_category)}",
            status_code=303,
        )

    is_test = item["vendor"].startswith(TEST_VENDOR_PREFIX)
    try:
        recorded_elsewhere = _post_one_approved(
            item, chosen_category, chosen_account, chosen_notes,
            chosen_evidence)
    except RuntimeError as exc:
        return _redirect_review_item(item_id, str(exc))

    if is_test:
        return _redirect(
            msg=f'Approved (test item, kept out of your books): '
                f'{item["vendor"]} {signed_money(item)} as {chosen_category}.')
    note = " (recorded anyway — duplicate override)" if force == "1" else ""
    message = (f'Approved: {item["vendor"]} {signed_money(item)} recorded as '
               f'{chosen_category}.{note}')
    if recorded_elsewhere:
        # Not a failure: the money is on the books, posted by whichever
        # source records it more completely.
        return _redirect(msg=f"{message} {recorded_elsewhere}")
    return _redirect(msg=message)


@app.post("/review/deny")
async def review_bulk_deny(request: Request):
    """Dismiss every selected transaction.

    Selection arrives as repeated ``ids`` fields, which table.js copies
    into this form from the checked rows.
    """
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).strip().isdigit()]
    if not ids:
        return _redirect(err="Nothing was selected.")

    denied = 0
    for item_id in ids:
        item = db.get_item(item_id)
        if item is None or item["status"] != "pending":
            continue
        db.set_status(item_id, "denied")
        denied += 1

    skipped = len(ids) - denied
    msg = f"Dismissed {denied} {_plural(denied, 'transaction')}."
    if skipped:
        # Something else moved them — a second tab, or the daily check.
        # Saying so is better than reporting a count that silently
        # disagrees with what the user selected.
        msg += f" {skipped} had already been handled."
    return _redirect(msg=msg)


@app.post("/review/approve")
async def review_bulk_approve(request: Request):
    """Record every selected transaction in the native ledger.

    Each row carries its own category, note and evidence — table.js
    copies them in as ``category_<id>`` and friends, because the inputs
    themselves belong to their row's individual approve form and an
    input can only be associated with one form.

    Items are processed one at a time through the same guards the single
    approve uses. Anything that cannot go through is counted and named
    rather than silently dropped: a bulk action that reports success for
    work it did not do is worse than one that does nothing.
    """
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).strip().isdigit()]
    if not ids:
        return _redirect(err="Nothing was selected.")

    suggestions_enabled = db.get_setting("auto_categorize_enabled") != "0"

    posted, no_category, duplicates, gone, failed = 0, [], [], 0, []
    deferred = []
    for item_id in ids:
        item = db.get_item(item_id)
        if item is None or item["status"] != "pending":
            gone += 1
            continue

        chosen = (form.get(f"category_{item_id}") or "").strip()
        if not chosen:
            if item.get("category_confidence") == "medium":
                chosen = ""
            elif suggestions_enabled:
                chosen = (item.get("suggested_category") or "").strip()
        chosen_account, chosen = resolve_submitted_category(chosen)
        if not chosen:
            no_category.append(item["vendor"])
            continue

        # The duplicate guard is not bypassed in bulk. Posting a second
        # duplicate copy is a decision, and it stays a per-item one.
        ext_id = item.get("external_transaction_id")
        if ext_id and db.get_wave_posted(ext_id):
            duplicates.append(item["vendor"])
            continue

        notes = (form.get(f"notes_{item_id}") or "").strip()[:500]
        evidence = (form.get(f"evidence_{item_id}") or "").strip()
        if evidence not in accountant_export.EVIDENCE_LABELS:
            evidence = None

        try:
            recorded_elsewhere = _post_one_approved(
                item, chosen, chosen_account, notes, evidence)
        except Exception as exc:  # one failure must not lose the rest
            failed.append(f'{item["vendor"]} ({exc})')
            continue
        if recorded_elsewhere:
            deferred.append(item["vendor"])
        posted += 1

    parts = [f"Recorded {posted} {_plural(posted, 'transaction')}."]
    if deferred:
        parts.append(f"{len(deferred)} were already covered by an invoice "
                     f"payment, a Shopify payout, or a transfer "
                     f"({', '.join(deferred[:3])}"
                     f"{'…' if len(deferred) > 3 else ''}).")
    if no_category:
        parts.append(f"{len(no_category)} still need a category "
                     f"({', '.join(no_category[:3])}"
                     f"{'…' if len(no_category) > 3 else ''}).")
    if duplicates:
        parts.append(f"{len(duplicates)} look like duplicates and were left "
                     f"for you to decide on.")
    if gone:
        parts.append(f"{gone} had already been handled.")
    if failed:
        parts.append(f"{len(failed)} failed: {'; '.join(failed[:2])}.")

    message = " ".join(parts)
    if posted == 0 and (no_category or duplicates or failed):
        return _redirect(err=message)
    return _redirect(msg=message)


@app.post("/deny/{item_id}")
def deny(item_id: int):
    item = db.get_item(item_id)
    if item is None or item["status"] != "pending":
        return _redirect(err="That item is no longer pending.")

    db.set_status(item_id, "denied")
    return _redirect(msg=f'Denied: {item["vendor"]} {signed_money(item)} dismissed.')


@app.post("/skip/{item_id}")
def skip(item_id: int):
    """Skip a bank transaction the duplicate guard flagged — mark it
    skipped without writing another ledger entry."""
    item = db.get_item(item_id)
    if item is None or item["status"] != "pending":
        return _redirect(err="That item is no longer pending.")

    db.set_status(item_id, "skipped")
    return _redirect(
        msg=f'Skipped: {item["vendor"]} {signed_money(item)} — not recorded '
            f"again (already in the posting audit)."
    )


@app.post("/dev/insert-test-item")
def insert_test_item(vendor: str = Form(None)):
    """Safe fixture: inserts one fake pending item so the confirm
    dialog, category dropdown, and vendor-memory flow can be tested
    without a bank call or a real ledger post. A custom vendor name
    is allowed but always forced to start with TEST_VENDOR_PREFIX so
    approve() can never mistake it for a real item. The suggestion
    runs through the same pipeline as real transactions
    (memory -> keywords -> fallback)."""
    db.init_db()
    vendor = (vendor or "").strip() or TEST_VENDOR_NAME
    if not vendor.startswith(TEST_VENDOR_PREFIX):
        vendor = f"{TEST_VENDOR_PREFIX} {vendor}"
    direction, category, confidence, category_source = (
        check_bank.categorize_pending(vendor, 0.01, "expense")
    )
    item_id = db.insert_pending_item(
        source="manual",
        vendor=vendor,
        amount=0.01,
        date=date.today().isoformat(),
        suggested_category=category,
        direction=direction,
        category_confidence=confidence if category else None,
        category_source=category_source if category else None,
    )
    return _redirect(msg=f"Test item inserted (id {item_id}).")


@app.post("/check")
def check():
    """Same fetch logic as the daily check, minus the email — you are
    already looking at the page the email would point you to."""
    try:
        new_items = check_bank.run_check()
    except RuntimeError as exc:
        return _redirect(err=str(exc))
    except Exception as exc:  # network errors etc.
        return _redirect(err=f"Check failed: {exc}")

    if new_items:
        return _redirect(msg=f"Found {len(new_items)} new transaction(s).")
    return _redirect(msg="No new transactions found.")


def _make_redirect(base):
    """POST-redirect-GET back to a page, carrying a banner.

    The separator is chosen rather than hardcoded because several bases
    now carry a tab (``/review?tab=followups``); appending ``?msg=`` to
    those produced a second question mark and a URL the router silently
    read as a tab literally named "followups?msg=Sent".
    """
    sep = "&" if "?" in base else "?"

    def redirect(msg=None, err=None):
        url = base
        if msg:
            url = f"{base}{sep}msg={quote(msg)}"
        elif err:
            url = f"{base}{sep}err={quote(err)}"
        return RedirectResponse(url, status_code=303)
    return redirect


# Approve / deny / check / insert-test-item all live on the Review
# page, so their round-trips land back on the right queue tab rather
# than the Home dashboard.
_redirect = _make_redirect("/review?tab=transactions")
_redirect_settings = _make_redirect("/settings")
_redirect_followups = _make_redirect("/review?tab=followups")
_redirect_payments = _make_redirect("/review?tab=payments")
# Reminder review is separate from the native invoice authoring workspace at
# /invoices. The explicit helper name keeps those destinations distinct.
_redirect_invoice_reminders = _make_redirect("/review?tab=invoices")
_redirect_connections = _make_redirect("/settings?tab=connections")
_redirect_automation = _make_redirect("/settings?tab=automation")
_redirect_nexus = _make_redirect("/tools/nexus")
_redirect_accountant_export = _make_redirect("/tools/accountant-export")
_redirect_ledger = _make_redirect("/ledger")
_redirect_ledger_accounts = _make_redirect("/ledger?tab=accounts")


def _redirect_inventory(msg=None, err=None, tab="stock"):
    return _make_redirect(f"/inventory?tab={tab}")(msg=msg, err=err)
_redirect_invoices_list = _make_redirect("/invoices")
_redirect_contacts = _make_redirect("/contacts")
_redirect_recurring = _make_redirect("/invoices?tab=recurring")
_redirect_review_recurring = _make_redirect("/review?tab=recurring")
_redirect_review_inventory = _make_redirect("/review?tab=inventory")


def _redirect_contact(contact_id):
    """Per-contact rather than a constant, since the base carries an id."""
    return _make_redirect(f"/contacts/{contact_id}")


def _redirect_invoice(invoice_id, msg=None, err=None):
    """Back to one invoice's own page, carrying a banner."""
    return _make_redirect(f"/invoices/{invoice_id}")(msg=msg, err=err)
