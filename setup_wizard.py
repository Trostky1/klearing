"""
Setup — Klearing's first-run flow.

Seven steps: app password, business identity, bank (required, via the
adapter registry), Klaviyo (optional), GA4 (optional/advanced, with
its own sub-wizard), email notifications, done.

Design notes, all learned from real setup pain:
- Every credential field has a Test Connection button wired to a JSON
  endpoint that validates FORMAT first (prefix, length, charset — no
  network) and only then makes a live API call, so a truncated paste
  fails in milliseconds with a message that says what's wrong.
- Tests auto-fire on paste, and format is re-checked as you type.
- Connection-level failures (DNS, refused, timeout) carry the
  ad-blocker/DNS-privacy hint; auth failures never do.
- GA4 is clearly optional, skippable, and its sub-wizard spells out
  Google Cloud checkpoints, warns Workspace users up front, and
  explains Property ID vs Measurement ID inline.
- The chosen step is persisted, so closing the browser mid-setup
  resumes where you left off.
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import app_lock
import banks
import business_registry
import check_bank
import db
import email_utils
import ga4_client
import klaviyo_client
import ledger
import secrets_store
import ui
from banks import CredentialField, simplefin
from config import APP_NAME, APP_URL

router = APIRouter()

SETUP_COMPLETE_KEY = "setup_complete"
SETUP_STEP_KEY = "setup_step"

# Sub-stage within the dedicated /setup/simplefin flow (separate from the
# numeric SETUP_STEP_KEY above, since this whole flow lives inside step 3
# and never advances the 7-step bar itself).
#
# An ORDERED list, not loose string constants: each screen requires "at
# least" a given stage, checked by list position via
# _require_simplefin_stage() below — never by hand-rolled equality. This is
# what makes the scheme safely additive: a later task appends a new stage
# name to the end of this list and calls
# _require_simplefin_stage("cutover"), full stop. It never has to re-audit
# every existing gate, because none of them hardcode which stage comes
# "after" which — they only compare list position.
#   "intro"   — default/unset. Screen 1 (cost + Sub-step A gate) not yet
#               confirmed; Screen 2 (the token paste) must redirect back
#               here if hit directly.
#   "token"   — Screen 1 confirmed ("my bank is connected in SimpleFIN").
#               Screen 2 may render its paste form. Token not yet
#               exchanged.
#   "mapping" — setup token exchanged for an Access URL, tested, and
#               stored. Screen 3 (account mapping, a later task) is next.
#               A visit to Screen 2 at this stage or later must NOT
#               re-offer the paste form — the token is already spent and
#               the Access URL already works, so re-pasting only burns a
#               fresh token for no reason. It shows an "already connected"
#               notice instead.
#   "cutover" — every SimpleFIN account has a mapping answer. Screen 4
#               (date + opening-balance collection, this task) is next.
#               A visit to Screen 3 at this stage or later is still the
#               legitimate "edit account mapping" re-entry point (see
#               that screen's own docstring) -- reaching "cutover" is not
#               a one-way door away from Screen 3.
#   "confirm", "done" — reserved for Task 6 (Screen 5: read-back/confirm,
#               plus the actual "finish" step that posts opening-balance
#               journal entries and activates SimpleFIN as the bank).
#               Append here, do not renumber or remove existing entries.
SIMPLEFIN_STAGE_KEY = "simplefin_setup_stage"
SIMPLEFIN_STAGES = ["intro", "token", "mapping", "cutover", "confirm", "done"]
SIMPLEFIN_STAGE_INTRO = "intro"
SIMPLEFIN_STAGE_TOKEN = "token"
SIMPLEFIN_STAGE_MAPPING = "mapping"
SIMPLEFIN_STAGE_CUTOVER = "cutover"
SIMPLEFIN_STAGE_CONFIRM = "confirm"
SIMPLEFIN_STAGE_DONE = "done"

REQUEST_BANK_URL = "https://github.com/Trostky1/klearing/issues/new?title=Bank+request:+"

STEPS = ["Password", "Business", "Bank", "Klaviyo", "GA4", "Email", "Done"]


def is_complete():
    db.init_db()
    return db.get_setting(SETUP_COMPLETE_KEY) == "1"


def current_step():
    db.init_db()
    try:
        step = int(db.get_setting(SETUP_STEP_KEY) or 1)
    except ValueError:
        step = 1
    return min(max(step, 1), len(STEPS))


def advance_to(step):
    if step > current_step():
        db.set_setting(SETUP_STEP_KEY, str(step))


def simplefin_stage():
    db.init_db()
    return db.get_setting(SIMPLEFIN_STAGE_KEY) or SIMPLEFIN_STAGE_INTRO


def _simplefin_stage_index(stage):
    """List position of a stage name, or 0 (== "intro") for anything
    unrecognized — a corrupt, hand-edited, or future-version stage string
    must fail closed (start the flow over), never fail open."""
    try:
        return SIMPLEFIN_STAGES.index(stage)
    except ValueError:
        return 0


def _simplefin_advance_stage(stage):
    """Move the stage forward only — mirrors advance_to() above, which
    already guards SETUP_STEP_KEY the same way. A stage can never be
    silently pushed backward by an out-of-order call."""
    if _simplefin_stage_index(stage) > _simplefin_stage_index(simplefin_stage()):
        db.set_setting(SIMPLEFIN_STAGE_KEY, stage)


def _require_simplefin_stage(minimum):
    """None if the flow has reached at least `minimum`; otherwise the
    303 RedirectResponse to the screen the user still owes. The one place
    stage ordering is compared — every route gate calls this instead of
    re-deriving the comparison."""
    if _simplefin_stage_index(simplefin_stage()) >= _simplefin_stage_index(minimum):
        return None
    return RedirectResponse(
        "/setup/simplefin?err=" +
        quote("Confirm your bank is connected in SimpleFIN first."),
        status_code=303)


# ---------------------------------------------------------------- rendering


def render_steps_bar(now):
    parts = ""
    for index, label in enumerate(STEPS, 1):
        cls = "done" if index < now else ("now" if index == now else "")
        current = ' aria-current="step"' if index == now else ""
        parts += f'<span class="step {cls}"{current}>{index}. {label}</span>'
    return f'<div class="steps">{parts}</div>'


def wizard_shell(step, content, message=None, error=None, extra_js=""):
    body = render_steps_bar(step) + content
    return ui.render_shell(
        "", "<span class='tag'>Setup</span>", body,
        message=message, error=error, show_nav=False, extra_js=extra_js,
    )


def paste_note():
    return ('<div class="paste-note">Paste it, don\'t retype it — one '
            "wrong character is enough to break a key.</div>")


def test_block(root_id, test_url, inner_fields_html, gate_id=None,
               button_label="Test Connection"):
    """One test-wired region: fields + Test button + status line +
    network-hint box. `gate_id` is a button to enable on live success."""
    gates = f' data-gates="{gate_id}"' if gate_id else ""
    return f"""
    <div id="{root_id}" data-test-url="{test_url}"{gates}>
      {inner_fields_html}
      <div class="test-row">
        <button class="check" type="button" data-test-button>{button_label}</button>
        <span class="test-status" data-test-status aria-live="polite"></span>
      </div>
      <div class="hint-box" data-test-hint style="display:none;"></div>
    </div>"""


def cred_input(name, label, hint, placeholder="", input_type="password",
               value=""):
    return f"""
    <div class="cred-field">
      <label class="q" for="in-{name}">{escape(label)}</label>
      <div class="hint">{hint}</div>
      <input type="{input_type}" id="in-{name}" name="{name}"
             placeholder="{escape(placeholder)}" value="{escape(value)}"
             autocomplete="off" spellcheck="false">
      {paste_note()}
    </div>"""


def env_select(name, field):
    """A sandbox/production picker for an adapter's "environment" field —
    a select rather than a text box so it can't be mistyped."""
    return f"""
    <div class="cred-field">
      <label class="q" for="in-{name}">{escape(field.label)}</label>
      <div class="hint">{escape(field.help_text)}</div>
      <select id="in-{name}" name="{name}">
        <option value="sandbox">sandbox — fake test banks (zero risk)</option>
        <option value="production">production — your real bank</option>
      </select>
    </div>"""


# ---------------------------------------------------------------- format specs

GMAIL_PASSWORD_FIELD = CredentialField(
    key="app_password",
    label="Gmail app password",
    strip_spaces=True,   # Google displays it as "abcd efgh ijkl mnop"
    min_length=16,
    max_length=16,
    format_error=(
        "A Gmail app password is exactly 16 letters (Google shows it in "
        "four groups of four — the spaces don't count). Copy the whole "
        "thing from myaccount.google.com/apppasswords."
    ),
)


def _format_json(field, value):
    """Format-check one field; JSON error response or None."""
    error = field.validate_format(value)
    if error:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": error, "hint": None})
    return None


def _format_ok_json():
    return JSONResponse({
        "ok": True, "stage": "format",
        "message": "Format looks right. Click Test Connection to check "
                   "it against the live service.",
        "hint": None,
    })


def _result_json(result, extra=None):
    payload = result.as_dict()
    payload["stage"] = "live"
    if extra:
        payload.update(extra)
    return JSONResponse(payload)


# ---------------------------------------------------------------- routes: flow


@router.get("/setup", response_class=HTMLResponse)
def setup_root():
    return RedirectResponse(f"/setup/step/{current_step()}", status_code=303)


@router.get("/setup/step/{step}", response_class=HTMLResponse)
def setup_step(step: int, msg: str = None, err: str = None):
    step = min(max(step, 1), len(STEPS))
    # Never let a link jump ahead of what's actually been finished.
    step = min(step, current_step())
    renderers = {
        1: render_step_password, 2: render_step_business, 3: render_step_bank,
        4: render_step_klaviyo, 5: render_step_ga4, 6: render_step_email,
        7: render_step_done,
    }
    return renderers[step](message=msg, error=err)


def _back_link(step):
    if step <= 1:
        return ""
    return f'<a class="skip-link" href="/setup/step/{step - 1}">&larr; Back</a>'


# ---------------------------------------------------------------- step 1


def render_step_password(message=None, error=None):
    already = app_lock.is_configured()
    intro = (
        "This password locks Klearing's pages so someone else using "
        "this computer can't browse your finances or approve "
        "transactions — that's its only job, so your daily automation "
        "never needs it."
    )
    if already:
        form = """
        <div class="callout info">A password is already set. Continue, or
          set a new one below (this replaces the old one).</div>
        <div class="cred-field">
          <label class="q" for="pw0">Current password</label>
          <input type="password" id="pw0" name="current_password"
                 autocomplete="current-password" style="max-width:320px;"
                 form="password-form">
        </div>"""
    else:
        form = ""
    content = f"""
    <div class="sub">Welcome to {APP_NAME} — local books for
      your business finances. Bank transactions wait here until you
      approve them into your native ledger; nothing is ever posted without a click.
      Setup takes about ten minutes.</div>
    <div class="section">
      <div class="section-head"><h2>Step 1 — Set your app password</h2></div>
      <div class="hint" style="margin-bottom:14px;">{intro}</div>
      {form}
      <form method="post" action="/setup/password" id="password-form">
        <div class="cred-field">
          <label class="q" for="pw">App password</label>
          <div class="hint">At least 8 characters. API keys are protected
            separately by your operating system's credential store, so
            changing this later never loses data.</div>
          <input type="password" id="pw" name="password" minlength="8"
                 required autocomplete="new-password" style="max-width:320px;">
        </div>
        <div class="cred-field">
          <label class="q" for="pw2">Confirm password</label>
          <input type="password" id="pw2" name="confirm_password" minlength="8"
                 required autocomplete="new-password" style="max-width:320px;">
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">
            {"Update password and continue" if already else "Create password and continue"}</button>
          {'<a class="skip-link" href="/setup/step/2">Keep current password &rarr;</a>' if already else ""}
        </div>
      </form>
    </div>
    <div class="paste-note" style="margin-top:14px;">Moving from another
      computer? <a href="/setup/restore">Restore a {APP_NAME} backup</a>
      instead of setting up from scratch.</div>"""
    return wizard_shell(1, content, message=message, error=error)


@router.post("/setup/password")
def setup_password(request: Request, password: str = Form(""),
                   confirm_password: str = Form(""),
                   current_password: str = Form("")):
    try:
        if app_lock.is_configured():
            # Replacing an existing password requires knowing it — even
            # with a valid session (a walk-up at an unlocked browser
            # shouldn't be able to lock the real user out).
            if not app_lock.verify(current_password):
                raise app_lock.LockError(
                    "Enter your current password to set a new one."
                )
        app_lock.set_password(password, confirm_password)
    except app_lock.LockError as exc:
        return HTMLResponse(render_step_password(error=str(exc)))

    # Bring the credential store up now so a broken keyring surfaces
    # here, not at 8am tomorrow.
    keyring_note = None
    if not secrets_store.is_available():
        keyring_note = (
            "Heads up: no OS credential store was found, so the "
            "encryption key is kept in a protected file instead."
        )

    advance_to(2)
    response = RedirectResponse(
        "/setup/step/2" + (f"?msg={keyring_note}" if keyring_note else ""),
        status_code=303,
    )
    token = app_lock.issue_session()
    response.set_cookie(app_lock.SESSION_COOKIE, token, httponly=True,
                        samesite="lax")
    return response


# ---------------------------------------------------------------- restore


def render_restore_page(message=None, error=None):
    content = f"""
    <div class="section">
      <div class="section-head"><h2>Restore from a backup</h2></div>
      <div class="hint" style="margin-bottom:14px;">Pick the
        <span class="mono">.klearingbackup</span> file you exported from
        Settings on your old computer, and enter the backup password you
        chose when exporting (not necessarily your app password).
        Everything comes back: connections, history, drafts, and your
        app password.</div>
      <form method="post" action="/setup/restore" enctype="multipart/form-data">
        <div class="cred-field">
          <label class="q" for="bk-file">Backup file</label>
          <input type="file" id="bk-file" name="backup_file"
                 accept=".klearingbackup" required>
        </div>
        <div class="cred-field" style="max-width:320px;">
          <label class="q" for="bk-pw">Backup password</label>
          <input type="password" id="bk-pw" name="backup_password" required
                 autocomplete="off">
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Restore</button>
          <a class="skip-link" href="/setup/step/1">&larr; Back to Setup</a>
        </div>
      </form>
    </div>"""
    return wizard_shell(1, content, message=message, error=error)


@router.get("/setup/restore", response_class=HTMLResponse)
def setup_restore_page(msg: str = None, err: str = None):
    if is_complete():
        return RedirectResponse("/", status_code=303)
    return render_restore_page(message=msg, error=err)


@router.post("/setup/restore")
async def setup_restore(backup_file: UploadFile = File(None),
                        backup_password: str = Form("")):
    import backup
    if is_complete():
        # Never clobber a finished install from this route.
        return RedirectResponse("/", status_code=303)
    file_bytes = await backup_file.read() if backup_file is not None else b""
    if not file_bytes:
        return HTMLResponse(render_restore_page(
            error="Choose the backup file first."))
    try:
        created_at = backup.restore_backup(file_bytes, backup_password)
    except Exception as exc:
        return HTMLResponse(render_restore_page(error=str(exc)))
    when = (created_at or "")[:16].replace("T", " ")
    return RedirectResponse(
        f"/unlock?msg={quote(f'Backup from {when} restored. Unlock with the app password you used back then.')}",
        status_code=303,
    )


# ------------------------------------------------------------- step 2: business


def render_step_business(message=None, error=None):
    active = business_registry.active_business()
    current_name = active["name"] if active else (
        db.get_setting("business_name") or "")
    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 2 — Name your business</h2></div>
      <div class="hint mb-4">This creates a local bookkeeping scope. It is
        stored only on this computer and can be renamed later.</div>
      <form method="post" action="/setup/save/business">
        <div class="cred-field">
          <label class="q" for="business-name">Business name</label>
          <input type="text" id="business-name" name="business_name"
                 value="{escape(current_name)}" maxlength="120" required>
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Save and continue</button>
          {_back_link(2)}
        </div>
      </form>
    </div>"""
    return wizard_shell(2, content, message=message, error=error)


@router.post("/setup/save/business")
def setup_save_business(business_name: str = Form("")):
    clean_name = (business_name or "").strip()
    if not clean_name:
        return HTMLResponse(render_step_business(error="Enter a business name."))
    active = business_registry.active_business()
    business_registry.upsert_business(
        clean_name, business_id=active["id"] if active else None,
        make_active=True)
    db.set_setting("business_name", clean_name)
    advance_to(3)
    return RedirectResponse("/setup/step/3", status_code=303)


# ---------------------------------------------------------------- step 3: bank


def render_step_bank(message=None, error=None):
    adapters = banks.all_adapters()
    active = banks.active_adapter()

    status_note = ""
    if active and active.is_configured():
        status_note = f"""
        <div class="callout info"><strong>Already connected</strong>
          {escape(active.display_name)} is connected. Continue, or
          reconnect below.</div>"""

    options = ""
    fieldsets = ""
    js_wires = ""
    for adapter in adapters:
        selected = " selected" if (active and adapter.id == active.id) or \
            (not active and adapter is adapters[0]) else ""
        options += f'<option value="{adapter.id}"{selected}>{escape(adapter.display_name)}</option>'

        if adapter.id == "simplefin":
            # SimpleFIN has no typeable credential field: its only stored
            # value (the Access URL) only exists after a server-side setup
            # token exchange, so a generic paste box here would show users a
            # field they have no way to fill in by hand. Its generic
            # token_help/token_url also lead straight to SimpleFIN's
            # token-creation page — exactly the wrong thing to show first,
            # since the user hasn't gone through SimpleFIN's own bank-connect
            # step yet. Skip the shared explainer entirely and point at the
            # dedicated setup flow instead, where the token step belongs.
            body = """
        <div class="hint" style="margin-bottom:10px;">
          SimpleFIN setup happens on its own screen: cost, the two-step
          bank connection, and the setup token paste all live there.
        </div>
        <a class="save-btn" href="/setup/simplefin">Set up SimpleFIN</a>"""
        else:
            inner = f"""
        <div class="hint" style="margin-bottom:10px;">
          {escape(adapter.token_help)}
          <a href="{adapter.token_url}" target="_blank" rel="noopener">Open {escape(adapter.display_name)}</a>
        </div>"""
            for field in adapter.credential_fields():
                field_name = f"{adapter.id}__{field.key}"
                if field.key == "environment":
                    inner += env_select(field_name, field)
                else:
                    inner += cred_input(
                        field_name, field.label,
                        escape(field.help_text), placeholder=field.placeholder,
                    )
            body = test_block(f"bank-test-{adapter.id}",
                              f"/setup/test/bank/{adapter.id}", inner,
                              gate_id="bank-continue")
            js_wires += f"wireTest('bank-test-{adapter.id}');\n"

        fieldsets += f"""
        <div class="bank-fields" id="bank-fields-{adapter.id}" style="display:none;">
          {body}
        </div>"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 3 — Connect your bank</h2>
        <span class="tag">required</span></div>
      <div class="hint" style="margin-bottom:14px;">{APP_NAME} reads new
        transactions from your business bank account and queues them for
        your approval. Read-only access is all it ever needs — it cannot
        move money.</div>
      {status_note}
      <form method="post" action="/setup/save/bank">
        <div class="cred-field">
          <label class="q" for="bank-select">Bank</label>
          <select id="bank-select" name="adapter_id">{options}</select>
          <div class="paste-note">Don't see yours?
            <a href="{REQUEST_BANK_URL}" target="_blank" rel="noopener">Request
            another bank</a> — the bank connection is a plugin, so new ones
            can be added without touching the rest of the app.</div>
        </div>
        {fieldsets}
        <div class="wizard-actions">
          <button class="save-btn" type="submit" id="bank-continue"
                  {"" if active and active.is_configured() else "disabled"}>Continue</button>
          {'<a class="skip-link" href="/setup/step/4">Keep current connection &rarr;</a>' if active and active.is_configured() else ""}
          {_back_link(3)}
        </div>
      </form>
    </div>"""

    extra_js = js_wires + """
    const bankSelect = document.getElementById('bank-select');
    const continueBtn = document.getElementById('bank-continue');
    function showBankFields() {
      document.querySelectorAll('.bank-fields').forEach(el => el.style.display = 'none');
      const chosen = document.getElementById('bank-fields-' + bankSelect.value);
      if (chosen) chosen.style.display = '';
      // SimpleFIN has nothing to Continue with from this page — its own
      // link IS the continue action, so hide the button rather than leave
      // it permanently disabled next to a live "Set up SimpleFIN" link.
      continueBtn.style.display = bankSelect.value === 'simplefin' ? 'none' : '';
    }
    bankSelect.addEventListener('change', () => {
      continueBtn.disabled = true;
      showBankFields();
    });
    showBankFields();
    """
    return wizard_shell(3, content, message=message, error=error,
                        extra_js=extra_js)


def _bank_form_values(adapter, form):
    values = {}
    for field in adapter.credential_fields():
        raw = form.get(f"{adapter.id}__{field.key}", "")
        values[field.key] = field.clean(raw)
    return values


@router.post("/setup/test/bank/{adapter_id}")
async def setup_test_bank(adapter_id: str, request: Request):
    try:
        adapter = banks.get_adapter(adapter_id)
    except KeyError:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": "Unknown bank.", "hint": None})
    form = await request.form()
    values = {}
    for field in adapter.credential_fields():
        raw = form.get(f"{adapter.id}__{field.key}", "")
        error = field.validate_format(raw)
        if error:
            return JSONResponse({"ok": False, "stage": "format",
                                 "message": error, "hint": None})
        values[field.key] = field.clean(raw)
    if form.get("live", "1") != "1":
        return _format_ok_json()
    result = adapter.test_connection(values)
    return _result_json(result)


@router.post("/setup/save/bank")
async def setup_save_bank(request: Request):
    form = await request.form()
    adapter_id = form.get("adapter_id", "")

    # SimpleFIN has no credential fields for this form to validate or
    # store — its whole setup (token exchange, account mapping, cutover
    # date) lives in its own dedicated flow. Unlike Plaid's "still needs a
    # Link connect step" branch below, this fires unconditionally: there is
    # no valid form submission to process for SimpleFIN at step 3, whether
    # or not it's already configured.
    if adapter_id == "simplefin":
        return RedirectResponse("/setup/simplefin", status_code=303)

    try:
        adapter = banks.get_adapter(adapter_id)
    except KeyError:
        return HTMLResponse(render_step_bank(error="Unknown bank."))

    # Keeping an existing connection: empty fields + already configured.
    values = _bank_form_values(adapter, form)
    if not any(values.values()) and adapter.is_configured():
        db.set_setting(banks.ACTIVE_BANK_KEY, adapter.id)
        advance_to(4)
        return RedirectResponse("/setup/step/4", status_code=303)

    for field in adapter.credential_fields():
        error = field.validate_format(form.get(f"{adapter.id}__{field.key}", ""))
        if error:
            return HTMLResponse(render_step_bank(error=error))

    result = adapter.test_connection(values)
    if not result.ok:
        return HTMLResponse(render_step_bank(error=result.message))

    for field in adapter.credential_fields():
        secrets_store.set_secret(adapter.secret_name(field.key),
                                 values[field.key])
    previous_bank = db.get_setting(banks.ACTIVE_BANK_KEY)
    db.set_setting(banks.ACTIVE_BANK_KEY, adapter.id)
    if previous_bank and previous_bank != adapter.id:
        # Account ids from the old bank mean nothing at the new one —
        # a stale selection would sum to $0 and cry wolf every morning.
        db.set_setting("cash_accounts", "[]")

    # Plaid is two-phase: pasting the keys only proves they work. The user
    # still has to connect a bank through Plaid Link to get an access
    # token, so send them to the connect step rather than straight on.
    # Every other adapter is fully configured once its token is stored.
    if adapter.id == "plaid" and not adapter.is_configured():
        return RedirectResponse("/setup/plaid", status_code=303)

    advance_to(4)
    return RedirectResponse("/setup/step/4", status_code=303)


# ---------------------------------------------- step 3b: Plaid Link (connect)


def _plaid_adapter_and_credentials():
    """The Plaid adapter plus its stored keys, or (adapter, None) when the
    keys aren't saved yet."""
    adapter = banks.get_adapter("plaid")
    return adapter, adapter.stored_credentials()


def render_plaid_connect(message=None, error=None):
    adapter, credentials = _plaid_adapter_and_credentials()
    if credentials is None:
        return RedirectResponse(
            "/setup/step/3?err=Paste your Plaid keys first.", status_code=303)

    environment = credentials.get("environment", "sandbox")
    connected = len(credentials.get("access_tokens") or [])
    connected_note = (
        f'<div class="callout info"><strong>{connected} bank(s) '
        f'connected.</strong> Connect another, or continue.</div>'
        if connected else
        '<div class="hint" style="margin-bottom:12px;">No bank connected '
        'yet. Open Plaid Link below and sign in to your bank through '
        "Plaid's secure flow — Klearing never sees your bank password.</div>"
    )

    sandbox_button = ""
    if environment == "sandbox":
        sandbox_button = """
        <form method="post" action="/setup/plaid/connect-sandbox"
              style="margin-top:12px;">
          <button class="check" type="submit">Connect a Sandbox test bank
            (no login)</button>
        </form>
        <div class="paste-note">Sandbox only: connects a fake test bank so
          you can see the whole flow with zero risk.</div>"""

    continue_action = ""
    if connected:
        continue_action = """
        <form method="post" action="/setup/plaid/continue">
          <button class="save-btn" type="submit">Continue &rarr;</button>
        </form>"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 3 — Connect your bank (Plaid)</h2>
        <span class="tag">required</span></div>
      <div class="hint" style="margin-bottom:14px;">Environment:
        <strong>{escape(environment)}</strong>. Klearing opens Plaid Link in
        this window; you sign in to your bank through Plaid directly.
        Read-only access only.</div>
      {connected_note}
      <div class="wizard-actions" style="margin-top:4px;">
        <button class="save-btn" type="button" id="plaid-connect-btn">Connect
          your bank with Plaid</button>
      </div>
      <div class="test-status" id="plaid-link-status" aria-live="polite"
           style="margin-top:10px;"></div>
      {sandbox_button}
      <div style="margin-top:22px;">{continue_action}</div>
      <div class="wizard-actions">{_back_link(3)}</div>
    </div>
    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>"""

    extra_js = """
    async function plaidConnect() {
      const status = document.getElementById('plaid-link-status');
      status.className = 'test-status busy';
      status.textContent = 'Starting Plaid Link…';
      let linkToken;
      try {
        const resp = await fetch('/setup/plaid/link-token', {method: 'POST'});
        const data = await resp.json();
        if (!data.link_token) {
          status.className = 'test-status err';
          status.textContent = data.error || 'Could not start Plaid Link.';
          return;
        }
        linkToken = data.link_token;
      } catch (e) {
        status.className = 'test-status err';
        status.textContent = 'Could not reach Plaid to start Link.';
        return;
      }
      const handler = Plaid.create({
        token: linkToken,
        onSuccess: async (publicToken) => {
          status.className = 'test-status busy';
          status.textContent = 'Connecting your bank…';
          const resp = await fetch('/setup/plaid/exchange', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({public_token: publicToken}),
          });
          const data = await resp.json();
          if (data.ok) {
            window.location = '/setup/plaid?msg=' +
              encodeURIComponent('Bank connected.');
          } else {
            status.className = 'test-status err';
            status.textContent = data.error || 'Could not finish connecting.';
          }
        },
        onExit: (err) => {
          if (err) {
            status.className = 'test-status err';
            status.textContent = 'Plaid Link closed: ' +
              (err.display_message || err.error_message || 'no bank connected');
          } else {
            status.className = 'test-status';
            status.textContent = '';
          }
        },
      });
      handler.open();
    }
    const btn = document.getElementById('plaid-connect-btn');
    if (btn) btn.addEventListener('click', plaidConnect);
    """
    return wizard_shell(3, content, message=message, error=error,
                        extra_js=extra_js)


@router.get("/setup/plaid", response_class=HTMLResponse)
def setup_plaid_connect(msg: str = None, err: str = None):
    return render_plaid_connect(message=msg, error=err)


PLAID_OAUTH_REDIRECT_URI = f"{APP_URL}/plaid/oauth-return"


@router.post("/setup/plaid/link-token")
def setup_plaid_link_token():
    adapter, credentials = _plaid_adapter_and_credentials()
    if credentials is None:
        return JSONResponse({"error": "Paste your Plaid keys first."})
    try:
        link_token = adapter.create_link_token(
            credentials, redirect_uri=PLAID_OAUTH_REDIRECT_URI)
    except Exception as exc:
        return JSONResponse({"error": f"Plaid: {exc}"})
    return JSONResponse({"link_token": link_token,
                         "redirect_uri": PLAID_OAUTH_REDIRECT_URI})


@router.post("/setup/plaid/exchange")
async def setup_plaid_exchange(request: Request):
    adapter, credentials = _plaid_adapter_and_credentials()
    if credentials is None:
        return JSONResponse({"ok": False, "error": "Plaid keys are missing."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    public_token = (body or {}).get("public_token")
    if not public_token:
        return JSONResponse({"ok": False, "error": "No public token from Link."})
    try:
        adapter.connect_via_public_token(credentials, public_token)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Plaid: {exc}"})
    return JSONResponse({"ok": True})


@router.post("/setup/plaid/connect-sandbox")
def setup_plaid_connect_sandbox():
    adapter, credentials = _plaid_adapter_and_credentials()
    if credentials is None:
        return RedirectResponse("/setup/step/3", status_code=303)
    if credentials.get("environment") != "sandbox":
        return RedirectResponse(
            "/setup/plaid?err=Sandbox connect is only for the sandbox "
            "environment.", status_code=303)
    try:
        public_token = adapter.create_sandbox_public_token(credentials)
        adapter.connect_via_public_token(credentials, public_token)
    except Exception as exc:
        return RedirectResponse(
            f"/setup/plaid?err={quote(f'Plaid sandbox connect failed: {exc}')}",
            status_code=303)
    return RedirectResponse(
        "/setup/plaid?msg=Sandbox+test+bank+connected.", status_code=303)


@router.post("/setup/plaid/continue")
def setup_plaid_continue():
    adapter = banks.get_adapter("plaid")
    if not adapter.is_configured():
        return RedirectResponse(
            "/setup/plaid?err=Connect a bank before continuing.",
            status_code=303)
    advance_to(4)
    return RedirectResponse("/setup/step/4", status_code=303)


@router.get("/plaid/oauth-return", response_class=HTMLResponse)
def plaid_oauth_return(oauth_state_id: str = None, oauth_token: str = None):
    """OAuth redirect landing page.

    When a bank uses OAuth (e.g. Veridian Credit Union), Plaid sends the
    user to their bank's website and then back here. The page re-initializes
    Plaid Link with receivedRedirectUri so Link can complete the handshake
    and fire onSuccess just as if the flow were non-OAuth.

    The full URL including the query string must match what was passed as
    redirect_uri when the link_token was created, and what is registered in
    the Plaid Dashboard — only the origin+path is compared, query params are
    passed through by Plaid.
    """
    if not oauth_state_id:
        return RedirectResponse("/setup/plaid?err=Missing oauth_state_id — "
                                "open Plaid Link from the setup page.",
                                status_code=303)

    content = """
    <div class="section">
      <div class="section-head"><h2>Finishing bank connection…</h2></div>
      <div class="hint" style="margin-bottom:14px;">
        Plaid Link is completing your OAuth sign-in. This should only take
        a moment.</div>
      <div class="test-status busy" id="oauth-status" aria-live="polite">Resuming Plaid Link…</div>
    </div>
    <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>"""

    extra_js = f"""
    (async function () {{
      const status = document.getElementById('oauth-status');
      // Step 1: get a fresh link_token (same endpoint as normal connect).
      let linkToken, redirectUri;
      try {{
        const resp = await fetch('/setup/plaid/link-token', {{method: 'POST'}});
        const data = await resp.json();
        if (!data.link_token) {{
          status.className = 'test-status err';
          status.textContent = data.error || 'Could not resume Plaid Link.';
          return;
        }}
        linkToken = data.link_token;
        redirectUri = data.redirect_uri;
      }} catch (e) {{
        status.className = 'test-status err';
        status.textContent = 'Could not reach Plaid to resume the connection.';
        return;
      }}
      // Step 2: re-initialize Link with receivedRedirectUri so it knows
      // we are returning from an OAuth flow (not starting fresh).
      const handler = Plaid.create({{
        token: linkToken,
        receivedRedirectUri: window.location.href,
        onSuccess: async (publicToken) => {{
          status.className = 'test-status busy';
          status.textContent = 'Connecting your bank…';
          const resp = await fetch('/setup/plaid/exchange', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{public_token: publicToken}}),
          }});
          const data = await resp.json();
          if (data.ok) {{
            window.location = '/setup/plaid?msg=' +
              encodeURIComponent('Bank connected via OAuth.');
          }} else {{
            status.className = 'test-status err';
            status.textContent = data.error || 'Could not finish connecting.';
          }}
        }},
        onExit: (err) => {{
          if (err) {{
            status.className = 'test-status err';
            status.textContent = 'Plaid Link closed: ' +
              (err.display_message || err.error_message || 'no bank connected');
          }} else {{
            window.location = '/setup/plaid';
          }}
        }},
      }});
      handler.open();
    }})();
    """
    return wizard_shell(3, content, extra_js=extra_js)


# ------------------------------------------- step 3c: SimpleFIN dedicated flow
#
# SimpleFIN's real connection is two separate user actions on SimpleFIN's own
# site, done at different times:
#   Sub-step A — sign up at SimpleFIN Bridge and connect a bank there.
#                Nothing is pasted into Klearing during this.
#   Sub-step B — inside SimpleFIN Bridge, create a "New app connection",
#                which shows a one-use setup token. THAT is what gets pasted
#                into Klearing.
#
# Screen 1 below (GET /setup/simplefin) covers the cost disclosure and gates
# the user through Sub-step A. Screen 2 (GET /setup/simplefin/token) covers
# Sub-step B and the paste. The token-creation link (adapter.token_url) must
# only ever appear on Screen 2 — showing it any earlier sends users looking
# for a token that doesn't exist yet (see banks/simplefin.py's module
# docstring and the step-3 branch in render_step_bank above).


def render_simplefin_intro(message=None, error=None):
    content = f"""
    <div class="section">
      <div class="section-head"><h2>Connect your bank with SimpleFIN Bridge</h2>
        <span class="tag">required</span></div>
      <div class="hint" style="margin-bottom:14px;">{APP_NAME} reads your
        bank transactions through SimpleFIN Bridge, a read-only connection
        service. It cannot move money — it can only report what already
        happened.</div>
      <div class="callout info"><strong>SimpleFIN costs $15/year.</strong>
        SimpleFIN Bridge bills you directly for this, separately from
        {APP_NAME}. {APP_NAME} never charges you for it and never sees your
        payment details.</div>
      <div class="hint" style="margin:14px 0;">This takes two steps on
        SimpleFIN's site, then one paste back here.</div>
      <div class="hint" style="margin-bottom:6px;"><strong>Step 1 of 2 (on
        SimpleFIN's site):</strong> sign up at SimpleFIN Bridge and connect
        your bank there.</div>
      <div class="wizard-actions" style="margin-top:4px;">
        <a class="save-btn" href="{simplefin.BRIDGE_URL}" target="_blank"
           rel="noopener">Open SimpleFIN Bridge</a>
      </div>
      <div class="paste-note" style="margin-bottom:18px;">{escape(simplefin.BRIDGE_HELP)}</div>
      <form method="post" action="/setup/simplefin/bank-connected">
        <div class="wizard-actions">
          <button class="save-btn" type="submit">My bank is connected in
            SimpleFIN — continue</button>
        </div>
      </form>
      <div class="wizard-actions">
        <a class="skip-link" href="/setup/step/3">&larr; Back</a>
      </div>
    </div>"""
    return wizard_shell(3, content, message=message, error=error)


@router.get("/setup/simplefin", response_class=HTMLResponse)
def setup_simplefin_intro(msg: str = None, err: str = None):
    return render_simplefin_intro(message=msg, error=err)


@router.post("/setup/simplefin/bank-connected")
def setup_simplefin_bank_connected():
    # No live network call here — this is just the user's own checkpoint
    # that they finished Sub-step A on SimpleFIN's site. There is nothing
    # in SimpleFIN's API to check that against until a setup token exists.
    _simplefin_advance_stage(SIMPLEFIN_STAGE_TOKEN)
    return RedirectResponse("/setup/simplefin/token", status_code=303)


def render_simplefin_token(message=None, error=None, reconnect=False):
    """``reconnect=True`` renders the exact same paste form, plus a
    hidden ``reconnect=1`` field the POST handler below checks for --
    the ONLY way a submission from THIS RENDERED FORM can bypass the
    already-connected short-circuit there. It is set ONLY by the GET
    handler's own explicit ``?reconnect=1`` branch (itself only reached
    via the Connections page's deliberate "Reconnect with a new token"
    link), never by default -- so a plain revisit of this screen, or a
    resubmission of a form rendered before reconnecting was requested,
    still carries no such field and still can't bypass anything.

    Be honest with yourself about what this is: a UX safety rail
    against an ACCIDENTAL resubmission (browser back button, a stale
    bookmark, double-click), not a security boundary. The field is a
    plain, forgeable form value, not a signed token -- anyone who can
    already reach this app and deliberately crafts a POST with
    reconnect=1 set could bypass this regardless. That is fine, because
    the thing being protected against is an honest mistake burning a
    one-use token, not an adversary; do not read the wording above as a
    stronger guarantee than that."""
    adapter = banks.get_adapter("simplefin")
    reconnect_field = (
        '<input type="hidden" name="reconnect" value="1">' if reconnect else "")
    reconnect_note = ("""
      <div class="callout info">Reconnecting replaces your stored Access
        URL only. Your account mapping, cutover date, and opening
        balances are kept as-is.</div>""" if reconnect else "")
    back_href = "/settings?tab=connections" if reconnect else "/setup/simplefin"
    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 2 of 2 (on SimpleFIN's site) —
        create a setup token</h2>
        <span class="tag">required</span></div>
      {reconnect_note}
      <div class="hint" style="margin-bottom:14px;">Inside SimpleFIN Bridge,
        open <strong>New app connection</strong>. SimpleFIN shows you a
        one-use setup token there — copy the whole thing.</div>
      <div class="wizard-actions" style="margin-top:4px;">
        <a class="save-btn" href="{adapter.token_url}" target="_blank"
           rel="noopener">Open SimpleFIN Bridge to create a token</a>
      </div>
      <div class="paste-note" style="margin-bottom:14px;">{escape(adapter.token_help)}</div>
      <div class="hint" style="margin-bottom:14px;">The token works once.
        {APP_NAME} trades it for a permanent key right away, and then the
        token is spent — if you need to start over, make a fresh one in
        SimpleFIN.</div>
      <form method="post" action="/setup/simplefin/token">
        {reconnect_field}
        <div class="cred-field">
          <label class="q" for="simplefin-token">Setup token</label>
          <div class="hint">Paste the whole token SimpleFIN just showed
            you.</div>
          <textarea id="simplefin-token" name="setup_token" rows="4"
                    autocomplete="off" spellcheck="false" required></textarea>
          {paste_note()}
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Connect SimpleFIN</button>
          <a class="skip-link" href="{back_href}">&larr; Back</a>
        </div>
      </form>
    </div>"""
    return wizard_shell(3, content, message=message, error=error)


def render_simplefin_already_connected(message=None, error=None):
    """Shown instead of the paste form once the stage has already reached
    "mapping" or later — the token is spent and the Access URL already
    works, so re-offering the paste box would only invite burning a fresh
    token for no reason."""
    adapter = banks.get_adapter("simplefin")
    account_note = "Already connected."
    credentials = adapter.stored_credentials()
    if credentials:
        try:
            count = len(adapter.list_accounts(credentials))
            account_note = f"Already connected — {count} account(s) found."
        except Exception:
            # Same "don't crash the screen over a transient live check"
            # posture as the rest of this flow; the stored connection is
            # not in question here, only the wording of this notice.
            pass
    content = f"""
    <div class="section">
      <div class="section-head"><h2>SimpleFIN is connected</h2></div>
      <div class="callout info"><strong>{escape(account_note)}</strong>
        Your setup token has already been used and the connection works —
        continue to account mapping rather than pasting another one.</div>
      <div class="wizard-actions">
        <a class="save-btn" href="/setup/simplefin/accounts">Continue to
          account mapping</a>
      </div>
      <div class="wizard-actions">
        <a class="skip-link" href="/setup/step/3">&larr; Back to bank setup</a>
      </div>
    </div>"""
    return wizard_shell(3, content, message=message, error=error)


@router.get("/setup/simplefin/token", response_class=HTMLResponse)
def setup_simplefin_token_screen(msg: str = None, err: str = None,
                                  reconnect: str = None):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_TOKEN)
    if redirect is not None:
        return redirect
    if reconnect == "1":
        # An explicit, deliberate re-entry -- the Connections page's own
        # "Reconnect with a new token" link is the only place this query
        # param is ever set. It bypasses the already-connected notice
        # below ON PURPOSE, because this visit genuinely does want a
        # fresh paste form (e.g. the stored Access URL stopped working
        # and SimpleFIN Bridge needs a new one-use token). A bare or
        # bookmarked visit to this same URL WITHOUT the query param
        # falls straight through to the notice below, exactly as
        # before -- so an accidental revisit still can't reach the
        # paste form and burn a token by surprise.
        return render_simplefin_token(message=msg, error=err, reconnect=True)
    if _simplefin_stage_index(simplefin_stage()) >= \
            _simplefin_stage_index(SIMPLEFIN_STAGE_MAPPING):
        return render_simplefin_already_connected(message=msg, error=err)
    return render_simplefin_token(message=msg, error=err)


@router.post("/setup/simplefin/token")
async def setup_simplefin_token(request: Request):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_TOKEN)
    if redirect is not None:
        return redirect

    form = await request.form()
    # Set ONLY by render_simplefin_token's reconnect=True branch above,
    # which itself is only reached via the GET handler's explicit
    # ?reconnect=1 check -- never present on a form rendered any other
    # way. This is what lets a genuine reconnect's POST actually
    # process, without loosening the guard below for anyone who did NOT
    # come through that deliberate path.
    is_reconnect = str(form.get("reconnect") or "") == "1"

    if not is_reconnect and _simplefin_stage_index(simplefin_stage()) >= \
            _simplefin_stage_index(SIMPLEFIN_STAGE_MAPPING):
        # Already connected, and this submission did not carry the
        # reconnect field above — resubmitting here would only burn a
        # second, unnecessary setup token. Send them to the notice above
        # instead of processing another exchange.
        return RedirectResponse("/setup/simplefin/token", status_code=303)

    # This is the raw, one-use setup token — never a CredentialField, never
    # passed through adapter.credential_fields()/secrets_store, and never
    # allowed into a log line or an error banner. It must not survive past
    # this function.
    token = str(form.get("setup_token") or "").strip()
    if not token:
        return HTMLResponse(render_simplefin_token(
            error="Paste your SimpleFIN setup token.", reconnect=is_reconnect))

    adapter = banks.get_adapter("simplefin")
    try:
        access_url = simplefin.exchange_setup_token(token)
    except simplefin.SimpleFINError as exc:
        # str(exc) here is always one of the adapter's own curated,
        # user-actionable messages (empty / not base64 / HTTP 403 / bad
        # Access URL) — never the raw token or a claim URL.
        return HTMLResponse(render_simplefin_token(
            error=str(exc), reconnect=is_reconnect))

    # An Access URL that exchanges successfully but can't actually list
    # accounts is not a working connection — verify before storing anything.
    result = adapter.test_connection({"access_url": access_url})
    if not result.ok:
        return HTMLResponse(render_simplefin_token(
            error=result.message, reconnect=is_reconnect))

    secrets_store.set_secret(adapter.secret_name("access_url"), access_url)
    # Deliberately NOT done here (later tasks own this):
    #   - db.set_setting(banks.ACTIVE_BANK_KEY, "simplefin") — this
    #     connection isn't "the active bank" until mapping, cutover, and
    #     opening balances finish.
    #   - any change to the "cash_accounts" setting.
    # On a genuine reconnect this is a no-op — _simplefin_advance_stage
    # only ever moves forward (see its own docstring), and the stage is
    # already at "mapping" or later by construction of the is_reconnect
    # branch above. Account mapping, cutover, and opening balances are
    # untouched; only the stored Access URL just changed.
    _simplefin_advance_stage(SIMPLEFIN_STAGE_MAPPING)

    # result.message already reports "Connected. SimpleFIN returned N USD
    # account(s)." (connectivity.ok(), driven by the same list_accounts()
    # call test_connection() just made) — reuse it instead of a second
    # network round trip for the same count. Screen 3 (account mapping,
    # below) is where the user lands next, whether this was a first
    # connection or a reconnect — it is the correct "continue from here"
    # destination either way, and already knows how to render a
    # previously-mapped account's real prior answer on a revisit.
    return RedirectResponse(
        "/setup/simplefin/accounts?msg=" + quote(result.message),
        status_code=303)


# ------------------------------------------- step 3d: SimpleFIN account mapping
#
# The one and only place a SimpleFIN account ever gets a Ledger mapping.
# ensure_bank_ledger_account() (db.py) refuses to guess for any non-Plaid
# source, so a SimpleFIN account left unanswered here stays permanently
# unposted (ledger_sync.py logs SkipReason.UNMAPPED_BANK_ACCOUNT) rather
# than risk landing in the wrong place. Every account SimpleFIN returns
# must get an explicit answer -- a real Ledger account, a deliberate
# exclusion, or a brand-new account created for it -- before this screen
# lets the user continue. Nothing here is ever preselected for an
# unanswered account; an account that already has an answer from a
# previous visit shows that real answer selected, because that reflects
# the truth already on file, not a guess.
#
# This screen is also the "edit account mapping" re-entry point a later
# task wires from the Connections page (see _require_simplefin_stage's
# docstring: "mapping" or any later stage satisfies the gate), so it must
# render a person's PRIOR answers correctly on every revisit, not just the
# first.
#
# Form field naming (stable -- later tasks/screens may rely on this):
#   mapping__<account_id>       the answer for one SimpleFIN account:
#                                 ""          unanswered (never submitted
#                                             on purpose; only a forged/
#                                             incomplete POST sends this)
#                                 "__exclude__"  deliberately not imported
#                                 "__new__"      create a new Ledger account
#                                 <ledger account CODE>  map to that
#                                             existing asset/liability
#                                             account (resolved via
#                                             ledger.account_by_code())
#   new_account_name__<account_id>   paired text field, only meaningful
#                                     when mapping__<account_id> == "__new__"
#   new_account_type__<account_id>   paired type field ("asset" or
#                                     "liability"), same condition


_UNSET = object()


def _fetch_simplefin_accounts_or_none(adapter):
    """Live SimpleFIN account list, or None if it can't be reached right
    now (missing credentials or any adapter/network failure) -- never
    raises, so a transient outage renders the "could not reach" state
    instead of a 500."""
    credentials = adapter.stored_credentials()
    if credentials is None:
        return None
    try:
        return adapter.list_accounts(credentials)
    except Exception:
        return None


def _eligible_ledger_accounts():
    """Asset/liability Ledger accounts only -- a bank account is a source
    of money or a debt, never a revenue/expense/equity category. Mirrors
    the reasoning app.py's get_categories() already uses for a similar
    filtered-list pattern."""
    return [account for account in ledger.list_accounts()
            if account["type"] in ("asset", "liability")]


def _stale_simplefin_ledger_ids(live_ids):
    """Ledger account ids still claimed by a stored SimpleFIN
    bank_accounts row that is NOT among ``live_ids`` -- an account
    SimpleFIN stopped reporting (the institution disconnected it, or the
    person closed it on their end). Such an account has no way to answer
    for itself here ever again, so a Ledger account it still holds must
    not permanently block a live account from claiming it -- neither in
    the rendered dropdown (see _build_accounts_form) nor in the actual
    write (see the pre-clear step in setup_simplefin_accounts below).
    """
    live_id_set = set(live_ids)
    return {row["ledger_account_id"]
            for row in db.list_bank_accounts("simplefin")
            if row["account_id"] not in live_id_set
            and row["ledger_account_id"] is not None}


def _sync_simplefin_accounts(live_accounts):
    """Seed/refresh a bank_accounts row for every account this live list
    contains, so the row exists (and reflects current name/mask) whether
    this call originated from the GET render or the POST handler.
    db.upsert_bank_account is idempotent and never clobbers an existing
    mapping_state or ledger_account_id.

    This MUST run before any write to a bank_accounts row for one of these
    accounts -- set_bank_ledger_account()/exclude_bank_account() are plain
    UPDATEs that silently affect zero rows (and, in
    set_bank_ledger_account's case, read back as a collision-shaped
    ``False``) if the row was never created. Returns the deduplicated,
    order-preserving list of account IDs actually seeded -- the only IDs a
    caller may treat as real going forward.
    """
    synced_at = datetime.now(timezone.utc).isoformat()
    ids = []
    seen = set()
    for account in live_accounts:
        account_id = str(account.get("id") or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
        db.upsert_bank_account({
            "source": "simplefin", "account_id": account_id,
            "name": account.get("name") or "", "official_name": None,
            "type": "", "subtype": "", "mask": account.get("mask"),
            "synced_at": synced_at,
        })
    return ids


def render_simplefin_accounts(message=None, error=None, live_accounts=_UNSET):
    """Render Screen 3 for a GET (or a POST failure that occurred before
    ANY bank_accounts write, i.e. the "could not reach SimpleFIN" case).
    ``live_accounts`` lets a caller reuse an account list it already
    fetched instead of hitting SimpleFIN a second time.

    This is the ONE path that seeds/refreshes bank_accounts rows
    (_sync_simplefin_accounts -- a write). A POST that fails validation
    AFTER a live account list was already obtained must re-render WITHOUT
    writing anything (every bank_accounts row has to stay byte-identical
    across a failed attempt) -- that path calls _build_accounts_form
    directly instead of this function. See setup_simplefin_accounts below.
    """
    adapter = banks.get_adapter("simplefin")
    if live_accounts is _UNSET:
        live_accounts = _fetch_simplefin_accounts_or_none(adapter)

    if live_accounts is None:
        back_link = ('<a class="skip-link" href="/setup/simplefin/token">'
                    '&larr; Back</a>')
        content = f"""
        <div class="section">
          <div class="section-head"><h2>Map your SimpleFIN accounts</h2>
            <span class="tag">required</span></div>
          <div class="none-note">Could not reach {escape(adapter.display_name)}
            to list accounts. Check the connection and reload.</div>
          <div class="wizard-actions">{back_link}</div>
        </div>"""
        return wizard_shell(3, content, message=message, error=error)

    # Seed/refresh bank_accounts rows for everything SimpleFIN returned, so
    # the account list (and any answer already given) is durable across a
    # reload even before mapping finishes.
    _sync_simplefin_accounts(live_accounts)
    return _build_accounts_form(adapter, live_accounts, message, error)


def _build_accounts_form(adapter, live_accounts, message, error):
    """Build the mapping-form HTML from an already-fetched, non-None live
    account list. Strictly read-only (db.list_bank_accounts and
    db.taken_ledger_account_ids only) -- makes no writes to bank_accounts,
    so it is safe to call from a POST validation failure, where every row
    must stay byte-identical to before the attempt. Name/mask/balance are
    read straight from ``live_accounts`` (not from a bank_accounts row),
    so this renders correctly even for an account no GET has ever synced
    yet; ``mapping_state``/``ledger_account_id`` still come from
    bank_accounts, defaulting to "unanswered" via dict.get() when no row
    exists.
    """
    back_link = ('<a class="skip-link" href="/setup/simplefin/token">'
                '&larr; Back</a>')

    live_ids = []
    live_by_id = {}
    for account in live_accounts:
        account_id = str(account.get("id") or "").strip()
        if account_id and account_id not in live_by_id:
            live_ids.append(account_id)
            live_by_id[account_id] = account

    if not live_ids:
        content = f"""
        <div class="section">
          <div class="section-head"><h2>Map your SimpleFIN accounts</h2>
            <span class="tag">required</span></div>
          <div class="none-note">{escape(adapter.display_name)} returned no
            accounts.</div>
          <div class="wizard-actions">{back_link}</div>
        </div>"""
        return wizard_shell(3, content, message=message, error=error)

    all_rows = db.list_bank_accounts("simplefin")
    rows_by_id = {row["account_id"]: row for row in all_rows}
    live_id_set = set(live_ids)
    # Stable, case-insensitive-by-name order (list_bank_accounts' own
    # ordering), restricted to accounts SimpleFIN actually returned this
    # visit -- a stale row for an account that has since disappeared from
    # SimpleFIN's side is never shown. An account with no bank_accounts
    # row yet (only possible on a POST-failure re-render that skipped the
    # sync write) has no place in that order yet -- append it at the end
    # rather than drop it.
    ordered_known_ids = [row["account_id"] for row in all_rows
                        if row["account_id"] in live_id_set]
    ordered_ids = ordered_known_ids + [
        account_id for account_id in live_ids
        if account_id not in set(ordered_known_ids)]

    eligible_accounts = _eligible_ledger_accounts()
    code_by_ledger_id = {account["id"]: account["code"]
                        for account in eligible_accounts}
    taken = db.taken_ledger_account_ids()
    # Ledger accounts still claimed only by a since-vanished SimpleFIN
    # account (see _stale_simplefin_ledger_ids) must not read as
    # "taken" here -- otherwise a live account could never reclaim a code
    # its predecessor left behind, since reloading this screen would show
    # it disabled forever with no control to free it.
    stale_claims = _stale_simplefin_ledger_ids(live_ids)

    row_html = ""
    js_wires = ""
    for index, account_id in enumerate(ordered_ids):
        row = rows_by_id.get(account_id, {})
        live = live_by_id.get(account_id, {})
        name = live.get("name") or row.get("name") or account_id
        mask = row.get("mask")
        balance = live.get("balance")
        current_state = row.get("mapping_state") or ""
        current_ledger_id = row.get("ledger_account_id")

        selected_value = ""
        if current_state == "excluded":
            selected_value = "__exclude__"
        elif current_ledger_id and current_ledger_id in code_by_ledger_id:
            selected_value = code_by_ledger_id[current_ledger_id]

        options = '<option value="">Choose...</option>'
        for account in eligible_accounts:
            claimant = taken.get(account["id"])
            is_disabled = (claimant is not None
                          and claimant != ("simplefin", account_id)
                          and account["id"] not in stale_claims)
            is_selected = account["code"] == selected_value
            options += (
                f'<option value="{escape(account["code"])}"'
                f'{" selected" if is_selected else ""}'
                f'{" disabled" if is_disabled else ""}>'
                f'{escape(account["code"])} — {escape(account["name"])} '
                f'({escape(account["type"])})</option>'
            )
        options += (
            f'<option value="__exclude__"'
            f'{" selected" if selected_value == "__exclude__" else ""}>'
            "Exclude — do not import this account</option>"
        )
        options += '<option value="__new__">Create a new account for this...</option>'

        mask_html = f" ••••{escape(mask)}" if mask else ""
        balance_html = (ui.money(balance)
                       if isinstance(balance, (int, float)) else "")

        select_name = escape(f"mapping__{account_id}")
        new_name_field = escape(f"new_account_name__{account_id}")
        new_type_field = escape(f"new_account_type__{account_id}")
        select_id = f"mapping-select-{index}"
        new_fields_id = f"new-fields-{index}"

        row_html += f"""
        <div class="cred-field simplefin-account-row">
          <label class="q" for="{select_id}">{escape(name)}{mask_html}</label>
          <div class="hint">Balance: {balance_html}</div>
          <select id="{select_id}" class="mapping-select" name="{select_name}">
            {options}
          </select>
          <div class="new-account-fields" id="{new_fields_id}"
               style="display:none; margin-top:8px;">
            <input type="text" name="{new_name_field}" maxlength="100"
                   placeholder="New account name" autocomplete="off">
            <select name="{new_type_field}">
              <option value="">Choose type...</option>
              <option value="asset">Asset (money you have)</option>
              <option value="liability">Liability (money you owe)</option>
            </select>
          </div>
        </div>"""
        js_wires += f"wireMappingRow('{select_id}', '{new_fields_id}');\n"

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Map your SimpleFIN accounts</h2>
        <span class="tag">required</span></div>
      <div class="hint" style="margin-bottom:14px;">SimpleFIN found
        {len(ordered_ids)} account(s). Choose where each one posts in your
        Ledger, or exclude it -- {APP_NAME} never guesses this for you, so
        every account needs an explicit answer before you can continue.</div>
      <form method="post" action="/setup/simplefin/accounts">
        {row_html}
        <div class="wizard-actions">
          <button class="save-btn" type="submit" id="accounts-continue" disabled>
            Save and continue</button>
          {back_link}
        </div>
      </form>
    </div>"""

    extra_js = js_wires + """
    function wireMappingRow(selectId, newFieldsId) {
      const select = document.getElementById(selectId);
      const fields = document.getElementById(newFieldsId);
      function update() {
        fields.style.display = select.value === '__new__' ? '' : 'none';
      }
      select.addEventListener('change', update);
      update();
    }
    const accountsContinueBtn = document.getElementById('accounts-continue');
    const mappingSelects = document.querySelectorAll('.mapping-select');
    function checkAllAccountsAnswered() {
      accountsContinueBtn.disabled = Array.prototype.some.call(
        mappingSelects, function(s) { return s.value === ''; });
    }
    Array.prototype.forEach.call(mappingSelects, function(s) {
      s.addEventListener('change', checkAllAccountsAnswered);
    });
    checkAllAccountsAnswered();
    """
    return wizard_shell(3, content, message=message, error=error,
                        extra_js=extra_js)


@router.get("/setup/simplefin/accounts", response_class=HTMLResponse)
def setup_simplefin_accounts_screen(msg: str = None, err: str = None):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_MAPPING)
    if redirect is not None:
        return redirect
    return render_simplefin_accounts(message=msg, error=err)


@router.post("/setup/simplefin/accounts")
async def setup_simplefin_accounts(request: Request):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_MAPPING)
    if redirect is not None:
        return redirect

    adapter = banks.get_adapter("simplefin")
    # Never trust the submitted form about WHICH accounts exist -- only
    # what the user chose for each. Re-fetch the live list exactly like
    # the GET handler does.
    live_accounts = _fetch_simplefin_accounts_or_none(adapter)
    if live_accounts is None:
        return HTMLResponse(render_simplefin_accounts(
            live_accounts=None,
            error=f"Could not reach {adapter.display_name} to list "
                 "accounts. Check the connection and reload, then try "
                 "again."))

    form = await request.form()

    # Derive the account ID/name set from the live list directly -- NOT
    # via _sync_simplefin_accounts yet. That helper writes to
    # bank_accounts (refreshing synced_at/name/mask), and validation below
    # can still fail; a failed attempt must leave every bank_accounts row
    # byte-identical to before the POST, so the actual sync write is
    # deferred until after every check below passes (see "Writes" below).
    account_ids = []
    name_by_id = {}
    for account in live_accounts:
        account_id = str(account.get("id") or "").strip()
        if account_id and account_id not in name_by_id:
            account_ids.append(account_id)
            name_by_id[account_id] = str(account.get("name") or account_id)

    eligible_accounts = _eligible_ledger_accounts()
    accounts_by_code = {account["code"]: account for account in eligible_accounts}

    # ---- Validate everything before writing anything (all-or-nothing).
    errors = []
    parsed = {}  # account_id -> ("existing", account) | ("exclude", None)
                # | ("new", name, type)
    for account_id in account_ids:
        label = name_by_id[account_id]
        raw = form.get(f"mapping__{account_id}")
        raw = str(raw).strip() if raw is not None else ""
        if not raw:
            errors.append(f'"{label}" still needs an answer.')
            continue
        if raw == "__exclude__":
            parsed[account_id] = ("exclude", None)
        elif raw == "__new__":
            new_name = str(
                form.get(f"new_account_name__{account_id}") or "").strip()
            new_type = str(
                form.get(f"new_account_type__{account_id}") or "").strip()
            if not new_name:
                errors.append(f'"{label}": give the new account a name.')
                continue
            if len(new_name) > 100:
                # A nicer, per-account message for the common case -- NOT
                # the thing standing between this and an orphaned account.
                # ledger.create_accounts() below enforces the same 100
                # character limit itself, inside its one all-or-nothing
                # transaction, regardless of whether this check is ever
                # bypassed (a forged POST) or this limit ever changes.
                errors.append(
                    f'"{label}": account names are limited to 100 characters.')
                continue
            if new_type not in ("asset", "liability"):
                errors.append(
                    f'"{label}": choose whether the new account is an '
                    "asset or a liability.")
                continue
            parsed[account_id] = ("new", new_name, new_type)
        else:
            target = accounts_by_code.get(raw)
            if target is None:
                errors.append(
                    f'"{label}": that is not a recognized asset or '
                    "liability account.")
                continue
            parsed[account_id] = ("existing", target)

    if errors:
        return HTMLResponse(_build_accounts_form(
            adapter, live_accounts, None, " ".join(errors)))

    # No two accounts in this submission may claim the same EXISTING
    # ledger account -- the dropdown disables already-claimed options
    # client-side, but a forged POST could still pick two disabled ones.
    code_claims = {}
    for account_id, info in parsed.items():
        if info[0] != "existing":
            continue
        code = info[1]["code"]
        if code in code_claims:
            other_id = code_claims[code]
            errors.append(
                f'"{name_by_id[other_id]}" and "{name_by_id[account_id]}" '
                f"both selected the same ledger account ({code}). Choose "
                "a different account for one of them.")
        else:
            code_claims[code] = account_id

    # No two accounts in this submission may request "__new__" with the
    # same name (case-insensitive, matching ledger.create_account's own
    # duplicate-name check) -- catch it here for a clearer message instead
    # of letting the second create_account() call raise a generic one.
    new_name_claims = {}
    for account_id, info in parsed.items():
        if info[0] != "new":
            continue
        key = info[1].lower()
        if key in new_name_claims:
            other_id = new_name_claims[key]
            errors.append(
                f'"{name_by_id[other_id]}" and "{name_by_id[account_id]}" '
                f'both named their new account "{info[1]}". Give them '
                "different names.")
        else:
            new_name_claims[key] = account_id

    # A "__new__" name must also not collide with a name that ALREADY
    # exists anywhere in the Chart of Accounts (from a prior visit, or
    # from anything else in the app) -- checked here, before any writes,
    # so a mid-submission collision can never leave an EARLIER "__new__"
    # account in this same batch already created and committed by the
    # time ledger.create_account's own duplicate-name check would
    # otherwise catch a LATER one. Without this, "validate everything
    # before writing anything" would be violated: the submission as a
    # whole would still be refused, but a Chart-of-Accounts row from
    # this same failed attempt would be left behind. This spans every
    # account type (not just asset/liability), matching the scope of
    # ledger.create_account's own check.
    existing_chart_names = {account["name"].lower()
                            for account in ledger.list_accounts()}
    for account_id, info in parsed.items():
        if info[0] != "new":
            continue
        key = info[1].lower()
        if key in existing_chart_names:
            errors.append(
                f'"{name_by_id[account_id]}": an account named '
                f'"{info[1]}" already exists in your Chart of Accounts. '
                "Choose a different name.")

    if errors:
        return HTMLResponse(_build_accounts_form(
            adapter, live_accounts, None, " ".join(errors)))

    # Every check above passed -- now it's safe to seed/refresh the
    # bank_accounts rows (the same call the GET render makes), guaranteeing
    # a row exists for every account_id before the mapping writes below
    # touch it. set_bank_ledger_account/exclude_bank_account are plain
    # UPDATEs that would otherwise silently affect zero rows (read back as
    # a collision-shaped ``False``) if a row had never been created.
    _sync_simplefin_accounts(live_accounts)

    # ---- Writes. Create every "__new__" Ledger account in ONE call to
    # ledger.create_accounts() -- NOT a per-item loop calling
    # create_account(). create_accounts() runs the whole batch inside a
    # single database transaction, so if ANY spec in it fails for ANY
    # reason (duplicate name, too long, an invalid type, code-band
    # exhaustion, or anything a future validation rule adds -- the
    # pre-checks above already catch the common, nameable cases for a
    # nicer error message, but they are NOT what makes this safe), every
    # account already created earlier in the SAME call is rolled back
    # too, automatically, by SQLite's own transaction mechanics. Nothing
    # is left orphaned in the Chart of Accounts on any failure here.
    new_specs = []
    new_spec_account_ids = []  # same order as new_specs
    for account_id in account_ids:
        info = parsed[account_id]
        if info[0] == "new":
            _, new_name, new_type = info
            new_specs.append((new_name, new_type))
            new_spec_account_ids.append(account_id)

    resolved = {}  # account_id -> ledger account dict, for non-excluded rows
    if new_specs:
        try:
            created_accounts = ledger.create_accounts(new_specs)
        except ledger.LedgerValidationError as exc:
            # Deliberately not attributed to one specific account: with a
            # single batch call we do not know which spec failed (nor
            # need to -- every earlier spec in this same call was already
            # rolled back along with it, so there is nothing partially
            # applied to explain away).
            return HTMLResponse(_build_accounts_form(
                adapter, live_accounts, None,
                f"Could not create your new account(s): {exc}"))
        for created_account_id, created_account in zip(
                new_spec_account_ids, created_accounts):
            resolved[created_account_id] = created_account
    for account_id in account_ids:
        info = parsed[account_id]
        if info[0] == "existing":
            resolved[account_id] = info[1]

    # Release any Ledger account THIS batch is about to (re)claim, from
    # whoever currently holds it, before the final write loop below --
    # two cases:
    #   1. A genuine intra-batch swap: account A in THIS batch currently
    #      holds a code that account B in THIS batch now wants, and A's
    #      own final answer is something else. Pre-validation above
    #      already forbids two accounts requesting the same FINAL target,
    #      so this only fires when a code changes hands within the batch;
    #      without releasing A first, B's set_bank_ledger_account call
    #      below could lose a race against A's own still-held mapping
    #      purely because of write order.
    #   2. A STALE claim: a SimpleFIN account that no longer appears in
    #      this visit's live list (institution disconnect, or the person
    #      closed it on their end) but whose bank_accounts row still
    #      holds a Ledger account this batch wants. That account has no
    #      way to answer for itself here ever again, so it must not be
    #      able to permanently block a live account from claiming it.
    # db.clear_bank_ledger_account -- NOT exclude_bank_account -- is used
    # here deliberately: it only nulls ledger_account_id, leaving
    # mapping_state exactly as it was. exclude_bank_account would instead
    # write mapping_state = 'excluded', a specific claim about what the
    # PERSON chose. If anything failed between this step and the final
    # write loop below, a released account would read back as merely
    # unanswered (honest, given the write did not complete) rather than
    # as an exclusion nobody actually chose.
    current_rows = {row["account_id"]: row
                    for row in db.list_bank_accounts("simplefin")}
    resolved_target_ids = {account["id"] for account in resolved.values()}
    for row_account_id, row in current_rows.items():
        old_ledger_id = row["ledger_account_id"]
        if not old_ledger_id or old_ledger_id not in resolved_target_ids:
            continue
        if row_account_id in parsed:
            # Part of this batch -- only release it if it is actually
            # GIVING UP this code (its own final answer targets something
            # else, or it is being excluded). If it is keeping its own
            # mapping, there is nothing to release.
            new_ledger_id = resolved.get(row_account_id, {}).get("id")
            if new_ledger_id == old_ledger_id:
                continue
        # Either outside this batch entirely (case 2, a stale/vanished
        # account) or inside it but genuinely giving the code up (case 1).
        db.clear_bank_ledger_account("simplefin", row_account_id)

    for account_id in account_ids:
        info = parsed[account_id]
        if info[0] == "exclude":
            db.exclude_bank_account("simplefin", account_id)
            continue
        target = resolved[account_id]
        ok = db.set_bank_ledger_account("simplefin", account_id, target["id"])
        if not ok:
            # Already pre-validated against duplicates in THIS submission,
            # and any stale or intra-batch holder was already released
            # above, so a False here means something changed concurrently
            # (e.g. another process claimed the target between validation
            # and write) -- treat it as a failure and say so, rather than
            # silently continuing past an account that didn't actually
            # get mapped.
            return HTMLResponse(_build_accounts_form(
                adapter, live_accounts, None,
                f'"{name_by_id[account_id]}": that ledger account '
                "was just claimed by another connection. Reload and "
                "try again."))

    _simplefin_advance_stage(SIMPLEFIN_STAGE_CUTOVER)
    return RedirectResponse("/setup/simplefin/cutover", status_code=303)


# ------------------------------------------- step 3e: SimpleFIN cutover
#
# Screen 4 of the dedicated /setup/simplefin flow: date + opening-balance
# collection ONLY (Task 5). This screen never writes to journal_entries,
# journal_lines, or banks.ACTIVE_BANK_KEY -- it persists a DRAFT under
# SIMPLEFIN_PENDING_CUTOVER_KEY (schema documented on that constant,
# below) that Task 6's Screen 5 (confirm) reads back and is the one that
# actually posts opening-balance journal entries and activates SimpleFIN
# as the bank.
#
# WHY this screen exists: SimpleFIN's own API only ever returns
# banks.simplefin.MAX_HISTORY_DAYS days of transaction history --
# enforced server-side by SimpleFIN, and mirrored client-side by
# banks/simplefin.py::_start_date(). Anything before the chosen start
# date has to enter the books as a starting balance, never guessed and
# never defaulted to zero, or the books will not match the bank. Every
# date-boundary computation and every piece of UI copy below reads
# MAX_HISTORY_DAYS from banks.simplefin directly -- never a hardcoded
# "90" -- so this screen can never silently drift from what the adapter
# itself enforces.

SIMPLEFIN_PENDING_CUTOVER_KEY = "simplefin_pending_cutover"
# The persisted draft this screen writes (via db.set_setting, JSON-
# encoded) and Task 6 reads back:
#   {
#     "start_date": "2026-06-01",              # ISO date string
#     "balances_cents": {                       # account_id -> int cents
#       "acc-checking": 150000,
#       "acc-savings": 300000
#     }
#   }
# Every value under "balances_cents" is an ALREADY-VALIDATED, non-
# negative integer number of cents -- never a float, never a raw string
# -- matching Klearing's "integer cents at the DB/settings boundary"
# rule for money everywhere else. This key holds a DRAFT ONLY: writing
# it never touches journal_entries, journal_lines, or
# banks.ACTIVE_BANK_KEY.

_CUTOVER_CENT = Decimal("0.01")
# A generous, clearly-safe ceiling on a single account's opening balance --
# no real small-business bank account opens with a $1B+ balance, and this
# stays comfortably under SQLite's own 64-bit INTEGER range (+/- ~9.22e18)
# even after Task 6 sums several accounts together. Without this ceiling,
# Screen 4 could accept and persist a draft value that Task 6's real
# ledger.create_sourced_journal_entry() call cannot post at all --
# ledger._amount_to_cents has no upper bound either, and an oversized int
# passed to SQLite raises a bare OverflowError, not a
# ledger.LedgerValidationError Task 6's normal error handling expects.
_MAX_OPENING_BALANCE_CENTS = 100_000_000_000  # $1,000,000,000.00


def _mapped_simplefin_accounts():
    """Genuinely MAPPED SimpleFIN accounts only -- ledger_account_id IS
    NOT NULL. set_bank_ledger_account/exclude_bank_account (db.py) keep
    ledger_account_id and mapping_state consistent with each other, so
    an excluded account never carries a ledger_account_id -- filtering
    on ledger_account_id alone already excludes it. Excluded accounts
    need no opening balance and must never appear on this screen, in
    either its GET render or its POST validation."""
    return [row for row in db.list_bank_accounts("simplefin")
            if row.get("ledger_account_id") is not None]


def _cutover_date_bounds():
    """(earliest, latest) allowed start date, both inclusive -- derived
    from the REAL banks.simplefin.MAX_HISTORY_DAYS constant, never a
    hardcoded "90", so this can never drift from what the SimpleFIN
    adapter itself enforces (banks/simplefin.py::_start_date computes
    the identical floor: today - (MAX_HISTORY_DAYS - 1))."""
    today = date.today()
    return today - timedelta(days=simplefin.MAX_HISTORY_DAYS - 1), today


def _opening_balance_to_cents(raw, label):
    """Validate one opening-balance amount and return it as integer
    cents. Mirrors ledger.py's _amount_to_cents validation rules EXACTLY
    (Decimal(str(value)), is_finite(), >= 0, quantize to 2 decimals with
    ROUND_HALF_UP, reject if the quantized value differs from the input
    -- i.e. more than two decimal places were entered) without importing
    that function directly -- it lives in ledger.py and is tied to
    journal-line posting semantics, which this screen must not touch
    (persisting a DRAFT is all Task 5 does; Task 6 is the one that
    actually calls ledger.create_sourced_journal_entry). Also caps the
    result at _MAX_OPENING_BALANCE_CENTS -- see that constant's comment
    for why. Raises ValueError, naming `label`, on any violation -- never
    returns a partially-valid amount.

    Decimal(...) and .quantize(...) are wrapped in ONE try/except, not
    two -- an extreme-exponent input (e.g. "1e400", which a plain
    <input type="number"> happily accepts via scientific notation) parses
    into a syntactically valid Decimal just fine, and only blows up at
    .quantize() with decimal.InvalidOperation (a context precision
    error, not ValueError -- ArithmeticError is its base, so a bare
    `except ValueError` never catches it). Splitting the two calls across
    separate try blocks was exactly what let that escape as a raw HTTP
    500 instead of a friendly per-account error banner. This merged
    shape matches plaid_client._amount_to_cents and
    check_bank._bank_amount_to_cents, the other two boundary-local
    converters in this codebase that already guard against the same
    failure mode the same way."""
    try:
        amount = Decimal(str(raw))
        rounded = amount.quantize(_CUTOVER_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} must be a valid amount.")
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be non-negative.")
    if rounded != amount:
        raise ValueError(f"{label} cannot have more than two decimals.")
    cents = int(rounded * 100)
    if cents > _MAX_OPENING_BALANCE_CENTS:
        # ui.money(), not a hardcoded "$1,000,000,000.00" string -- the
        # ceiling is stated from the same constant that enforces it, the
        # same anti-drift discipline _cutover_date_bounds() already
        # applies to MAX_HISTORY_DAYS.
        raise ValueError(
            f"{label} cannot exceed "
            f"{ui.money(Decimal(_MAX_OPENING_BALANCE_CENTS) / 100)}.")
    return cents


def _cents_to_input_value(cents):
    """Format an integer-cents amount as the plain decimal string an
    <input type="number" step="0.01"> expects (e.g. 150000 -> "1500.00").
    Decimal all the way through -- this is purely a cosmetic prefill, but
    it still never turns money into a float, matching the rule
    everywhere else."""
    return str((Decimal(cents) / 100).quantize(_CUTOVER_CENT))


def _read_pending_cutover_draft():
    """The persisted Screen 4 draft, or None if unset or corrupt. Corrupt
    or hand-edited JSON fails closed to "no draft" (blank fields on
    resume) rather than crashing the screen or trusting partial data --
    the same posture ga4_client.conversion_events() already takes toward
    a malformed settings row."""
    raw = db.get_setting(SIMPLEFIN_PENDING_CUTOVER_KEY)
    if not raw:
        return None
    try:
        draft = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(draft, dict):
        return None
    start_date_value = draft.get("start_date")
    balances = draft.get("balances_cents")
    if not isinstance(start_date_value, str) or not isinstance(balances, dict):
        return None
    return {"start_date": start_date_value, "balances_cents": balances}


def _validated_cutover_draft():
    """The persisted Screen 4 draft, FULLY validated, or None if no draft
    exists or ANYTHING about it fails validation -- this is the interface
    Task 6 must use to read this draft back, not
    _read_pending_cutover_draft() above (which only checks top-level JSON
    shape: a dict, with a start_date that's *a* string and a
    balances_cents that's *a* dict -- it never confirms the date actually
    parses, is still within today's _cutover_date_bounds() window, or
    that every balance is a genuine non-negative int). Task 6 is the one
    that turns this draft into real money movement
    (ledger.create_sourced_journal_entry calls), so it cannot afford to
    inherit a reader that would hand it a string balance, a float, a
    bool, a negative number, or a date that has since rolled outside
    SimpleFIN's live history window because time passed between when
    this draft was written and when Task 6 reads it.

    On success, returns {"start_date": a real datetime.date (not a
    string), "balances_cents": {account_id: non-negative int, at most
    _MAX_OPENING_BALANCE_CENTS}}. Deliberately all-or-nothing: a single
    corrupt field invalidates the WHOLE draft (returns None) rather than
    returning a partially-trustworthy dict, because Screen 5/6 cannot
    safely post some of a broken draft and skip the rest."""
    draft = _read_pending_cutover_draft()
    if draft is None:
        return None

    try:
        start_date = date.fromisoformat(draft["start_date"])
    except (ValueError, TypeError):
        return None
    earliest, latest = _cutover_date_bounds()
    if start_date < earliest or start_date > latest:
        return None

    validated_balances = {}
    for account_id, cents in draft["balances_cents"].items():
        if not isinstance(account_id, str) or not account_id:
            return None
        if not isinstance(cents, int) or isinstance(cents, bool):
            return None
        if cents < 0 or cents > _MAX_OPENING_BALANCE_CENTS:
            return None
        validated_balances[account_id] = cents

    return {"start_date": start_date, "balances_cents": validated_balances}


def render_simplefin_cutover(message=None, error=None):
    """Render Screen 4 for a GET, or for a POST that failed validation.
    The failure path calls this exact function too -- NEVER a form built
    from the just-submitted, possibly-invalid values -- so a failed
    attempt always shows the same resumable state a fresh GET would: the
    persisted draft from before this attempt, or blank fields if none
    exists yet. This mirrors Screen 3's own re-render posture (its POST
    failures re-render from bank_accounts/Ledger DB state via
    _build_accounts_form, never from the submitted form)."""
    mapped_rows = _mapped_simplefin_accounts()
    back_link = ('<a class="skip-link" href="/setup/simplefin/accounts">'
                '&larr; Back</a>')

    if not mapped_rows:
        content = f"""
        <div class="section">
          <div class="section-head"><h2>Set your cutover date and opening
            balances</h2><span class="tag">required</span></div>
          <div class="none-note">No accounts were mapped yet — go back to
            Account Mapping and choose at least one.</div>
          <div class="wizard-actions">{back_link}</div>
        </div>"""
        return wizard_shell(3, content, message=message, error=error)

    earliest, latest = _cutover_date_bounds()
    # The VALIDATED reader, not the raw one -- see _validated_cutover_draft's
    # own docstring for why Task 6 (and this render, which is the other
    # consumer of this draft) must never trust top-level-shape-only JSON.
    validated_draft = _validated_cutover_draft()
    date_value = (validated_draft["start_date"].isoformat()
                 if validated_draft else "")
    balances_draft = validated_draft["balances_cents"] if validated_draft else {}

    row_html = ""
    for row in mapped_rows:
        account_id = row["account_id"]
        name = row.get("name") or account_id
        mask = row.get("mask")
        mask_html = f" ••••{escape(mask)}" if mask else ""
        cents = balances_draft.get(account_id)
        prefill = _cents_to_input_value(cents) if cents is not None else ""
        field_name = f"balance__{account_id}"
        # Raw (never pre-escaped) -- escape() is called at every point of
        # use below (for=, id=, name=), matching every neighboring
        # interpolation in this function instead of baking an escaped
        # value into a variable and reusing it unescaped at the display
        # site (a trap for a future editor, even though it was safe here
        # today).
        input_id = f"in-{field_name}"
        row_html += f"""
        <div class="cred-field">
          <label class="q" for="{escape(input_id)}">{escape(name)}{mask_html}</label>
          <div class="hint">Balance the day before your start date — what
            was already in this account the day before {APP_NAME} starts
            tracking it. Type 0 if it was genuinely zero.</div>
          <input type="number" step="0.01" min="0" inputmode="decimal"
                 id="{escape(input_id)}" name="{escape(field_name)}"
                 value="{escape(prefill)}" placeholder="0.00">
        </div>"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Set your cutover date and opening
        balances</h2><span class="tag">required</span></div>
      <div class="hint" style="margin-bottom:14px;">SimpleFIN only
        returns {simplefin.MAX_HISTORY_DAYS} days of transaction history
        — anything before that has to enter your books as a starting
        balance, or your books won't match the bank.</div>
      <form method="post" action="/setup/simplefin/cutover">
        <div class="cred-field">
          <label class="q" for="cutover-start-date">Start pulling
            transactions from</label>
          <div class="hint">Earliest allowed: {escape(earliest.isoformat())}
            ({simplefin.MAX_HISTORY_DAYS} days ago) — SimpleFIN itself
            will not return anything earlier than that.</div>
          <input type="date" id="cutover-start-date" name="start_date"
                 value="{escape(date_value)}"
                 min="{escape(earliest.isoformat())}"
                 max="{escape(latest.isoformat())}" required>
        </div>
        {row_html}
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Save and continue</button>
          {back_link}
        </div>
      </form>
    </div>"""
    return wizard_shell(3, content, message=message, error=error)


@router.get("/setup/simplefin/cutover", response_class=HTMLResponse)
def setup_simplefin_cutover_screen(msg: str = None, err: str = None):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_CUTOVER)
    if redirect is not None:
        return redirect
    if simplefin_stage() == SIMPLEFIN_STAGE_DONE:
        # _require_simplefin_stage(SIMPLEFIN_STAGE_CUTOVER) above is a
        # >= check, so it still passes once the stage reaches "done" --
        # correctly, since /setup/simplefin/accounts (the mapping
        # screen) must stay reachable past "done" on purpose (the
        # future "edit mapping" re-entry point). But THIS screen, and
        # Screen 5 after it, are permanently short-circuited past
        # "done" in this task: without this check, a person who mapped
        # a brand-new account after a normal finish could fill out a
        # genuine opening balance here, submit it, and have Screen 5
        # bounce it straight to the "already connected" banner without
        # ever reading it -- silently orphaning real user input in
        # SIMPLEFIN_PENDING_CUTOVER_KEY forever. Refusing HERE, before
        # any form is even rendered, closes that dead end at the point
        # of entry instead of after the user has done work.
        return _simplefin_done_redirect(_already_done_banner())
    return render_simplefin_cutover(message=msg, error=err)


@router.post("/setup/simplefin/cutover")
async def setup_simplefin_cutover(request: Request):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_CUTOVER)
    if redirect is not None:
        return redirect
    if simplefin_stage() == SIMPLEFIN_STAGE_DONE:
        # Same short-circuit as the GET handler above, and for the same
        # reason -- a forged or direct POST (bypassing the GET's own
        # check) must be refused identically, before it ever writes
        # anything to SIMPLEFIN_PENDING_CUTOVER_KEY.
        return _simplefin_done_redirect(_already_done_banner())

    # Never trust the submitted form about WHICH accounts need an answer
    # -- re-derive the mapped-account list from the database exactly like
    # the GET handler does (same principle as Screen 3's POST).
    mapped_rows = _mapped_simplefin_accounts()
    if not mapped_rows:
        # Reachable non-adversarially (e.g. two browser tabs open, the
        # other one just unmapped every account on Screen 3) -- every
        # other failure path on this screen passes a specific error
        # message via wizard_shell's banner, matching Screen 3's own
        # "could not reach / no accounts" POST-failure precedent
        # (render_simplefin_accounts's unreachable-state branch does the
        # same: a baked-in none-note explanation in the content, plus a
        # separate error= banner).
        return HTMLResponse(render_simplefin_cutover(
            error="No accounts were mapped yet — go back to Account "
                 "Mapping and choose at least one."))

    form = await request.form()
    earliest, latest = _cutover_date_bounds()
    errors = []

    # ---- Validate the date.
    raw_date = str(form.get("start_date") or "").strip()
    start_date = None
    if not raw_date:
        errors.append("Choose a start date.")
    else:
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError:
            errors.append("Start date must be a valid date (YYYY-MM-DD).")
        else:
            if parsed_date < earliest or parsed_date > latest:
                errors.append(
                    "Start date must be between "
                    f"{earliest.isoformat()} and {latest.isoformat()} "
                    "— SimpleFIN only returns "
                    f"{simplefin.MAX_HISTORY_DAYS} days of history.")
            else:
                start_date = parsed_date

    # ---- Validate every mapped account's balance -- ALL of them, even
    # after the date already failed, so a single resubmission can surface
    # every problem at once rather than one error per round trip.
    balances_cents = {}
    for row in mapped_rows:
        account_id = row["account_id"]
        label = row.get("name") or account_id
        raw_balance = form.get(f"balance__{account_id}")
        raw_balance = str(raw_balance).strip() if raw_balance is not None else ""
        if not raw_balance:
            # Blank is an error, never an implicit zero -- the person
            # must type "0" to mean zero.
            errors.append(
                f'"{label}": enter the balance the day before your start '
                'date (type 0 if it was zero).')
            continue
        try:
            balances_cents[account_id] = _opening_balance_to_cents(
                raw_balance, f'"{label}" balance')
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        return HTMLResponse(render_simplefin_cutover(error=" ".join(errors)))

    # ---- Every check passed -- persist the DRAFT only. No
    # journal_entries / journal_lines write, no banks.ACTIVE_BANK_KEY
    # write: Task 6's confirm screen is the only place that posts
    # anything.
    db.set_setting(SIMPLEFIN_PENDING_CUTOVER_KEY, json.dumps({
        "start_date": start_date.isoformat(),
        "balances_cents": balances_cents,
    }))
    _simplefin_advance_stage(SIMPLEFIN_STAGE_CONFIRM)
    return RedirectResponse("/setup/simplefin/confirm", status_code=303)


# ------------------------------------------- step 3f: SimpleFIN confirm/finish
#
# Screen 5 of the dedicated /setup/simplefin flow, and the ONLY place in this
# whole flow that posts real journal entries and activates SimpleFIN as the
# live bank (banks.ACTIVE_BANK_KEY). Everything in Screens 1-4 (token
# exchange, account mapping, cutover date + opening-balance draft) exists to
# feed this screen safely -- see _validated_confirm_context() below for the
# two independent re-checks every GET and every POST here run before
# trusting the draft Screen 4 left behind.

OWNER_EQUITY_ACCOUNT_CODE = "3000"
# The offset side of every opening-balance entry this screen posts. This
# was an assumption carried through the whole SimpleFIN feature plan and
# never explicitly re-confirmed with a real accountant/owner sign-off --
# implemented here because it is the obviously-correct default (an
# opening balance is owner's equity by definition, the same account a
# brand-new Chart of Accounts already seeds at code 3000), but flagged
# prominently in this task's own report for a second look.

SIMPLEFIN_START_DATE_KEY = "simplefin_start_date"
# A permanent, display-oriented record of when SimpleFIN's history
# begins -- written once, at the end of a successful finish, alongside
# (but distinct from) the bank_last_checked.simplefin sync CURSOR below.
# A future Connections-page card reads this exact key to render
# "history since [date]" -- a named constant here (instead of a bare
# string literal repeated at the read site) is what keeps that future
# read from silently drifting out of sync with this write.

SIMPLEFIN_RESOLVED_ACCOUNTS_KEY = "simplefin_resolved_accounts"
# Every SimpleFIN account_id that had a real opening-balance DECISION
# made for it at the most recent successful finish -- whether that
# decision was "post a real entry" (non-zero balance) or "correctly
# post nothing" (a genuine $0 balance -- see the $0 skip in the finish
# handler below; ledger._validate_journal_entry would reject a $0 line
# anyway). Written ONCE, at the end of a successful finish, as a JSON
# list of account_id strings -- exactly that finish's
# draft["balances_cents"].keys(), which _validated_confirm_context()
# already guarantees equals the live mapped-account set AT THAT MOMENT.
#
# This is what lets _already_done_banner() below tell apart two very
# different reasons a currently-mapped account might have no
# ledger_source_imports row:
#   - it IS in this set: its balance was genuinely $0 at finish time,
#     and skipping it was the correct, deliberate outcome. Nothing to
#     warn about.
#   - it is NOT in this set: no opening-balance decision was EVER made
#     for it. This happens because _require_simplefin_stage is a >=
#     check, so /setup/simplefin/accounts (the mapping screen) stays
#     reachable even after the stage reaches "done" -- intentionally,
#     as the future "edit mapping" re-entry point -- but Screens 4/5
#     themselves are permanently short-circuited past "done" in THIS
#     task. A brand-new SimpleFIN account mapped after that point has
#     no UI path left here to ever get an opening balance. Treating it
#     the same as a deliberate $0 would silently hide a real, permanent
#     gap -- exactly what this whole feature exists to prevent.


def _validated_confirm_context():
    """Full re-validation for Screen 5 -- called fresh by BOTH the GET
    render and the POST finish handler, so a forged/direct POST (one
    that never rendered the GET first) gets exactly the same protection
    the GET's own checks would have given it. Never trust that GET
    already ran; never trust anything the client submitted, since this
    screen's POST has no user-editable fields to submit anyway.

    Two independent checks, both against LIVE state at the moment of
    THIS call -- never a copy fetched earlier in the same request or
    left over from a previous one:
      1. _validated_cutover_draft() (Task 5) -- draft shape/date
         re-validation. It already re-checks the date is still within
         _cutover_date_bounds() at the moment of calling, handling
         day-rollover for free.
      2. The stale-account-set check -- THIS task's own job, which
         Task 5 deliberately did not build. The draft's
         balances_cents key set must match _mapped_simplefin_accounts()'s
         LIVE account_id set EXACTLY. An account mapped, excluded, or
         remapped on Screen 3 after this draft was written must never
         be posted with a now-stale balance, and a newly-mapped account
         must never silently default to a $0 opening balance.

    Returns (draft, mapped_rows) on success -- draft is the validated
    dict from _validated_cutover_draft() (a real datetime.date, genuine
    non-negative int cents), mapped_rows is the SAME
    _mapped_simplefin_accounts() call just used for the comparison, so
    a caller never has to re-fetch it and risk it drifting from what
    was actually checked here.

    Returns (None, message) on failure, where message is the
    human-readable banner to show after the 303 redirect back to
    Screen 4.
    """
    draft = _validated_cutover_draft()
    if draft is None:
        return None, (
            "Your opening-balance draft could not be read -- confirm "
            "your cutover date and balances again.")
    mapped_rows = _mapped_simplefin_accounts()
    live_ids = {row["account_id"] for row in mapped_rows}
    draft_ids = set(draft["balances_cents"].keys())
    if live_ids != draft_ids:
        return None, (
            "Your account mapping changed since you entered opening "
            "balances -- please confirm them again.")
    return (draft, mapped_rows), None


def _read_resolved_accounts():
    """The set of account_ids SIMPLEFIN_RESOLVED_ACCOUNTS_KEY names, or
    an EMPTY set if unset or corrupt. Fails toward OVER-warning, not
    under-warning: an empty set makes _already_done_banner() below
    treat every currently-unposted mapped account as "missing", which
    is the safe direction to be wrong in on a money screen -- a false
    "you should check this" is far cheaper than a silently missing
    opening balance nobody was ever told about (e.g. an installation
    that reached "done" before this setting existed at all)."""
    raw = db.get_setting(SIMPLEFIN_RESOLVED_ACCOUNTS_KEY)
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {value for value in parsed if isinstance(value, str)}


def simplefin_resolution_summary():
    """Structured, per-mapped-account resolution classification -- the
    computation _already_done_banner() below used to do inline, pulled
    out so a non-prose caller (the Connections-page card) can tell the
    outcomes apart itself instead of parsing banner text. Public (no
    leading underscore) on purpose -- this exists specifically as a
    cross-module API for app.py's Connections card, not as an internal
    helper.

    Checked against the LIVE _mapped_simplefin_accounts() set, not a
    cached copy, using the same durable records the banner always read:
      - db.ledger_source_import_exists() -- did this account actually
        get a posted opening-balance entry?
      - db.source_import_account_codes() -- which Ledger account CODE
        did that posted entry actually hit? A posted entry is not the
        same thing as "correctly resolved" -- see "remapped" below,
        the reviewer-found gap this check exists to catch.
      - _read_resolved_accounts() -- was ANY decision (post, or a
        deliberate $0) ever made for this account at all?

    Returns {"posted": [...], "zero_resolved": [...], "missing": [...],
    "remapped": [...]}, each a list of rows from
    _mapped_simplefin_accounts() (same dicts, same order -- at least
    "account_id" and "name"):
      - "posted": has a real posted opening-balance entry, AND that
        entry's Ledger account CODE still matches the account's
        CURRENT mapping. The only bucket that is genuinely fine.
      - "zero_resolved": resolved but unposted -- a genuine, deliberate
        $0 balance at finish time. Correct and silent, nothing to
        report.
      - "missing": NOT resolved at all -- mapped (see the mapping
        screen's own >= stage gate, SIMPLEFIN_RESOLVED_ACCOUNTS_KEY's
        own comment above) after the stage already reached "done", so
        it has no opening balance on file and no UI path left in THIS
        task to ever get one. This has to be surfaced explicitly, not
        folded silently into "posted", or its transactions would sync
        forward from a starting balance nobody was ever warned is
        missing.
      - "remapped": REVIEWER-FOUND GAP, fixed here. A real entry WAS
        posted, but the account's mapping was later changed (via the
        mapping screen's own "edit mapping" re-entry point) to a
        DIFFERENT Ledger account, and the posted entry was never moved
        with it. Checking ledger_source_import_exists() alone can never
        tell this apart from a genuinely-fine "posted" account -- that
        check stays true forever once an entry exists, regardless of
        which account it hit. Left undetected, this is strictly worse
        than "missing": real money is silently sitting in the OLD
        account while every future synced transaction lands in the NEW
        one, with the UI reporting nothing wrong.
    """
    mapped_rows = _mapped_simplefin_accounts()
    resolved_ids = _read_resolved_accounts()
    accounts_by_id = {account["id"]: account for account in ledger.list_accounts()}
    posted = []
    zero_resolved = []
    missing = []
    remapped = []
    for row in mapped_rows:
        account_id = row["account_id"]
        source_reference = f"simplefin:opening:{account_id}"
        if db.ledger_source_import_exists(source_reference):
            posted_codes = db.source_import_account_codes(source_reference)
            current_account = accounts_by_id.get(row.get("ledger_account_id"))
            current_code = current_account["code"] if current_account else None
            if current_code is not None and current_code in posted_codes:
                posted.append(row)
            else:
                # The entry exists, but not against the account this
                # SimpleFIN account is CURRENTLY mapped to -- either the
                # mapping changed after finish, or the current mapping's
                # Ledger account itself no longer exists. Either way,
                # this is not "fine" and must not be silently counted as
                # "posted".
                remapped.append(row)
        elif account_id in resolved_ids:
            zero_resolved.append(row)
        else:
            missing.append(row)
    return {"posted": posted, "zero_resolved": zero_resolved,
            "missing": missing, "remapped": remapped}


def _already_done_banner():
    """Honest summary for a request that reaches Screen 5 AFTER finish
    already completed successfully once -- the double-click/retry case
    where the first attempt already cleared the pending-cutover draft
    and advanced the stage to "done". There is deliberately no draft
    left to re-read at that point (see the finish handler's own
    "clear the draft" step), so this formats
    simplefin_resolution_summary() above -- a pure prose wrapper, not a
    second computation -- into the honest sentence a person reads on
    Screen 5.

    A mapped account that is resolved but unposted was a genuine $0
    balance at finish time -- correct, and silent. A mapped account
    that is NOT resolved at all was mapped (see the mapping screen's
    own >= stage gate, SIMPLEFIN_RESOLVED_ACCOUNTS_KEY's own comment
    above) after the stage already reached "done", so it has no
    opening balance on file and no UI path left in THIS task to ever
    get one -- that has to be said explicitly, not folded silently
    into the same "already had one" count, or its transactions would
    sync forward from a starting balance nobody was ever warned is
    missing. A "remapped" account (a real entry posted, but the
    mapping was later changed to a different Ledger account) is a
    THIRD, distinct case, and strictly worse than "missing" -- money is
    silently sitting in the old account -- so it gets its own sentence
    rather than being folded into either of the other two.
    """
    summary = simplefin_resolution_summary()
    posted_count = len(summary["posted"])
    missing_names = [row.get("name") or row["account_id"]
                      for row in summary["missing"]]
    remapped_names = [row.get("name") or row["account_id"]
                       for row in summary["remapped"]]

    parts = ["SimpleFIN is already connected."]
    if posted_count:
        plural = "" if posted_count == 1 else "s"
        parts.append(
            f"{posted_count} account{plural} already has an opening "
            "balance posted from a previous attempt.")
    if missing_names:
        # States the observable fact only ("no opening balance is
        # recorded") rather than asserting WHY -- a specific causal
        # claim ("it was mapped after setup finished") is not actually
        # knowable when _read_resolved_accounts() came back empty
        # because the record itself is missing/corrupt (e.g. an
        # installation that reached "done" before this record existed
        # at all): a genuinely-$0 account from before this fix shipped
        # would land in this same branch, and it would be false to
        # claim it was necessarily mapped late.
        names = ", ".join(missing_names)
        if len(missing_names) == 1:
            parts.append(
                f"1 account ({names}) is mapped, but no opening balance "
                "is recorded for it, and there is no way to add one "
                "from this screen yet. It will sync new transactions "
                "from whatever the bank reports next, not from a "
                "recorded starting balance.")
        else:
            parts.append(
                f"{len(missing_names)} accounts ({names}) are mapped, "
                "but no opening balance is recorded for them, and "
                "there is no way to add one from this screen yet. They "
                "will sync new transactions from whatever the bank "
                "reports next, not from a recorded starting balance.")
    if remapped_names:
        # REVIEWER-FOUND GAP, fixed: a real opening-balance entry was
        # posted, but this account's mapping later changed to a
        # DIFFERENT Ledger account (via the mapping screen's "edit
        # mapping" re-entry point) and the posted entry was never moved
        # with it -- money is silently sitting in the OLD account while
        # new transactions sync into the NEW one. There is no way to
        # move the old entry from this screen; that remains a manual,
        # out-of-band fix.
        names = ", ".join(remapped_names)
        if len(remapped_names) == 1:
            parts.append(
                f"1 account ({names}) has an opening balance posted to "
                "a DIFFERENT Ledger account than the one it is "
                "currently mapped to -- its mapping changed after that "
                "balance posted, and the entry was never moved. There "
                "is no way to fix this from this screen yet.")
        else:
            parts.append(
                f"{len(remapped_names)} accounts ({names}) have an "
                "opening balance posted to a DIFFERENT Ledger account "
                "than the one each is currently mapped to -- their "
                "mapping changed after that balance posted, and the "
                "entries were never moved. There is no way to fix this "
                "from this screen yet.")
    return " ".join(parts)


def _simplefin_done_redirect(banner):
    """The redirect used by every "already done" short-circuit across
    the SimpleFIN flow (Screen 4 and Screen 5 alike), and by a
    successful finish itself: first-run setup and a later reconnect-
    from-Connections-page visit need DIFFERENT redirect targets, or
    this banner gets silently swallowed.

    is_complete() mirrors the exact check app.py's own gatekeeper
    middleware uses: while first-run setup is still in progress (the
    PRIMARY path through this whole screen -- a brand-new business
    connecting its bank for the first time), that middleware redirects
    EVERY non-"/setup" path -- including "/settings" -- straight back
    to "/setup", with the query string DROPPED. Redirecting to
    "/settings?tab=connections" during first-run setup would silently
    discard this banner (e.g. "Posted opening balances for 2 accounts.
    SimpleFIN is now your active bank.", on the one screen in the app
    that posts real money) and leave the wizard stuck showing step 3
    forever -- re-clicking Continue there loops right back into
    "/setup/simplefin".

    Mirrors the exact first-run-vs-reconnect branch the GA4 sub-wizard
    already uses for the same distinction (its own
    ``return_to == "connections"`` check): advance_to(4) plus a
    redirect to "/setup/step/4", exactly like every other bank path in
    this file (see setup_save_bank above), while first-run setup is
    still in progress; "/settings?tab=connections" once setup is
    already complete (a later reconnect visit, e.g. a future "edit
    mapping" link from the Connections page).
    """
    if not is_complete():
        advance_to(4)
        return RedirectResponse("/setup/step/4?msg=" + quote(banner),
                                status_code=303)
    return RedirectResponse(
        "/settings?tab=connections&msg=" + quote(banner), status_code=303)


def _finish_banner(newly_posted, already_posted, skipped_zero):
    """Compose an honest summary of what THIS finish call actually did --
    never a generic "Success!" that could mask a partial-completion
    retry. Distinguishes a fresh post (newly_posted) from an idempotent
    skip (already_posted -- posted by an earlier, interrupted attempt)
    from a genuine no-op (skipped_zero -- the account's opening balance
    was exactly $0, so no entry was ever needed for it)."""
    parts = []
    if newly_posted:
        plural = "" if newly_posted == 1 else "s"
        parts.append(
            f"Posted opening balances for {newly_posted} account{plural}.")
    if already_posted:
        plural = "" if already_posted == 1 else "s"
        parts.append(
            f"{already_posted} account{plural} already had an opening "
            "balance from a previous attempt.")
    if skipped_zero:
        plural = "" if skipped_zero == 1 else "s"
        parts.append(
            f"{skipped_zero} account{plural} started at $0, so nothing "
            "needed posting.")
    if not parts:
        parts.append("No opening balances were needed.")
    parts.append("SimpleFIN is now your active bank.")
    return " ".join(parts)


def render_simplefin_confirm(draft, mapped_rows, message=None, error=None):
    """Render Screen 5 -- the read-back -- for a GET, or for a POST
    /finish that failed AFTER _validated_confirm_context() already
    passed (e.g. a mapped account's Ledger account vanished between
    draft-creation and finish). Always built from the SAME validated
    (draft, mapped_rows) pair _validated_confirm_context() just
    produced, never from anything the client submitted -- there is
    nothing on this screen for a person to type, only one confirm
    button, so there is nothing to echo back either way.
    """
    adapter = banks.get_adapter("simplefin")
    start_date = draft["start_date"]
    opening_date = start_date - timedelta(days=1)
    balances = draft["balances_cents"]
    accounts_by_id = {account["id"]: account for account in ledger.list_accounts()}
    excluded_rows = [row for row in db.list_bank_accounts("simplefin")
                     if row.get("mapping_state") == "excluded"]

    row_html = ""
    for row in mapped_rows:
        account_id = row["account_id"]
        name = row.get("name") or account_id
        mask = row.get("mask")
        mask_html = f" ••••{escape(mask)}" if mask else ""
        cents = balances.get(account_id, 0)
        # ledger._cents_to_amount(), not an independent Decimal(cents)/100
        # expression -- the whole point of a read-back screen is "what
        # you see is what posts"; routing the display amount and the
        # POSTED amount (see setup_simplefin_finish below) through the
        # SAME conversion is what keeps that true if either is ever
        # touched separately.
        amount_html = escape(ui.money(ledger._cents_to_amount(cents)))
        ledger_account = accounts_by_id.get(row.get("ledger_account_id"))
        target_html = (
            f'{escape(ledger_account["code"])} — {escape(ledger_account["name"])}'
            if ledger_account else "(that Ledger account no longer exists)")
        row_html += f"""
        <div class="cred-field">
          <div class="q">{escape(name)}{mask_html}</div>
          <div class="hint">Opening balance {amount_html} &rarr; {target_html}</div>
        </div>"""

    excluded_html = ""
    if excluded_rows:
        items = "".join(
            f"<li>{escape(row.get('name') or row['account_id'])}</li>"
            for row in excluded_rows)
        excluded_html = f"""
        <div class="hint" style="margin-top:14px;"><strong>Not
          imported:</strong>
          <ul>{items}</ul>
        </div>"""

    # The offset side of every entry -- see OWNER_EQUITY_ACCOUNT_CODE's
    # own comment: never explicitly re-confirmed, but disclosed here
    # plainly so whoever needs to sign off on it being the right choice
    # sees it BEFORE clicking "Post and finish", not after. Looked up
    # (rather than hardcoded as "3000 Owner's Equity") so the disclosed
    # label can never drift from the account this screen will actually
    # post against, and so a missing account is caught here too, before
    # the button is even shown -- the POST handler already refuses
    # gracefully if this account vanishes between GET and POST, but a
    # person should find out before clicking, not only after.
    equity_account = ledger.account_by_code(OWNER_EQUITY_ACCOUNT_CODE)
    if equity_account is None:
        offset_note = (
            f"The offset account ({OWNER_EQUITY_ACCOUNT_CODE} Owner's "
            "Equity) is missing from your Chart of Accounts. Restore it "
            "before finishing -- Post and finish will fail until it "
            "exists.")
        offset_html = (f'<div class="callout error" '
                       f'style="margin-top:14px;">{escape(offset_note)}'
                       f'</div>')
    else:
        offset_label = f'{equity_account["code"]} {equity_account["name"]}'
        offset_html = (
            f'<div class="hint">The other side of each entry posts to '
            f'{escape(offset_label)}.</div>')

    finish_note = (
        "Klearing will post one opening-balance entry per account, dated "
        f"{opening_date.isoformat()}, and will never pull transactions "
        f"before {start_date.isoformat()}.")
    confirm_html = ui.confirm_attrs(
        finish_note, title="Post opening balances and finish?",
        confirm_label="Post and finish", busy_label="Posting…")

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Confirm and finish</h2>
        <span class="tag">required</span></div>
      <div class="hint">Connection: {escape(adapter.display_name)}
        (read-only).</div>
      <div class="hint">Start pulling transactions from
        {escape(start_date.isoformat())}.</div>
      <div class="hint" style="margin-bottom:14px;">Opening-balance date:
        {escape(opening_date.isoformat())} (the day before your start
        date).</div>
      {row_html}
      {offset_html}
      {excluded_html}
      <div class="callout info" style="margin-top:14px;">{escape(finish_note)}</div>
      <form method="post" action="/setup/simplefin/finish" {confirm_html}>
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Post opening balances
            and finish</button>
          <a class="skip-link" href="/setup/simplefin/cutover">&larr;
            Back and change</a>
        </div>
      </form>
    </div>"""
    return wizard_shell(3, content, message=message, error=error)


@router.get("/setup/simplefin/confirm", response_class=HTMLResponse)
def setup_simplefin_confirm_screen(msg: str = None, err: str = None):
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_CONFIRM)
    if redirect is not None:
        return redirect
    if simplefin_stage() == SIMPLEFIN_STAGE_DONE:
        return _simplefin_done_redirect(_already_done_banner())
    context, error = _validated_confirm_context()
    if context is None:
        return RedirectResponse(
            "/setup/simplefin/cutover?err=" + quote(error), status_code=303)
    draft, mapped_rows = context
    return HTMLResponse(render_simplefin_confirm(
        draft, mapped_rows, message=msg, error=err))


@router.post("/setup/simplefin/finish")
def setup_simplefin_finish():
    # No form to read -- like setup_simplefin_bank_connected above, this
    # is a plain confirm-click POST. Everything this screen needs was
    # already validated and persisted by Screen 4; there is nothing for
    # a person to type here.
    redirect = _require_simplefin_stage(SIMPLEFIN_STAGE_CONFIRM)
    if redirect is not None:
        return redirect
    if simplefin_stage() == SIMPLEFIN_STAGE_DONE:
        # Already finished by an earlier request -- most commonly a
        # double-click, or a retried submission whose first response
        # never reached the browser. The draft was deliberately cleared
        # on that earlier success (see the "clear the draft" step
        # below), so there is nothing left here to re-validate or
        # re-post; report the same honest outcome without touching
        # anything.
        return _simplefin_done_redirect(_already_done_banner())

    # Same gate, same re-validation as the GET handler -- a forged or
    # direct POST (one that skipped ever rendering the GET) must be
    # refused exactly the same way, never trusting that the GET's own
    # checks already ran.
    context, error = _validated_confirm_context()
    if context is None:
        return RedirectResponse(
            "/setup/simplefin/cutover?err=" + quote(error), status_code=303)
    draft, mapped_rows = context

    adapter = banks.get_adapter("simplefin")
    start_date = draft["start_date"]
    opening_date = start_date - timedelta(days=1)
    balances = draft["balances_cents"]

    # The offset account for every opening-balance entry -- see
    # OWNER_EQUITY_ACCOUNT_CODE's own comment above: this was an
    # assumption in the original plan, never explicitly re-confirmed,
    # implemented here as the obviously-correct default.
    equity_account = ledger.account_by_code(OWNER_EQUITY_ACCOUNT_CODE)
    if equity_account is None:
        return HTMLResponse(render_simplefin_confirm(
            draft, mapped_rows,
            error=f"The Owner's Equity account "
                 f"({OWNER_EQUITY_ACCOUNT_CODE}) is missing from your "
                 "Chart of Accounts -- restore it before finishing."))

    # ---- Determine what to post, per mapped account. Validated
    # up front, ALL of it, before a single spec is built -- one bad
    # account (e.g. its mapped Ledger account vanished between
    # draft-creation and finish) refuses the WHOLE request rather than
    # silently posting the rest and skipping the bad one, matching
    # Screen 3's own "validate everything before writing anything"
    # posture.
    accounts_by_id = {account["id"]: account for account in ledger.list_accounts()}
    specs = []
    zero_balance_count = 0
    for row in mapped_rows:
        account_id = row["account_id"]
        name = row.get("name") or account_id
        cents = balances.get(account_id, 0)
        if cents == 0:
            # ledger._validate_journal_entry requires each line to carry
            # exactly one POSITIVE debit or credit -- a $0 line has
            # neither and would be REJECTED, not silently accepted. A
            # genuinely $0 opening balance needs no entry at all: there
            # was no money to record, so this is a deliberate skip, not
            # an error.
            zero_balance_count += 1
            continue
        ledger_account = accounts_by_id.get(row.get("ledger_account_id"))
        if ledger_account is None or ledger_account["type"] not in (
                "asset", "liability"):
            return HTMLResponse(render_simplefin_confirm(
                draft, mapped_rows,
                error=f'"{name}": its mapped Ledger account is no '
                     "longer a valid asset or liability account -- go "
                     "back to Account Mapping, fix it, and return "
                     "here."))
        # ledger._cents_to_amount(), the SAME conversion the read-back
        # screen's display amount already routes through -- never an
        # independent Decimal(cents)/100 expression that could diverge
        # from what the user was shown if either is ever touched
        # separately.
        amount = ledger._cents_to_amount(cents)
        if ledger_account["type"] == "asset":
            # Asset: DEBIT the mapped account (increases its balance),
            # CREDIT the offset equity account.
            debit_id, credit_id = ledger_account["id"], equity_account["id"]
        else:
            # Liability: CREDIT the mapped account (increases what's
            # owed), DEBIT the offset equity account.
            debit_id, credit_id = equity_account["id"], ledger_account["id"]
        specs.append({
            "date_value": opening_date.isoformat(),
            "memo": f"SimpleFIN opening balance: {name}",
            "lines": [
                {"account_id": debit_id, "debit_amount": amount,
                 "credit_amount": 0},
                {"account_id": credit_id, "debit_amount": 0,
                 "credit_amount": amount},
            ],
            "source_reference": f"simplefin:opening:{account_id}",
            "created_by": "simplefin-setup",
        })

    # ---- Post all of it in ONE real database transaction -- NOT a loop
    # of independent create_sourced_journal_entry() calls, each opening
    # its own connection. If any spec fails validation for any reason,
    # every entry already saved earlier in THIS SAME call is rolled
    # back too, by SQLite's own transaction mechanics -- see
    # ledger.create_sourced_journal_entries()'s own docstring, which
    # mirrors Task 4's create_accounts()/_create_account_locked()
    # precedent for atomicity specifically. (The pre-check just above --
    # refusing the whole request before any spec is even built if one
    # mapped account's Ledger account is invalid -- is a SEPARATE, extra
    # safety net this function adds on top; it is not what makes this
    # batch call itself atomic.)
    try:
        results = ledger.create_sourced_journal_entries(specs)
    except ledger.LedgerValidationError as exc:
        return HTMLResponse(render_simplefin_confirm(
            draft, mapped_rows,
            error=f"Could not post opening balances: {exc}"))

    newly_posted = sum(1 for result in results if result is not None)
    already_posted = sum(1 for result in results if result is None)

    # ---- Only after the batch succeeded -- settings writes, in this
    # exact order. banks.ACTIVE_BANK_KEY is written LAST among them
    # deliberately: a failure anywhere earlier in this block must never
    # leave SimpleFIN half-activated as the live bank while opening
    # balances are missing or a previous bank's cash-account selection
    # is stale.
    db.set_setting(SIMPLEFIN_START_DATE_KEY, start_date.isoformat())
    # Every account that got a real opening-balance decision at THIS
    # finish -- posted or a deliberate $0 -- so _already_done_banner()
    # can later tell that apart from an account mapped after the fact
    # with no decision at all. See SIMPLEFIN_RESOLVED_ACCOUNTS_KEY's own
    # comment for why this distinction matters.
    db.set_setting(SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                   json.dumps(sorted(balances.keys())))
    # Seed the sync cursor via db.set_meta -- NOT db.set_setting.
    # check_bank.run_check() reads this via db.get_meta(), matching the
    # store here exactly (see check_bank.LAST_CHECKED_KEY_PREFIX and its
    # own last_checked[:10] slicing) -- writing to the wrong store would
    # leave the first real sync silently falling through to
    # since_date=None, which pulls the SimpleFIN adapter's full history
    # window regardless of what was chosen here.
    db.set_meta(check_bank.LAST_CHECKED_KEY_PREFIX + adapter.id,
               start_date.isoformat())

    # Same wipe-on-switch LOGIC as setup_save_bank's own precedent
    # (read the CURRENT active bank before overwriting it, wipe only if
    # it is genuinely changing -- a cash-account selection from a
    # different bank means nothing here), but deliberately NOT the same
    # ORDER: setup_save_bank sets ACTIVE_BANK_KEY first and wipes after,
    # which is fine there since nothing money-critical follows it. Here
    # the wipe (and everything else in this block) must happen BEFORE
    # banks.ACTIVE_BANK_KEY is written, per the "written LAST" rule
    # above.
    previous_bank = db.get_setting(banks.ACTIVE_BANK_KEY)
    if previous_bank and previous_bank != adapter.id:
        db.set_setting("cash_accounts", "[]")

    db.set_setting(banks.ACTIVE_BANK_KEY, adapter.id)

    # Clear the spent draft so a later re-entry into this flow (e.g. a
    # future "reconnect"/"edit mapping" trip from the Connections page)
    # starts from a clean slate rather than a stale one. delete_setting,
    # not set_setting(..., "") -- an actually-removed row, not an
    # empty-string one lingering in the table.
    db.delete_setting(SIMPLEFIN_PENDING_CUTOVER_KEY)
    _simplefin_advance_stage(SIMPLEFIN_STAGE_DONE)

    banner = _finish_banner(newly_posted, already_posted, zero_balance_count)
    return _simplefin_done_redirect(banner)


# ---------------------------------------------------------------- step 4: Klaviyo


def render_step_klaviyo(message=None, error=None):
    configured = klaviyo_client.is_configured()
    status_note = ""
    if configured:
        status_note = """
        <div class="callout info"><strong>Already connected</strong>
          Continue, or reconnect below.</div>"""

    fields = cred_input(
        "api_key", "Klaviyo private API key",
        'In Klaviyo: click your account name (bottom left) → Settings → '
        '<strong>API keys</strong> → Create Private API Key. Read-only '
        'access to Lists is enough. Starts with <span class="mono">pk_</span>.',
        placeholder="pk_…",
    )
    picker = """
    <div class="cred-field" id="klaviyo-list-wrap" style="display:none;">
      <label class="q" for="klaviyo-list">Which list holds your leads?</label>
      <div class="hint">Klearing watches this list for people who asked a
        question or requested a quote.</div>
      <select id="klaviyo-list" name="list_id"></select>
    </div>"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 4 — Connect Klaviyo</h2>
        <span class="tag">optional</span></div>
      <div class="hint" style="margin-bottom:14px;">
        <strong>What it unlocks:</strong> the Follow-ups tab. {APP_NAME}
        watches one Klaviyo list for new leads, checks your Gmail for
        replies, and drafts a follow-up email for anyone who hasn't heard
        back within 48 hours — you review and send each one by hand.
        Skip this if you don't use Klaviyo; everything else works
        without it.</div>
      {status_note}
      <form method="post" action="/setup/save/klaviyo">
        {test_block("klaviyo-test", "/setup/test/klaviyo", fields + picker,
                    gate_id="klaviyo-continue")}
        <div class="wizard-actions">
          <button class="save-btn" type="submit" id="klaviyo-continue"
                  {"" if configured else "disabled"}>Continue</button>
          <a class="skip-link" href="/setup/skip/klaviyo">Skip — I don't
            use Klaviyo &rarr;</a>
          {_back_link(4)}
        </div>
      </form>
    </div>"""

    extra_js = """
    wireTest('klaviyo-test');
    document.getElementById('klaviyo-test').addEventListener('test-passed', (e) => {
      const lists = e.detail.lists || [];
      const wrap = document.getElementById('klaviyo-list-wrap');
      const select = document.getElementById('klaviyo-list');
      select.innerHTML = '';
      for (const l of lists) {
        const option = document.createElement('option');
        option.value = l.id;
        option.textContent = l.name + ' (' + l.id + ')';
        select.appendChild(option);
      }
      wrap.style.display = lists.length ? '' : 'none';
    });
    """
    return wizard_shell(4, content, message=message, error=error,
                        extra_js=extra_js)


@router.post("/setup/test/klaviyo")
def setup_test_klaviyo(api_key: str = Form(""), live: str = Form("1"),
                       list_id: str = Form("")):
    key = (api_key or "").strip()
    error = klaviyo_client.validate_key_format(key)
    if error:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": error, "hint": None})
    if live != "1":
        return _format_ok_json()
    result = klaviyo_client.test_connection(key, list_id or None)
    return _result_json(result, {"lists": getattr(result, "lists", [])})


@router.post("/setup/save/klaviyo")
def setup_save_klaviyo(api_key: str = Form(""), list_id: str = Form("")):
    key = (api_key or "").strip()
    if not key and klaviyo_client.is_configured():
        advance_to(5)
        return RedirectResponse("/setup/step/5", status_code=303)
    error = klaviyo_client.validate_key_format(key)
    if error:
        return HTMLResponse(render_step_klaviyo(error=error))
    if not list_id.strip():
        return HTMLResponse(render_step_klaviyo(
            error="Test the connection, then pick which list to watch."))
    result = klaviyo_client.test_connection(key, list_id)
    if not result.ok:
        return HTMLResponse(render_step_klaviyo(error=result.message))
    secrets_store.set_secret(klaviyo_client.PRIVATE_KEY_SECRET, key)
    db.set_setting(klaviyo_client.LIST_ID_KEY, list_id.strip())
    advance_to(5)
    return RedirectResponse("/setup/step/5", status_code=303)


@router.get("/setup/skip/{what}")
def setup_skip(what: str):
    next_step = {"klaviyo": 5, "ga4": 6, "email": 7}.get(what)
    if next_step is None:
        return RedirectResponse("/setup", status_code=303)
    advance_to(next_step)
    return RedirectResponse(f"/setup/step/{next_step}", status_code=303)


# ---------------------------------------------------------------- step 5: GA4


def render_step_ga4(message=None, error=None):
    configured = ga4_client.is_configured()
    status = ("<div class='callout info'><strong>Already connected</strong> "
              "GA4 insights are on. Continue below.</div>") if configured else ""
    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 5 — Google Analytics (GA4)</h2>
        <span class="tag">optional &middot; advanced</span></div>
      <div class="hint" style="margin-bottom:14px;">
        <strong>What it unlocks:</strong> the Insights tab — your website's
        traffic and conversion numbers next to your money, refreshed once a
        day. It is genuinely optional, and it is the fiddliest connection
        here because Google requires a small amount of Google Cloud setup.
        Skipping now loses nothing: <strong>you can set it up later</strong>
        from the Connections page whenever you feel like it.</div>
      {status}
      <div class="callout"><strong>Before you start</strong>
        If you're using a Google Workspace email (a custom domain, not
        @gmail.com) and hit a permission error creating a project, try a
        personal Gmail account instead — Workspace accounts often restrict
        this by default.</div>
      <div class="wizard-actions">
        <a class="btn-link primary" href="/setup/ga4">Set up GA4 (guided,
          ~10 minutes)</a>
        <a class="skip-link" href="/setup/skip/ga4">Skip — set up later &rarr;</a>
        {_back_link(5)}
      </div>
    </div>"""
    return wizard_shell(5, content, message=message, error=error)


def render_ga4_subwizard(return_to="setup", message=None, error=None):
    """The dedicated GA4 walkthrough: numbered stages, each with a
    checkpoint so the user knows they're still on track."""
    back_url = "/setup/step/5" if return_to == "setup" else "/connections"
    checkpoints = f"""
    <div class="callout"><strong>Workspace warning (read first)</strong>
      If you're using a Google Workspace email (a custom domain, not
      @gmail.com) and hit a permission error creating a project, try a
      personal Gmail account instead — Workspace accounts often restrict
      this by default. The Gmail account only needs Viewer access to your
      Analytics property; it doesn't have to own your website.</div>

    <div class="callout info"><strong>Property ID vs Measurement ID —
      don't mix these up</strong>
      The <strong>Property ID</strong> is a plain number (like 544081549)
      and is what {APP_NAME} needs. The <strong>Measurement ID</strong>
      starts with G- and belongs to your website's tag — it will NOT work
      here. There's also a Stream ID, which you can ignore entirely.</div>

    <div class="checkpoint">
      <div class="cp-title">1. Open Google Cloud and pick a project</div>
      Go to <a href="https://console.cloud.google.com" target="_blank"
      rel="noopener">console.cloud.google.com</a>, sign in, and use the
      project picker in the top bar. Any existing project works; otherwise
      New Project (a name like "my-analytics" is fine — nothing here
      affects your website).
      <div class="cp-verify">Checkpoint: your project's name shows in the
        blue top bar.</div>
    </div>

    <div class="checkpoint">
      <div class="cp-title">2. Enable the Google Analytics <em>Data</em> API</div>
      Menu → APIs &amp; Services → Library → search
      "Google Analytics Data API" → Enable. Careful: you want the
      <strong>Data API</strong> (reads report numbers). The similarly named
      <strong>Admin API</strong> manages Analytics settings and is NOT the
      one — enabling only the Admin API is a classic dead end.
      <div class="cp-verify">Checkpoint: the API's page now shows a blue
        "Manage" button instead of "Enable".</div>
    </div>

    <div class="checkpoint">
      <div class="cp-title">3. Create a service account</div>
      Menu → IAM &amp; Admin → Service Accounts → Create service account.
      Name it anything (e.g. "klearing-reader"), click through — it needs
      <strong>no roles</strong> on the project. A service account is just a
      robot identity that {APP_NAME} will use to read your numbers.
      <div class="cp-verify">Checkpoint: the list shows your account with
        an email ending in <span class="mono">.iam.gserviceaccount.com</span>.
        Copy that email — you need it in stage 5.</div>
    </div>

    <div class="checkpoint">
      <div class="cp-title">4. Download a JSON key for it</div>
      Click the service account → Keys tab → Add key → Create new key →
      JSON → Create. A .json file downloads. That file is the credential
      you'll upload below — treat it like a password.
      <div class="cp-verify">Checkpoint: the file opens in a text editor
        and contains <span class="mono">"type": "service_account"</span>.</div>
    </div>

    <div class="checkpoint">
      <div class="cp-title">5. Give it access to your GA4 property</div>
      In <a href="https://analytics.google.com" target="_blank"
      rel="noopener">analytics.google.com</a>: Admin (gear, bottom left) →
      Property access management → "+" → Add users → paste the service
      account email from stage 3 → role <strong>Viewer</strong> → Add.
      <div class="cp-verify">Checkpoint: the service account email appears
        in the property's user list.</div>
      <div class="hint" style="margin-top:6px;">Two properties on your
        account? Make sure you're in the one actually receiving data:
        open Reports → Realtime in one tab, visit your website in another,
        and confirm your visit shows up — orphaned duplicate properties
        that receive nothing are a real (and maddening) trap.</div>
    </div>

    <div class="checkpoint">
      <div class="cp-title">6. Find your Property ID</div>
      Still in Admin → Property → Property details. The Property ID is at
      the top right — a plain number. (Not the G- Measurement ID; see the
      blue box above.)
      <div class="cp-verify">Checkpoint: you have a number, roughly 9-10
        digits, no letters.</div>
    </div>"""

    upload_form = f"""
    <form method="post" action="/setup/save/ga4" enctype="multipart/form-data">
      <input type="hidden" name="return_to" value="{escape(return_to)}">
      {test_block("ga4-test", "/setup/test/ga4", f'''
        <div class="cred-field">
          <label class="q" for="ga4-file">Service account key file (.json)</label>
          <div class="hint">The file from stage 4. It's stored encrypted on
            this computer only.</div>
          <input type="file" id="ga4-file" name="key_file" accept=".json,application/json">
        </div>
        <div class="cred-field">
          <label class="q" for="in-property_id">Property ID</label>
          <div class="hint">The plain number from stage 6 — not the G-
            Measurement ID.</div>
          <input type="text" id="in-property_id" name="property_id"
                 placeholder="e.g. 544081549" autocomplete="off"
                 spellcheck="false" style="max-width:240px;">
          {paste_note()}
        </div>''',
                  gate_id="ga4-continue")}
      <div class="cred-field">
        <label class="q" for="ga4-events">Conversion events to track
          (optional)</label>
        <div class="hint">Comma-separated GA4 event names you care about,
          e.g. <span class="mono">sign_up, purchase</span>. They show on
          the Insights tab week over week. Leave blank for traffic and top
          pages only; edit any time in Connections.</div>
        <input type="text" id="ga4-events" name="events"
               placeholder="sign_up, purchase" autocomplete="off">
      </div>
      <div class="wizard-actions">
        <button class="save-btn" type="submit" id="ga4-continue" disabled>
          Save GA4 connection</button>
        <a class="skip-link" href="{back_url}">&larr; Back (nothing saved)</a>
      </div>
    </form>"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>GA4 setup, step by step</h2>
        <span class="tag">optional &middot; advanced</span></div>
      <div class="hint">Six short stages in Google's consoles, then upload
        the key file below. Each stage ends with a checkpoint — if the
        checkpoint doesn't match what you see, stop and re-read that stage
        rather than pushing on.</div>
      {checkpoints}
      <h3 style="margin-top:26px;">Connect it</h3>
      {upload_form}
    </div>"""

    shell = (wizard_shell(5, content, message=message, error=error,
                          extra_js="wireTest('ga4-test');")
             if return_to == "setup" else
             ui.render_shell("Settings", "", content, message=message,
                             error=error, extra_js="wireTest('ga4-test');"))
    return shell


@router.get("/setup/ga4", response_class=HTMLResponse)
def setup_ga4_page(return_to: str = "setup", msg: str = None, err: str = None):
    return render_ga4_subwizard(return_to=return_to, message=msg, error=err)


@router.post("/setup/test/ga4")
async def setup_test_ga4(key_file: UploadFile = File(None),
                         property_id: str = Form(""), live: str = Form("1")):
    property_error = ga4_client.validate_property_id_format(property_id)
    file_bytes = await key_file.read() if key_file is not None else b""
    if not file_bytes:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": "Choose the .json key file from "
                                        "stage 4 first.", "hint": None})
    try:
        ga4_client.parse_service_account_json(file_bytes)
    except ga4_client.GA4ConfigError as exc:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": str(exc), "hint": None})
    if property_error:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": property_error, "hint": None})
    if live != "1":
        return _format_ok_json()
    result = ga4_client.test_connection(file_bytes, property_id)
    return _result_json(result)


@router.post("/setup/save/ga4")
async def setup_save_ga4(key_file: UploadFile = File(None),
                         property_id: str = Form(""),
                         events: str = Form(""),
                         return_to: str = Form("setup")):
    file_bytes = await key_file.read() if key_file is not None else b""
    if not file_bytes:
        return HTMLResponse(render_ga4_subwizard(
            return_to=return_to,
            error="Choose the .json key file, test the connection, then save."))
    result = ga4_client.test_connection(file_bytes, property_id)
    if not result.ok:
        return HTMLResponse(render_ga4_subwizard(return_to=return_to,
                                                 error=result.message))
    secrets_store.set_secret(ga4_client.SERVICE_ACCOUNT_SECRET, file_bytes,
                             is_file=True)
    ga4_client.reset_cache()
    db.set_setting(ga4_client.PROPERTY_ID_KEY,
                   property_id.strip().split("/")[-1])
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    db.set_setting(ga4_client.CONVERSION_EVENTS_KEY, json.dumps(event_list))

    if return_to == "connections":
        return RedirectResponse("/connections?msg=GA4 connected.",
                                status_code=303)
    advance_to(6)
    return RedirectResponse("/setup/step/6?msg=GA4 connected.",
                            status_code=303)


# ---------------------------------------------------------------- step 6: email


def render_step_email(message=None, error=None):
    configured = email_utils.is_configured()
    db.init_db()
    status_note = ""
    if configured:
        status_note = """
        <div class="callout info"><strong>Already connected</strong>
          Continue, or reconnect below.</div>"""

    fields = f"""
    <div class="cred-field">
      <label class="q" for="in-gmail_address">Gmail address</label>
      <div class="hint">The account notifications are sent from (and
        replies are checked in, for follow-ups).</div>
      <input type="email" id="in-gmail_address" name="gmail_address"
             placeholder="you@gmail.com" autocomplete="off" spellcheck="false"
             value="{escape(db.get_setting(email_utils.GMAIL_ADDRESS_KEY) or "")}">
    </div>
    {cred_input(
        "app_password", "Gmail app password",
        'Generate one at <a href="https://myaccount.google.com/apppasswords" '
        'target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>: '
        "sign in, type a name like Klearing, click Create, and copy the "
        "16-letter password it shows (it's shown exactly once). Requires "
        "2-Step Verification on the account — turn that on first if the "
        "page won't load. This is NOT your normal Gmail password.",
        placeholder="abcd efgh ijkl mnop",
    )}"""

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 6 — Email notifications</h2>
        <span class="tag">recommended</span></div>
      <div class="hint" style="margin-bottom:14px;">With email set up,
        {APP_NAME} sends you a note when new transactions arrive, a weekly
        digest, and low-cash warnings — and it's how drafted follow-ups and
        invoice reminders actually get sent. Without it, the review queues
        still work; nothing can be emailed.</div>
      {status_note}
      <form method="post" action="/setup/save/email">
        {test_block("email-test", "/setup/test/email", fields,
                    gate_id="email-continue")}
        <div class="cred-field">
          <label class="q" for="notify">Send notifications to (optional)</label>
          <div class="hint">Defaults to the Gmail address itself.</div>
          <input type="email" id="notify" name="notify_email"
                 placeholder="you@yourbusiness.com"
                 value="{escape(db.get_setting(email_utils.NOTIFY_EMAIL_KEY) or "")}">
        </div>
        <div class="cred-field">
          <label class="q" for="sender">Your name (optional)</label>
          <div class="hint">Used to sign drafted follow-ups and invoice
            reminders, e.g. "Best, Alex".</div>
          <input type="text" id="sender" name="sender_name"
                 value="{escape(db.get_setting(email_utils.SENDER_NAME_KEY) or "")}">
        </div>
        <div class="wizard-actions">
          <button class="save-btn" type="submit" id="email-continue"
                  {"" if configured else "disabled"}>Continue</button>
          <a class="skip-link" href="/setup/skip/email">Skip for now &rarr;</a>
          {_back_link(6)}
        </div>
      </form>
    </div>"""
    return wizard_shell(6, content, message=message, error=error,
                        extra_js="wireTest('email-test');")


@router.post("/setup/test/email")
def setup_test_email(gmail_address: str = Form(""),
                     app_password: str = Form(""), live: str = Form("1")):
    address = (gmail_address or "").strip()
    if not address or "@" not in address or "." not in address:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": "Enter the full Gmail address, "
                                        "like you@gmail.com.", "hint": None})
    password = GMAIL_PASSWORD_FIELD.clean(app_password)
    error = GMAIL_PASSWORD_FIELD.validate_format(app_password)
    if error:
        return JSONResponse({"ok": False, "stage": "format",
                             "message": error, "hint": None})
    if live != "1":
        return _format_ok_json()
    result = email_utils.test_connection(address, password)
    return _result_json(result)


@router.post("/setup/save/email")
def setup_save_email(gmail_address: str = Form(""),
                     app_password: str = Form(""),
                     notify_email: str = Form(""),
                     sender_name: str = Form("")):
    address = (gmail_address or "").strip()
    password = GMAIL_PASSWORD_FIELD.clean(app_password)
    if not address and not password and email_utils.is_configured():
        advance_to(7)
        return RedirectResponse("/setup/step/7", status_code=303)
    error = GMAIL_PASSWORD_FIELD.validate_format(app_password)
    if error:
        return HTMLResponse(render_step_email(error=error))
    result = email_utils.test_connection(address, password)
    if not result.ok:
        return HTMLResponse(render_step_email(error=result.message))
    db.set_setting(email_utils.GMAIL_ADDRESS_KEY, address)
    secrets_store.set_secret(email_utils.APP_PASSWORD_SECRET, password)
    db.set_setting(email_utils.NOTIFY_EMAIL_KEY, (notify_email or "").strip())
    if (sender_name or "").strip():
        db.set_setting(email_utils.SENDER_NAME_KEY, sender_name.strip())
    advance_to(7)
    return RedirectResponse("/setup/step/7", status_code=303)


# ---------------------------------------------------------------- step 7: done


def render_step_done(message=None, error=None):
    db.init_db()
    adapter = banks.active_adapter()
    rows = [
        ("App password", app_lock.is_configured(), "required"),
        ("Business", business_registry.active_business() is not None, "required"),
        (f"Bank ({adapter.display_name})" if adapter else "Bank",
         adapter is not None and adapter.is_configured(), "required"),
        ("Klaviyo", klaviyo_client.is_configured(), "optional"),
        ("GA4", ga4_client.is_configured(), "optional"),
        ("Email", email_utils.is_configured(), "recommended"),
    ]
    items = ""
    for label, on, kind in rows:
        state = ('<span class="tag ok">connected</span>' if on
                 else f'<span class="tag off">skipped ({kind})</span>')
        items += f'<li><span class="acct-name">{escape(label)}</span>{state}</li>'

    content = f"""
    <div class="section">
      <div class="section-head"><h2>Step 7 — You're set</h2></div>
      <ul class="accounts" style="margin-bottom:18px;">{items}</ul>
      <div class="hint" style="margin-bottom:8px;">Anything skipped can be
        connected later on the <strong>Connections</strong> page (under
        Settings). Two things worth doing next:</div>
      <ol class="setup-list">
        <li><strong>Turn on daily automation</strong> (Settings →
          Automation): checks the bank every morning at 8 and emails you
          when something needs review. Until then, use the "Check for new
          transactions" button.</li>
        <li><strong>Pick your business-cash accounts</strong> (Settings) if
          you want low-balance and runway warnings.</li>
      </ol>
      <form method="post" action="/setup/finish">
        <div class="wizard-actions">
          <button class="save-btn" type="submit">Open the dashboard</button>
          {_back_link(7)}
        </div>
      </form>
    </div>"""
    return wizard_shell(7, content, message=message, error=error)


@router.post("/setup/finish")
def setup_finish():
    db.init_db()
    if not app_lock.is_configured():
        return RedirectResponse("/setup?err=Set an app password before "
                                "finishing.", status_code=303)
    if business_registry.active_business() is None:
        return RedirectResponse("/setup?err=Name your business before "
                                "finishing.", status_code=303)
    adapter = banks.active_adapter()
    if adapter is None or not adapter.is_configured():
        return RedirectResponse("/setup?err=Connect a bank before finishing.",
                                status_code=303)
    db.set_setting(SETUP_COMPLETE_KEY, "1")
    return RedirectResponse("/?msg=Setup complete. Welcome to Klearing.",
                            status_code=303)
