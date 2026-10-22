"""Regression coverage for Screen 4 of the dedicated /setup/simplefin flow
(Task 5 of the SimpleFIN feature): GET/POST /setup/simplefin/cutover, the
date + opening-balance DRAFT collection screen.

This screen posts NOTHING to journal_entries/journal_lines and never
touches banks.ACTIVE_BANK_KEY -- it only persists a validated draft under
setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY for Task 6's confirm/finish
screen to read back and act on. This suite proves that boundary holds.

Covers:
- GET with zero mapped accounts shows the "go back and map something"
  message, not an empty form.
- GET with mapped accounts renders the date bound computed from the REAL
  banks.simplefin.MAX_HISTORY_DAYS constant (computed the same way in
  this test, not a hardcoded literal, so the test cannot silently drift
  from the real constant either).
- GET resumability: persist a draft, re-render GET, confirm the date and
  every balance field show the persisted values, not blank.
- POST with a date outside the allowed range (too old and in the
  future) -> rejected, nothing persisted, boundary stated correctly.
- POST with a missing balance for any one mapped account -> rejected,
  ZERO writes (full draft-setting + every mapped account row compared
  before/after).
- POST with an invalid balance (non-numeric, negative, more than two
  decimals) -> rejected, same zero-write proof.
- POST with a valid "0" balance -> accepted as exactly 0 cents (proven
  distinct from a missing/blank value, which is rejected).
- POST with all-valid input -> draft persisted with the exact structure
  (ISO date string, integer cents per account -- not floats or strings),
  stage advances to "confirm", 303 redirect fires.
- POST at the wrong stage (still "mapping") -> redirected away, nothing
  persisted.
- An EXCLUDED SimpleFIN account never appears on this screen's GET
  render or in its POST validation, alongside otherwise-mapped accounts.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-cutover-screen-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import banks  # noqa: E402
from banks import simplefin  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import secrets_store  # noqa: E402
import setup_wizard  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    print(("  PASS  " if condition else "  FAIL  ") + label)
    failures += 0 if condition else 1


class FakeRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def synced_at():
    return "2026-08-21T00:00:00Z"


def seed_mapped_account(account_id, name, mask, ledger_account):
    """Seed a bank_accounts row and give it a real Ledger mapping --
    exactly the state Screen 3 leaves behind for an account someone
    chose to map (not exclude)."""
    db.upsert_bank_account({
        "source": "simplefin", "account_id": account_id, "name": name,
        "official_name": None, "type": "", "subtype": "", "mask": mask,
        "synced_at": synced_at(),
    })
    ok = db.set_bank_ledger_account("simplefin", account_id, ledger_account["id"])
    assert ok, f"failed to seed mapping for {account_id}"


def seed_excluded_account(account_id, name):
    db.upsert_bank_account({
        "source": "simplefin", "account_id": account_id, "name": name,
        "official_name": None, "type": "", "subtype": "", "mask": None,
        "synced_at": synced_at(),
    })
    ok = db.exclude_bank_account("simplefin", account_id)
    assert ok, f"failed to seed exclusion for {account_id}"


def snapshot():
    """The full draft-setting raw string, the SimpleFIN setup stage, and
    every simplefin bank_accounts row, for a byte-identical before/after
    comparison. The stage is included deliberately, not just the draft
    and the accounts -- a validation failure must leave the stage
    untouched too (it is forward-only/irreversible, so a bug that
    advances it even on a rejected submission would let a user reach the
    next screen's gate with no real draft behind it). Without the stage
    in this tuple, that specific bug shape would slip past every
    "ZERO writes" check in this file."""
    return (
        db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY),
        db.get_setting(setup_wizard.SIMPLEFIN_STAGE_KEY),
        [dict(row) for row in db.list_bank_accounts("simplefin")],
    )


def balance_input_value(html, account_id):
    """The value="..." attribute of one account's balance <input>, or
    None if that input isn't on the page at all. The real template
    breaks the input's attributes across multiple lines (name="..." and
    value="..." are NOT adjacent text), so this uses a regex rather than
    a brittle concatenated substring match -- [^>]* matches newlines too
    since it is a negated character class, not ".", so it safely spans
    the line break between attributes without needing re.DOTALL."""
    match = re.search(
        r'name="balance__' + re.escape(account_id) + r'"[^>]*?value="([^"]*)"',
        html)
    return match.group(1) if match else None


def date_input_value(html):
    """The value="..." attribute of the start-date <input>, tolerant of
    the same multi-line attribute layout as balance_input_value above."""
    match = re.search(
        r'id="cutover-start-date"[^>]*?value="([^"]*)"', html)
    return match.group(1) if match else None


def expected_bounds():
    """Computed the same way setup_wizard._cutover_date_bounds() does,
    but independently derived here so this test cannot silently drift
    from the real constant if it ever changes -- both sides read
    banks.simplefin.MAX_HISTORY_DAYS directly, never a hardcoded '90'."""
    today = date.today()
    return today - timedelta(days=simplefin.MAX_HISTORY_DAYS - 1), today


def run():
    db.init_db()
    adapter = banks.get_adapter("simplefin")
    secrets_store.set_secret(adapter.secret_name("access_url"),
                             "https://user:pass@api.simplefin.test/simplefin")

    print("Wrong stage (still 'mapping'): GET and POST both redirect, "
          "nothing persisted")
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_MAPPING)
    check("stage really is 'mapping' before this block",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
    before_wrong_stage = snapshot()
    get_wrong_stage = setup_wizard.setup_simplefin_cutover_screen()
    check("GET at the wrong stage redirects (not a render)",
          isinstance(get_wrong_stage, RedirectResponse)
          and get_wrong_stage.status_code == 303)
    post_wrong_stage = asyncio.run(setup_wizard.setup_simplefin_cutover(
        FakeRequest({"start_date": date.today().isoformat()})))
    check("POST at the wrong stage redirects too",
          isinstance(post_wrong_stage, RedirectResponse)
          and post_wrong_stage.status_code == 303)
    check("nothing persisted from either wrong-stage attempt",
          snapshot() == before_wrong_stage)

    # Gate open: pretend Screens 1-3 already ran successfully.
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CUTOVER)
    check("stage now at least 'cutover'",
          setup_wizard._simplefin_stage_index(setup_wizard.simplefin_stage())
          >= setup_wizard._simplefin_stage_index(
              setup_wizard.SIMPLEFIN_STAGE_CUTOVER))

    # ---------------------------------------------------------------
    print("GET with zero mapped accounts: 'go back and map something', "
          "not an empty form")
    check("no mapped accounts exist yet",
          setup_wizard._mapped_simplefin_accounts() == [])
    html_zero = setup_wizard.setup_simplefin_cutover_screen()
    check("the GET route renders a string (not a crash)",
          isinstance(html_zero, str))
    check("the 'go back and map something' message is shown",
          "No accounts were mapped yet" in html_zero
          and "/setup/simplefin/accounts" in html_zero)
    check("no date input and no balance input are rendered for a form "
          "with nothing to fill in",
          'name="start_date"' not in html_zero
          and 'name="balance__' not in html_zero)

    # A forged POST at this stage, with zero accounts mapped, must also
    # refuse to persist anything rather than accepting a dateless/
    # accountless draft.
    before_zero_post = snapshot()
    resp_zero_post = asyncio.run(setup_wizard.setup_simplefin_cutover(
        FakeRequest({"start_date": date.today().isoformat()})))
    check("POST with zero mapped accounts also shows the 'go back' "
          "message rather than a redirect",
          isinstance(resp_zero_post, str)
          or (hasattr(resp_zero_post, "body")
              and not isinstance(resp_zero_post, RedirectResponse)))
    body_zero_post = (resp_zero_post if isinstance(resp_zero_post, str)
                      else resp_zero_post.body.decode("utf-8"))
    check("...and it says the same thing",
          "No accounts were mapped yet" in body_zero_post)
    check("...and persists nothing",
          snapshot() == before_zero_post)

    # ---------------------------------------------------------------
    print("Seed two mapped accounts and one EXCLUDED account, matching "
          "real Screen-3 output")
    checking_target = ledger.create_account(
        "Cutover Checking Target", "asset", parent_code="1000")
    savings_target = ledger.create_account(
        "Cutover Savings Target", "asset", parent_code="1000")
    seed_mapped_account("acc-checking", "Checking", "1234", checking_target)
    seed_mapped_account("acc-savings", "Savings", "5678", savings_target)
    seed_excluded_account("acc-excluded", "Excluded Account")

    mapped = setup_wizard._mapped_simplefin_accounts()
    check("exactly the two mapped accounts are returned, never the "
          "excluded one",
          {row["account_id"] for row in mapped}
          == {"acc-checking", "acc-savings"})

    # ---------------------------------------------------------------
    print("GET with mapped accounts: date bound derived from the real "
          "banks.simplefin.MAX_HISTORY_DAYS constant")
    earliest, latest = expected_bounds()
    html_form = setup_wizard.setup_simplefin_cutover_screen()
    check("the GET route renders a string",
          isinstance(html_form, str))
    check("the earliest allowed date (computed from the real constant) "
          "is stated in the help text",
          earliest.isoformat() in html_form)
    check(f"the day count ({simplefin.MAX_HISTORY_DAYS}) is stated too, "
          "not a hardcoded '90'",
          f"{simplefin.MAX_HISTORY_DAYS} days" in html_form)
    check("the date input carries the correct min/max attributes",
          f'min="{earliest.isoformat()}"' in html_form
          and f'max="{latest.isoformat()}"' in html_form)
    check("both mapped accounts get a balance input",
          'name="balance__acc-checking"' in html_form
          and 'name="balance__acc-savings"' in html_form)
    check("mapped account names/masks render",
          "Checking" in html_form and "1234" in html_form
          and "Savings" in html_form and "5678" in html_form)
    check("the EXCLUDED account never appears on this screen at all",
          "Excluded Account" not in html_form
          and 'name="balance__acc-excluded"' not in html_form)

    # ---------------------------------------------------------------
    print("POST: date too old -> rejected, nothing persisted, boundary "
          "stated correctly")
    before_too_old = snapshot()
    too_old = (earliest - timedelta(days=1)).isoformat()
    resp_too_old = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": too_old,
        "balance__acc-checking": "100.00",
        "balance__acc-savings": "200.00",
    })))
    check("too-old date is rejected (not a redirect)",
          not isinstance(resp_too_old, RedirectResponse))
    body_too_old = resp_too_old.body.decode("utf-8")
    check("the error names the real earliest boundary",
          earliest.isoformat() in body_too_old
          and latest.isoformat() in body_too_old)
    check("ZERO writes from the too-old-date attempt",
          snapshot() == before_too_old)

    print("POST: date in the future -> rejected, nothing persisted")
    before_future = snapshot()
    future = (latest + timedelta(days=1)).isoformat()
    resp_future = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": future,
        "balance__acc-checking": "100.00",
        "balance__acc-savings": "200.00",
    })))
    check("future date is rejected (not a redirect)",
          not isinstance(resp_future, RedirectResponse))
    body_future = resp_future.body.decode("utf-8")
    check("the error names the real boundary here too",
          earliest.isoformat() in body_future
          and latest.isoformat() in body_future)
    check("ZERO writes from the future-date attempt",
          snapshot() == before_future)

    # ---------------------------------------------------------------
    print("POST: missing balance for one mapped account -> rejected, "
          "ZERO writes")
    before_missing = snapshot()
    resp_missing = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": latest.isoformat(),
        "balance__acc-checking": "100.00",
        # acc-savings deliberately omitted entirely -- a forged/
        # incomplete POST.
    })))
    check("a missing balance re-renders with an error (not a redirect)",
          not isinstance(resp_missing, RedirectResponse))
    body_missing = resp_missing.body.decode("utf-8")
    check("the error names the account still needing a balance",
          "Savings" in body_missing
          and ("enter the balance" in body_missing))
    check("ZERO writes: the draft setting and every mapped bank_accounts "
          "row are byte-identical before/after",
          snapshot() == before_missing)

    print("POST: blank-string balance for one account (present field, "
          "empty value) -> rejected exactly like a missing one, ZERO "
          "writes")
    before_blank = snapshot()
    resp_blank = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": latest.isoformat(),
        "balance__acc-checking": "100.00",
        "balance__acc-savings": "   ",
    })))
    check("a blank-string balance is rejected too (not a redirect)",
          not isinstance(resp_blank, RedirectResponse))
    check("ZERO writes from the blank-string attempt",
          snapshot() == before_blank)

    # ---------------------------------------------------------------
    print("POST: invalid balances (non-numeric, negative, >2 decimals, "
          "extreme-exponent scientific notation) -> rejected cleanly "
          "(NOT a raw HTTP 500), ZERO writes each time")
    for label, bad_value in [
        ("non-numeric", "not-a-number"),
        ("negative", "-5.00"),
        ("too many decimals", "12.345"),
        # decimal.InvalidOperation reproduction: <input type="number">
        # accepts scientific notation, and Decimal("1e400") parses fine
        # (it's a syntactically valid Decimal) but blows the default
        # decimal context's precision at .quantize() time, raising
        # decimal.InvalidOperation -- ArithmeticError, NOT ValueError.
        # Before the fix, _opening_balance_to_cents only wrapped
        # Decimal(str(raw)) in try/except, leaving .quantize() outside
        # it, so this exact input propagated as an unhandled 500 instead
        # of a friendly per-account error banner.
        ("extreme positive exponent", "1e400"),
        ("extreme negative exponent", "-1e400"),
    ]:
        before_invalid = snapshot()
        # No try/except around this call: if the InvalidOperation bug
        # regresses, this line itself raises and the whole script dies
        # with a raw traceback -- exactly the "unhandled 500" failure
        # mode being guarded against, made maximally visible rather than
        # caught and silently downgraded to a FAIL line.
        resp_invalid = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
            "start_date": latest.isoformat(),
            "balance__acc-checking": bad_value,
            "balance__acc-savings": "50.00",
        })))
        check(f"{label} balance ({bad_value!r}) is rejected cleanly "
              "(a real error response, not a redirect and not a crash)",
              not isinstance(resp_invalid, RedirectResponse))
        check(f"{label} balance -> ZERO writes",
              snapshot() == before_invalid)

    # ---------------------------------------------------------------
    print("POST: balance ceiling (_MAX_OPENING_BALANCE_CENTS) -- just "
          "over the ceiling is rejected, just under it is accepted; "
          "guards against Screen 4 persisting a draft value Task 6's "
          "real ledger.create_sourced_journal_entry() cannot post at "
          "all (an oversized int hits SQLite's OverflowError, not "
          "ledger.LedgerValidationError)")
    ceiling_cents = setup_wizard._MAX_OPENING_BALANCE_CENTS
    just_over = str((Decimal(ceiling_cents) + 1) / 100)  # e.g. "...00.01" over
    just_under = str(Decimal(ceiling_cents) / 100)       # exactly at the ceiling

    before_over = snapshot()
    resp_over = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": latest.isoformat(),
        "balance__acc-checking": just_over,
        "balance__acc-savings": "50.00",
    })))
    check("a balance one cent over the ceiling is rejected (not a "
          "redirect)",
          not isinstance(resp_over, RedirectResponse))
    body_over = resp_over.body.decode("utf-8")
    check("the error names the actual ceiling ($1,000,000,000.00)",
          "1,000,000,000.00" in body_over)
    check("ZERO writes from the over-ceiling attempt",
          snapshot() == before_over)

    before_under = snapshot()
    resp_under = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": latest.isoformat(),
        "balance__acc-checking": just_under,
        "balance__acc-savings": "50.00",
    })))
    check("a balance exactly AT the ceiling is accepted (303 redirect)",
          isinstance(resp_under, RedirectResponse)
          and resp_under.status_code == 303)
    check("the write actually happened (sanity check on the snapshot "
          "helper)",
          snapshot() != before_under)
    stored_ceiling = json.loads(
        db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY))
    check("the exact ceiling value is stored as integer cents, no more "
          "and no less",
          stored_ceiling["balances_cents"]["acc-checking"] == ceiling_cents
          and isinstance(stored_ceiling["balances_cents"]["acc-checking"], int))

    # ---------------------------------------------------------------
    print('POST: a valid "0" balance is accepted as exactly 0 cents -- '
          "NOT treated the same as missing/blank")
    before_zero_balance = snapshot()
    resp_zero_balance = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": latest.isoformat(),
        "balance__acc-checking": "0",
        "balance__acc-savings": "0.00",
    })))
    check('an explicit "0"/"0.00" balance succeeds with a 303 redirect',
          isinstance(resp_zero_balance, RedirectResponse)
          and resp_zero_balance.status_code == 303)
    check("...and redirects to the confirm screen",
          resp_zero_balance.headers.get("location")
          == "/setup/simplefin/confirm")
    stored_zero = json.loads(
        db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY))
    check('both accounts stored as EXACTLY 0 (int), not skipped and not '
          "a float",
          stored_zero["balances_cents"]["acc-checking"] == 0
          and stored_zero["balances_cents"]["acc-savings"] == 0
          and isinstance(stored_zero["balances_cents"]["acc-checking"], int)
          and isinstance(stored_zero["balances_cents"]["acc-savings"], int))
    check("that write really changed the setting versus before (sanity "
          "check on the snapshot helper)",
          snapshot() != before_zero_balance)
    check("stage advanced to 'confirm' from this successful submission",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    # Reset the stage back to 'cutover' for the remaining tests below --
    # advance_stage is monotonic-forward-only, so re-visiting the route
    # itself must still work at 'confirm' or later per the "at least
    # this stage" gate semantics; this simulates the user coming BACK
    # to Screen 4 from Screen 5 to change something, which the task
    # spec explicitly calls out as a required resumability scenario.
    reachability_probe = setup_wizard.setup_simplefin_cutover_screen()
    check("Screen 4's own GET route is genuinely reachable (actually "
          "invoked here, not just index-compared) even after the stage "
          "has advanced past 'cutover' -- it renders the real form, not "
          "a redirect away",
          isinstance(reachability_probe, str)
          and not isinstance(reachability_probe, RedirectResponse)
          and 'name="start_date"' in reachability_probe)

    # ---------------------------------------------------------------
    print("GET resumability: the just-persisted draft (from the '0' "
          "balance submission above) pre-fills both fields on revisit")
    html_resume = setup_wizard.setup_simplefin_cutover_screen()
    check("GET after a persisted draft renders a string",
          isinstance(html_resume, str))
    check("the persisted start date pre-fills the date input",
          date_input_value(html_resume) == latest.isoformat())
    check("the persisted 0.00 balances pre-fill both balance inputs",
          balance_input_value(html_resume, "acc-checking") == "0.00"
          and balance_input_value(html_resume, "acc-savings") == "0.00")

    # ---------------------------------------------------------------
    print("POST: all-valid non-zero submission -> draft persisted with "
          "the EXACT structure (ISO date string, integer cents per "
          "account), stage advances, 303 fires")
    before_valid = snapshot()
    chosen_date = (latest - timedelta(days=3)).isoformat()
    resp_valid = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": chosen_date,
        "balance__acc-checking": "1500.50",
        "balance__acc-savings": "3000",
    })))
    check("a fully valid submission succeeds with a 303",
          isinstance(resp_valid, RedirectResponse)
          and resp_valid.status_code == 303)
    check("...and redirects to the confirm screen",
          resp_valid.headers.get("location") == "/setup/simplefin/confirm")
    check("the write actually changed the persisted setting",
          snapshot() != before_valid)

    raw_stored = db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY)
    check("the stored value is a JSON string (not None)",
          isinstance(raw_stored, str))
    stored = json.loads(raw_stored)
    check("top-level keys are exactly 'start_date' and 'balances_cents'",
          set(stored.keys()) == {"start_date", "balances_cents"})
    check("start_date is stored as the exact ISO string submitted",
          stored["start_date"] == chosen_date)
    check("balances_cents has exactly the two mapped account IDs as keys",
          set(stored["balances_cents"].keys())
          == {"acc-checking", "acc-savings"})
    check("acc-checking's balance is exactly 150050 integer cents (from "
          "$1500.50), and a genuine int (not a float or a string)",
          stored["balances_cents"]["acc-checking"] == 150050
          and isinstance(stored["balances_cents"]["acc-checking"], int)
          and not isinstance(stored["balances_cents"]["acc-checking"], bool))
    check("acc-savings' balance is exactly 300000 integer cents (from "
          "$3000, no decimal point in the input at all)",
          stored["balances_cents"]["acc-savings"] == 300000
          and isinstance(stored["balances_cents"]["acc-savings"], int))
    check("the raw JSON text contains no floating-point-looking cents "
          "value (a crude but direct proof against a stray float "
          "creeping into the persisted representation)",
          "150050.0" not in raw_stored and "300000.0" not in raw_stored)

    print("...and the exact Decimal reconstructed from those stored "
          "cents matches the dollar amounts submitted, proving the "
          "int-cents value is genuinely lossless")
    check("acc-checking reconstructs to exactly $1500.50",
          Decimal(stored["balances_cents"]["acc-checking"]) / 100
          == Decimal("1500.50"))
    check("acc-savings reconstructs to exactly $3000.00",
          Decimal(stored["balances_cents"]["acc-savings"]) / 100
          == Decimal("3000.00"))

    check("stage advanced to 'confirm' again",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    # ---------------------------------------------------------------
    print("GET resumability again: the NEW draft's real values (not the "
          "earlier '0' draft) now pre-fill the form")
    html_resume2 = setup_wizard.setup_simplefin_cutover_screen()
    check("the new persisted start date pre-fills the date input",
          date_input_value(html_resume2) == chosen_date)
    check("acc-checking's new balance ($1500.50) pre-fills correctly",
          balance_input_value(html_resume2, "acc-checking") == "1500.50")
    check("acc-savings' new balance ($3000.00, normalized to two "
          "decimals) pre-fills correctly",
          balance_input_value(html_resume2, "acc-savings") == "3000.00")

    # ---------------------------------------------------------------
    print("Corrupt/hand-edited draft setting: GET fails closed to blank "
          "fields rather than crashing")
    db.set_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY,
                   "not valid json at all {{{")
    html_corrupt = setup_wizard.setup_simplefin_cutover_screen()
    check("a corrupt draft setting still renders a string (no crash)",
          isinstance(html_corrupt, str))
    check("both balance fields fall back to blank (never a guessed "
          "value) when the draft cannot be parsed",
          balance_input_value(html_corrupt, "acc-checking") == ""
          and balance_input_value(html_corrupt, "acc-savings") == "")
    check("the date field falls back to blank too",
          date_input_value(html_corrupt) == "")

    # ---------------------------------------------------------------
    print("_validated_cutover_draft(): the VALIDATED reader Task 6 must "
          "use -- reproduces the reviewer's exact corruption scenarios "
          "(a string balance, a float, a bool, a negative number, and an "
          "unparseable/out-of-range date), each proven to invalidate the "
          "WHOLE draft (returns None), not just the one bad field. "
          "_read_pending_cutover_draft() (the raw reader) is proven to "
          "let every one of these through UNCHANGED first, to show "
          "exactly what a Task-6 author would get wrong by using it "
          "directly instead of the validated interface.")

    def set_raw_draft(start_date_value, balances_cents_value):
        db.set_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY,
                       json.dumps({"start_date": start_date_value,
                                   "balances_cents": balances_cents_value}))

    corrupt_cases = [
        ("string balance", latest.isoformat(), {"acc-checking": "1500"}),
        ("float balance", latest.isoformat(), {"acc-checking": 1500.7}),
        ("bool balance (True is an int subclass -- must be rejected "
         "explicitly, not silently accepted as 1)",
         latest.isoformat(), {"acc-checking": True}),
        ("negative balance", latest.isoformat(), {"acc-checking": -9999}),
        ("unparseable date", "banana", {"acc-checking": 100}),
        ("date before the earliest allowed boundary",
         (earliest - timedelta(days=1)).isoformat(), {"acc-checking": 100}),
        ("date after today", (latest + timedelta(days=1)).isoformat(),
         {"acc-checking": 100}),
        ("balance over the ceiling (hand-edited past what "
         "_opening_balance_to_cents would ever allow in)",
         latest.isoformat(),
         {"acc-checking": setup_wizard._MAX_OPENING_BALANCE_CENTS + 1}),
    ]
    for description, bad_start_date, bad_balances in corrupt_cases:
        set_raw_draft(bad_start_date, bad_balances)
        raw_read = setup_wizard._read_pending_cutover_draft()
        check(f"{description}: the RAW reader lets it through unchanged "
              "(proving it is genuinely unvalidated, not accidentally "
              "safe)",
              raw_read is not None
              and raw_read["start_date"] == bad_start_date
              and raw_read["balances_cents"] == bad_balances)
        validated_read = setup_wizard._validated_cutover_draft()
        check(f"{description}: the VALIDATED reader rejects the WHOLE "
              "draft (returns None)",
              validated_read is None)

    print("...and a genuinely clean draft still validates successfully "
          "through the same reader (proving the validator isn't just "
          "rejecting everything)")
    set_raw_draft(latest.isoformat(), {"acc-checking": 150000,
                                       "acc-savings": 0})
    clean_validated = setup_wizard._validated_cutover_draft()
    check("a clean draft validates to real types: a datetime.date for "
          "start_date, and genuine non-negative ints for every balance",
          clean_validated is not None
          and clean_validated["start_date"] == latest
          and isinstance(clean_validated["start_date"], date)
          and clean_validated["balances_cents"]
          == {"acc-checking": 150000, "acc-savings": 0}
          and all(isinstance(v, int) and not isinstance(v, bool)
                  for v in clean_validated["balances_cents"].values()))

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN cutover screen "
              "checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN cutover screen checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
