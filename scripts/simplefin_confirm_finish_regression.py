"""Regression coverage for Screen 5 of the dedicated /setup/simplefin flow
(Task 6 of the SimpleFIN feature): GET /setup/simplefin/confirm (the
read-back) and POST /setup/simplefin/finish -- the ONLY place in the whole
feature that posts real journal entries and activates SimpleFIN as the
live bank.

This is the highest-stakes screen in the feature: everything Screens 1-4
built exists to feed this one safely. This suite is written accordingly --
every check that touches money is proven against the real
journal_entries/journal_lines/ledger_source_imports tables, never assumed
from a status code alone.

Covers:
- GET renders the read-back correctly: start date, opening-balance date
  (day before), per-account amounts, mapped-to Ledger account, excluded
  accounts shown as "not imported" without being double-counted against
  the mapped list.
- GET/POST with a corrupt or day-rolled-past-validity draft -> redirected
  to Screen 4 with an explanatory banner, zero writes.
- GET/POST with a STALE account set (the live mapping changed after the
  draft was written -- an account excluded, or a brand-new one mapped) ->
  redirected to Screen 4, explicit banner, zero journal entries, zero
  settings changes.
- POST with valid everything: correct journal entries (right accounts,
  right debit/credit direction for BOTH an asset-type AND a
  liability-type mapped account in the SAME batch), correct dates,
  correct memos, simplefin_start_date setting, bank_last_checked.simplefin
  META (via db.get_meta, not db.get_setting) correct, cash_accounts wiped
  on a bank switch, ACTIVE_BANK_KEY set to "simplefin" only at the very
  end (proven via a mid-request failure that leaves it untouched), draft
  cleared, stage advances to "done", 303 redirect with the correct
  banner.
- A $0 mapped account: no journal entry posted for it, not treated as an
  error, other non-zero accounts in the same batch still post.
- Batch atomicity: TWO SEPARATE things are demonstrated here, not one
  claim proven two ways (the earlier docstring overstated this):
    1. Real transactional atomicity, proven directly against
       ledger.create_sourced_journal_entries() itself (the general-
       purpose primitive Task 6 built): a 2-spec batch where the second
       spec is invalid mid-batch -> ZERO entries committed from either
       spec. Mutation-tested against a deliberately non-atomic "loop of
       singular calls" replica, proving the test can actually tell the
       difference. THIS is the one that exercises SQLite's own
       commit/rollback.
    2. A SEPARATE safety net at the setup_wizard integration layer: a
       mapped account whose Ledger account vanished between draft-
       creation and finish (the task's own suggested scenario) is
       caught by PRE-VALIDATION before any spec is even built, refusing
       the whole finish request with zero entries posted for ANY
       account in that request. This proves the pre-check works, and
       proves ACTIVE_BANK_KEY is untouched by THAT specific failure --
       it does NOT exercise create_sourced_journal_entries()'s own
       transactional rollback, since the batch call is never reached at
       all in this scenario. A THIRD, separate test below (monkeypatching
       ledger.create_sourced_journal_entries to fail from INSIDE the
       batch call) is what actually proves ACTIVE_BANK_KEY is written
       last with respect to a batch-level failure specifically.
- Idempotent double-submit, both named scenarios:
    1. "Retry after an interrupted first attempt" -- some entries already
       posted (simulating a crash between the batch committing and the
       settings/draft/stage step), draft still present -> finish
       completes the rest, posts nothing twice, banner is honest.
    2. "Double-click after a fully completed first attempt" -- stage
       already "done", draft already cleared -> the short-circuit
       reports the same honest "already posted" outcome without
       touching anything again.
- Forged POST bypassing GET entirely (the POST handler invoked directly,
  never having rendered GET first) gets the exact same stale-draft/
  stale-account-set protections, proven the same way as the GET path.
- Reviewer-found gap, fixed: a brand-new SimpleFIN account mapped AFTER
  finish already reached "done" (reachable because the mapping screen's
  stage gate is a >= check, so it stays open past "done" on purpose)
  has no opening balance and no UI path in this task to ever get one.
  _already_done_banner() must name that account explicitly rather than
  silently folding it into "already posted" -- proven by reproducing
  the reviewer's exact scenario (finish for account A, then map a new
  account B directly), for both GET and POST. Also proven NOT to create
  a false positive for a mapped account that was genuinely $0 at finish
  time (correctly resolved, correctly silent), and proven to fail
  toward OVER-warning rather than under-warning when the new
  resolved-accounts record is missing/corrupt.
- Critical 1 fix (code-quality review): a successful finish, and both
  "already done" short-circuits, now redirect correctly for BOTH the
  first-run-setup case (/setup/step/4, with advance_to(4) actually
  called -- otherwise app.py's own gatekeeper middleware would
  intercept a /settings redirect, drop the query string, and strand
  the wizard on step 3 forever) and the later-reconnect case
  (/settings?tab=connections, once setup_wizard.is_complete() is
  true) -- proven for both branches, not just one.
- Critical 2 fix (code-quality review): Screen 4 (/setup/simplefin/
  cutover) now short-circuits at stage "done" too, exactly like
  Screen 5 already did. Reproduces the reviewer's exact scenario: map
  a new account after a normal finish, hit Screen 4's GET (must
  redirect, not render a real opening-balance form), hit Screen 4's
  POST directly with a genuine typed balance for the new account (must
  redirect, must NOT write anything to the draft) -- proving the
  former silent-data-loss dead end (a real balance orphaned forever in
  SIMPLEFIN_PENDING_CUTOVER_KEY, since Screen 5 would bounce it away
  without ever reading it) is now closed at the point of entry.
- Important 7 fix (code-quality review): a dedicated test proves
  ACTIVE_BANK_KEY is untouched by a failure that happens INSIDE the
  real ledger.create_sourced_journal_entries() batch call itself (via
  monkeypatching), not just by a failure caught during pre-validation
  before the batch is ever reached -- the earlier "vanish" scenario's
  proof was found to be unable to distinguish the two.
"""

import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-confirm-finish-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import banks  # noqa: E402
import check_bank  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402
import secrets_store  # noqa: E402
import setup_wizard  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402


class FakeRequest:
    """Minimal stand-in for a FastAPI Request -- just enough to drive
    Screen 4's async POST handler directly in a test, matching the
    Task 5 test suite's own FakeRequest convention exactly."""
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    print(("  PASS  " if condition else "  FAIL  ") + label)
    failures += 0 if condition else 1


def synced_at():
    return "2026-08-21T00:00:00Z"


def seed_mapped_account(account_id, name, mask, ledger_account):
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


def set_draft(start_date_value, balances_cents):
    """Persist a Screen-4-shaped draft directly (bypassing the real POST
    route, exactly like the Task 5 test suite's set_raw_draft helper) so
    each scenario below can control the exact cents values under test."""
    db.set_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY, json.dumps({
        "start_date": start_date_value,
        "balances_cents": balances_cents,
    }))


def full_snapshot():
    """Every piece of state Screen 5 must leave untouched on a rejected
    or short-circuited request: the draft, the stage, every simplefin
    bank_accounts row, every journal_entries/journal_lines/
    ledger_source_imports row, the four settings this screen writes on
    success (including the resolved-accounts record the reviewer's fix
    added), and the meta cursor check_bank.run_check() reads."""
    with db.get_connection() as conn:
        entries = [dict(row) for row in conn.execute(
            "SELECT * FROM journal_entries ORDER BY id").fetchall()]
        lines = [dict(row) for row in conn.execute(
            "SELECT * FROM journal_lines ORDER BY id").fetchall()]
        imports = [dict(row) for row in conn.execute(
            "SELECT * FROM ledger_source_imports "
            "ORDER BY source_reference").fetchall()]
    return (
        db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY),
        db.get_setting(setup_wizard.SIMPLEFIN_STAGE_KEY),
        [dict(row) for row in db.list_bank_accounts("simplefin")],
        entries, lines, imports,
        db.get_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY),
        db.get_setting("cash_accounts"),
        db.get_setting(banks.ACTIVE_BANK_KEY),
        db.get_meta(check_bank.LAST_CHECKED_KEY_PREFIX + "simplefin"),
        db.get_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY),
    )


def entries_for(source_reference):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT je.* FROM journal_entries je "
            "JOIN ledger_source_imports si ON si.journal_entry_id = je.id "
            "WHERE si.source_reference = ?", (source_reference,)
        ).fetchall()]


def lines_for_entry(entry_id):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM journal_lines WHERE journal_entry_id = ? "
            "ORDER BY id", (entry_id,)
        ).fetchall()]


def count_journal_entries():
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM journal_entries").fetchone()["n"]


def expected_bounds():
    from banks import simplefin
    today = date.today()
    return today - timedelta(days=simplefin.MAX_HISTORY_DAYS - 1), today


def fresh_data_dir():
    """Point KLEARING_DATA_DIR at a brand-new temp directory and
    re-init. Several scenarios below need the SimpleFIN setup stage to
    NOT already be "done" (the stage is monotonic-forward-only, so once
    a prior scenario in the same DB drives it to "done" it can never go
    back) -- paths.data_dir() reads the env var fresh on every call (no
    caching), so simply reassigning it and calling db.init_db() again is
    enough; no module reload is needed. Mirrors the same swap-mid-script
    pattern money_cents_regression.py already uses."""
    os.environ["KLEARING_DATA_DIR"] = tempfile.mkdtemp(
        prefix="klearing-simplefin-confirm-finish-")
    db.init_db()
    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        "https://user:pass@api.simplefin.test/simplefin")


def run():
    db.init_db()
    adapter = banks.get_adapter("simplefin")
    secrets_store.set_secret(adapter.secret_name("access_url"),
                             "https://user:pass@api.simplefin.test/simplefin")

    earliest, latest = expected_bounds()
    start_date = latest - timedelta(days=5)
    opening_date = start_date - timedelta(days=1)

    # ------------------------------------------------------------------
    print("Wrong stage (still 'cutover'): GET and POST both redirect, "
          "nothing persisted")
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CUTOVER)
    before_wrong_stage = full_snapshot()
    get_wrong_stage = setup_wizard.setup_simplefin_confirm_screen()
    check("GET at the wrong stage redirects (not a render)",
          isinstance(get_wrong_stage, RedirectResponse)
          and get_wrong_stage.status_code == 303)
    post_wrong_stage = setup_wizard.setup_simplefin_finish()
    check("POST at the wrong stage redirects too (forged POST, no GET "
          "ever rendered)",
          isinstance(post_wrong_stage, RedirectResponse)
          and post_wrong_stage.status_code == 303)
    check("nothing persisted from either wrong-stage attempt",
          full_snapshot() == before_wrong_stage)

    # ------------------------------------------------------------------
    print("Seed Chart-of-Accounts targets (one asset, one liability), two "
          "mapped SimpleFIN accounts, and one EXCLUDED account")
    checking_target = ledger.create_account(
        "Confirm Checking Target", "asset", parent_code="1000")
    card_target = ledger.create_account(
        "Confirm Credit Card Target", "liability", parent_code="2100")
    seed_mapped_account("acc-checking", "Checking", "1234", checking_target)
    seed_mapped_account("acc-card", "Credit Card", "9876", card_target)
    seed_excluded_account("acc-excluded", "Excluded Account")

    set_draft(start_date.isoformat(), {
        "acc-checking": 150000,   # $1500.00, asset
        "acc-card": 42599,        # $425.99, liability
    })
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    # ------------------------------------------------------------------
    print("GET renders the read-back correctly")
    html = setup_wizard.setup_simplefin_confirm_screen()
    check("GET returns an HTMLResponse (a render, not a redirect)",
          not isinstance(html, RedirectResponse))
    body = html.body.decode("utf-8") if hasattr(html, "body") else html
    check("the chosen start date is stated",
          start_date.isoformat() in body)
    check("the opening-balance date (day before start) is stated",
          opening_date.isoformat() in body)
    check("SimpleFIN Bridge / read-only is named",
          "SimpleFIN Bridge" in body and "read-only" in body)
    check("both mapped accounts' names and amounts render",
          "Checking" in body and "$1,500.00" in body
          and "Credit Card" in body and "$425.99" in body)
    check("each mapped account states which Ledger account it targets",
          checking_target["code"] in body and checking_target["name"] in body
          and card_target["code"] in body and card_target["name"] in body)
    check("the excluded account is listed as not imported",
          "Excluded Account" in body and "not" in body.lower()
          and "imported" in body.lower())
    check("the excluded account never appears as a mapped row (no "
          "amount/target line for it) -- not double-counted either way",
          "Excluded Account</li>" in body
          or ("Excluded Account" in body
              and "Excluded Account</div>" not in body))
    check("a confirm dialog is wired (no window.confirm()) and the "
          "finish form posts to the real route",
          "data-confirm=" in body
          and 'action="/setup/simplefin/finish"' in body)
    check("a back-and-change link returns to Screen 4",
          "/setup/simplefin/cutover" in body)

    # ------------------------------------------------------------------
    print("Corrupt draft: GET and POST both redirect to Screen 4 with an "
          "explanatory banner, zero writes")
    good_draft_raw = db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY)
    db.set_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY,
                   "not valid json {{{")
    before_corrupt = full_snapshot()
    get_corrupt = setup_wizard.setup_simplefin_confirm_screen()
    check("GET with a corrupt draft redirects to Screen 4",
          isinstance(get_corrupt, RedirectResponse)
          and get_corrupt.status_code == 303
          and get_corrupt.headers.get("location", "").startswith(
              "/setup/simplefin/cutover"))
    post_corrupt = setup_wizard.setup_simplefin_finish()
    check("POST (forged, no GET first) with a corrupt draft redirects "
          "the same way",
          isinstance(post_corrupt, RedirectResponse)
          and post_corrupt.status_code == 303
          and post_corrupt.headers.get("location", "").startswith(
              "/setup/simplefin/cutover"))
    check("zero writes from either corrupt-draft attempt",
          full_snapshot() == before_corrupt)
    check("zero journal entries exist yet at all",
          count_journal_entries() == 0)

    print("Day-rolled-past-validity draft (date now before the earliest "
          "allowed boundary): same redirect, zero writes -- proves "
          "_validated_cutover_draft()'s own re-check is what Screen 5 "
          "relies on, not a copy made at draft-write time")
    set_draft((earliest - timedelta(days=1)).isoformat(),
              {"acc-checking": 100, "acc-card": 100})
    before_rolled = full_snapshot()
    get_rolled = setup_wizard.setup_simplefin_confirm_screen()
    check("GET with a since-invalidated date redirects to Screen 4",
          isinstance(get_rolled, RedirectResponse)
          and get_rolled.status_code == 303)
    check("zero writes", full_snapshot() == before_rolled)

    # Restore the good draft for the next block.
    db.set_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY, good_draft_raw)

    # ------------------------------------------------------------------
    print("STALE account set: the live mapping changed after the draft "
          "was written -- exclude a previously-mapped account -> "
          "redirected to Screen 4, zero journal entries, zero settings "
          "changed")
    ok = db.exclude_bank_account("simplefin", "acc-card")
    assert ok
    before_stale = full_snapshot()
    get_stale = setup_wizard.setup_simplefin_confirm_screen()
    check("GET with a stale account set redirects to Screen 4",
          isinstance(get_stale, RedirectResponse)
          and get_stale.status_code == 303)
    check("the redirect banner explains the mapping changed",
          "changed" in get_stale.headers.get("location", "").lower())
    post_stale = setup_wizard.setup_simplefin_finish()
    check("POST (forged) with the same stale account set redirects too",
          isinstance(post_stale, RedirectResponse)
          and post_stale.status_code == 303)
    check("ZERO writes from either stale-set attempt -- no journal "
          "entries, no settings changes, draft/stage untouched",
          full_snapshot() == before_stale)
    check("zero journal entries exist yet at all",
          count_journal_entries() == 0)

    # Re-map acc-card and restore the two-account live set to match the
    # draft again for the remaining tests.
    ok = db.set_bank_ledger_account("simplefin", "acc-card", card_target["id"])
    assert ok
    live_ids = {row["account_id"]
               for row in setup_wizard._mapped_simplefin_accounts()}
    check("live mapped set is back to exactly the two accounts the "
          "draft expects",
          live_ids == {"acc-checking", "acc-card"})

    print("STALE account set, the other direction: a BRAND NEW account "
          "gets mapped after the draft was written -> also redirected, "
          "never silently defaulted to $0")
    savings_target = ledger.create_account(
        "Confirm Savings Target", "asset", parent_code="1000")
    seed_mapped_account("acc-savings", "Savings", "5555", savings_target)
    before_new = full_snapshot()
    get_new = setup_wizard.setup_simplefin_confirm_screen()
    check("GET with an unexpected NEW mapped account also redirects to "
          "Screen 4 (never silently posts $0 for it)",
          isinstance(get_new, RedirectResponse)
          and get_new.status_code == 303)
    check("zero writes", full_snapshot() == before_new)
    # Exclude the extra account again so the remaining tests see exactly
    # the original two-account draft/live state.
    ok = db.exclude_bank_account("simplefin", "acc-savings")
    assert ok

    # ------------------------------------------------------------------
    print("POST with valid everything: BOTH an asset-type AND a "
          "liability-type mapped account in the SAME batch, correct "
          "debit/credit direction proven directly from journal_lines")
    before_valid = full_snapshot()
    check("ACTIVE_BANK_KEY is unset before finishing (sanity check)",
          db.get_setting(banks.ACTIVE_BANK_KEY) is None)
    check("setup is NOT yet complete -- this is the PRIMARY, first-run "
          "path the reviewer's Critical 1 finding was about, not a "
          "later reconnect (that's tested separately below)",
          not setup_wizard.is_complete())
    resp = setup_wizard.setup_simplefin_finish()
    check("a fully valid finish succeeds with a 303",
          isinstance(resp, RedirectResponse) and resp.status_code == 303)
    check("REVIEWER FIX (Critical 1): during first-run setup, this "
          "redirects to /setup/step/4 -- NOT /settings?tab=connections, "
          "which app.py's own gatekeeper middleware would intercept and "
          "redirect to /setup with the query string (and this banner) "
          "silently DROPPED, stalling the wizard on step 3 forever",
          resp.headers.get("location", "").startswith("/setup/step/4"))
    check("the wizard actually advanced to step 4 (advance_to(4) was "
          "called), exactly like every other bank path in this file",
          setup_wizard.current_step() == 4)
    check("the banner honestly reports 2 newly-posted accounts",
          "Posted opening balances for 2 account" in
          urllib.parse.unquote(resp.headers.get("location", "")))

    checking_entries = entries_for("simplefin:opening:acc-checking")
    card_entries = entries_for("simplefin:opening:acc-card")
    check("exactly one journal entry exists for the asset account",
          len(checking_entries) == 1)
    check("exactly one journal entry exists for the liability account",
          len(card_entries) == 1)
    check("both entries are dated the opening-balance date (day before "
          "start), not the start date itself",
          checking_entries[0]["date"] == opening_date.isoformat()
          and card_entries[0]["date"] == opening_date.isoformat())
    check("both memos name the account clearly",
          "SimpleFIN opening balance: Checking" == checking_entries[0]["memo"]
          and "SimpleFIN opening balance: Credit Card" == card_entries[0]["memo"])

    checking_lines = lines_for_entry(checking_entries[0]["id"])
    card_lines = lines_for_entry(card_entries[0]["id"])
    equity_account = ledger.account_by_code("3000")

    def line_for(lines, account_id):
        matches = [line for line in lines if line["account_id"] == account_id]
        assert len(matches) == 1, f"expected exactly one line for {account_id}"
        return matches[0]

    checking_own_line = line_for(checking_lines, checking_target["id"])
    checking_equity_line = line_for(checking_lines, equity_account["id"])
    check("ASSET account: the mapped account is DEBITED $1500.00 "
          "(increases its balance)",
          checking_own_line["debit_amount"] == 150000
          and checking_own_line["credit_amount"] == 0)
    check("ASSET account: the offset (Owner's Equity, 3000) is CREDITED "
          "$1500.00",
          checking_equity_line["credit_amount"] == 150000
          and checking_equity_line["debit_amount"] == 0)

    card_own_line = line_for(card_lines, card_target["id"])
    card_equity_line = line_for(card_lines, equity_account["id"])
    check("LIABILITY account: the mapped account is CREDITED $425.99 "
          "(increases what's owed) -- the opposite direction from the "
          "asset case above, proven explicitly",
          card_own_line["credit_amount"] == 42599
          and card_own_line["debit_amount"] == 0)
    check("LIABILITY account: the offset (Owner's Equity) is DEBITED "
          "$425.99",
          card_equity_line["debit_amount"] == 42599
          and card_equity_line["credit_amount"] == 0)

    check("both entries individually balance (SQLite's own CHECK "
          "constraints plus ledger's own validation both agree)",
          sum(l["debit_amount"] for l in checking_lines)
          == sum(l["credit_amount"] for l in checking_lines)
          and sum(l["debit_amount"] for l in card_lines)
          == sum(l["credit_amount"] for l in card_lines))

    check("simplefin_start_date setting is the chosen ISO start date",
          db.get_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY)
          == start_date.isoformat())
    check("bank_last_checked.simplefin is written via db.get_meta "
          "(matching check_bank.run_check()'s own read), not "
          "db.get_setting -- and its value is the chosen start date",
          db.get_meta(check_bank.LAST_CHECKED_KEY_PREFIX + "simplefin")
          == start_date.isoformat()
          and db.get_setting(check_bank.LAST_CHECKED_KEY_PREFIX + "simplefin")
          is None)
    check("ACTIVE_BANK_KEY is now 'simplefin'",
          db.get_setting(banks.ACTIVE_BANK_KEY) == "simplefin")
    check("the pending draft is cleared",
          not db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY))
    check("stage advanced to 'done'",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_DONE)

    # Sanity check that run_check() would genuinely compute the chosen
    # start date as since_date from the seeded cursor -- proves the
    # exact slicing behavior the task called out, not just that SOME
    # value got written.
    seeded = db.get_meta(check_bank.LAST_CHECKED_KEY_PREFIX + "simplefin")
    check("run_check()'s own last_checked[:10] slice reproduces the "
          "exact chosen start date",
          seeded[:10] == start_date.isoformat())

    if failures:
        print(f"FAIL: {failures} of {checks} checks failed so far -- "
              "stopping before the destructive/atomicity scenarios "
              "below, which assume a clean baseline.")
        return 1

    # ==================================================================
    print("\n--- Fresh scenario: cash_accounts is wiped when switching "
          "from a DIFFERENT previous bank -- mirrors setup_save_bank's "
          "existing precedent exactly ---")
    fresh_data_dir()
    # Seed a prior, different active bank plus a non-empty cash_accounts
    # selection, exactly the state setup_save_bank's own wipe logic reads.
    db.set_setting(banks.ACTIVE_BANK_KEY, "mercury")
    db.set_setting("cash_accounts", '["mercury-acc-1", "mercury-acc-2"]')
    switch_target = ledger.create_account("Switch Test Asset", "asset",
                                          parent_code="1000")
    seed_mapped_account("acc-switch", "Switch Account", None, switch_target)
    switch_start = latest - timedelta(days=1)
    set_draft(switch_start.isoformat(), {"acc-switch": 10000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    resp_switch = setup_wizard.setup_simplefin_finish()
    check("finishing while switching FROM a different bank succeeds",
          isinstance(resp_switch, RedirectResponse) and resp_switch.status_code == 303)
    check("cash_accounts was wiped to '[]' because the active bank "
          "genuinely changed (mercury -> simplefin)",
          db.get_setting("cash_accounts") == "[]")
    check("ACTIVE_BANK_KEY is now simplefin",
          db.get_setting(banks.ACTIVE_BANK_KEY) == "simplefin")

    print("\n--- Fresh scenario: cash_accounts is NOT touched when "
          "SimpleFIN was ALREADY the active bank (no real switch) ---")
    fresh_data_dir()
    db.set_setting(banks.ACTIVE_BANK_KEY, "simplefin")
    db.set_setting("cash_accounts", '["simplefin-acc-already-there"]')
    same_target = ledger.create_account("Same Bank Test Asset", "asset",
                                        parent_code="1000")
    seed_mapped_account("acc-same", "Same Bank Account", None, same_target)
    same_start = latest - timedelta(days=1)
    set_draft(same_start.isoformat(), {"acc-same": 5000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)
    resp_same = setup_wizard.setup_simplefin_finish()
    check("finishing when SimpleFIN was already active succeeds",
          isinstance(resp_same, RedirectResponse) and resp_same.status_code == 303)
    check("cash_accounts is left exactly as it was -- no real bank "
          "switch happened, so nothing needed wiping",
          db.get_setting("cash_accounts") == '["simplefin-acc-already-there"]')

    # ==================================================================
    print("\n--- Fresh scenario: $0 opening balance is skipped, not an "
          "error, other accounts in the batch still post ---")
    fresh_data_dir()
    zero_target = ledger.create_account("Zero Checking", "asset",
                                        parent_code="1000")
    nonzero_target = ledger.create_account("Nonzero Savings", "asset",
                                           parent_code="1000")
    seed_mapped_account("acc-zero", "Zero Account", None, zero_target)
    seed_mapped_account("acc-nonzero", "Nonzero Account", None, nonzero_target)
    zero_start = latest - timedelta(days=2)
    set_draft(zero_start.isoformat(), {"acc-zero": 0, "acc-nonzero": 250000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    resp_zero = setup_wizard.setup_simplefin_finish()
    check("the finish succeeds even with a $0 account in the batch",
          isinstance(resp_zero, RedirectResponse) and resp_zero.status_code == 303)
    zero_entries = entries_for("simplefin:opening:acc-zero")
    nonzero_entries = entries_for("simplefin:opening:acc-nonzero")
    check("NO journal entry was created for the $0 account",
          zero_entries == [])
    check("the non-zero account in the SAME batch still posted correctly",
          len(nonzero_entries) == 1)
    nonzero_lines = lines_for_entry(nonzero_entries[0]["id"])
    check("the non-zero entry's amount is exactly right ($2500.00)",
          sum(l["debit_amount"] for l in nonzero_lines) == 250000)
    check("this is genuinely not treated as an error -- the redirect is "
          "a real 303 success, not an error re-render",
          isinstance(resp_zero, RedirectResponse))
    check("ACTIVE_BANK_KEY activated correctly even with a $0 account "
          "present",
          db.get_setting(banks.ACTIVE_BANK_KEY) == "simplefin")

    # ==================================================================
    print("\n--- ledger.create_sourced_journal_entries(): real batch "
          "atomicity, proven directly against the ledger primitive, "
          "mutation-tested against a non-atomic loop replica ---")
    plain_a = ledger.create_account("Atomicity Line A", "asset",
                                    parent_code="1000")
    plain_b = ledger.create_account("Atomicity Line B", "asset",
                                    parent_code="1000")
    good_spec = {
        "date_value": date.today().isoformat(),
        "memo": "Atomicity test: good spec",
        "lines": [
            {"account_id": plain_a["id"], "debit_amount": Decimal("10.00"),
             "credit_amount": 0},
            {"account_id": plain_b["id"], "debit_amount": 0,
             "credit_amount": Decimal("10.00")},
        ],
        "source_reference": "test:atomicity:good",
    }
    bad_spec = {
        "date_value": date.today().isoformat(),
        "memo": "Atomicity test: bad spec (unknown account)",
        "lines": [
            # 999999999 does not exist in the accounts table -- reaches
            # _save_journal_entry's own "unknown account(s)" check,
            # exactly the failure mode a concurrently-deleted Ledger
            # account would produce.
            {"account_id": 999999999, "debit_amount": Decimal("10.00"),
             "credit_amount": 0},
            {"account_id": plain_b["id"], "debit_amount": 0,
             "credit_amount": Decimal("10.00")},
        ],
        "source_reference": "test:atomicity:bad",
    }

    def buggy_loop_batch(specs):
        """Deliberately NOT atomic -- calls the SINGULAR function in a
        loop, each opening its OWN connection/transaction, exactly the
        bug shape create_accounts() was built to avoid for Chart-of-
        Accounts rows. Used ONLY to prove this test can tell the
        difference (the mutation test the task asks for) -- not part of
        production code."""
        results = []
        for spec in specs:
            results.append(ledger.create_sourced_journal_entry(
                spec["date_value"], spec["memo"], spec["lines"],
                spec["source_reference"],
                spec.get("created_by", "ledger-source-sync")))
        return results

    before_mutation = count_journal_entries()
    try:
        buggy_loop_batch([good_spec, bad_spec])
        check("mutation test: the buggy loop raised as expected", False)
    except ledger.LedgerValidationError:
        check("mutation test: the buggy loop raised on the bad spec "
              "(expected)", True)
    after_mutation = count_journal_entries()
    check("MUTATION TEST CATCHES THE BUG: the non-atomic loop replica "
          "left the GOOD spec committed even though the batch as a "
          "whole failed -- proving this test suite would catch a "
          "regression to a loop-based (non-transactional) "
          "implementation",
          after_mutation == before_mutation + 1
          and entries_for("test:atomicity:good") != [])

    # Clean up the mutation test's leaked entry so it cannot mask the
    # real implementation's own atomicity below.
    with db.get_connection() as conn:
        conn.execute("DELETE FROM ledger_source_imports "
                     "WHERE source_reference = 'test:atomicity:good'")
        conn.execute("DELETE FROM journal_lines WHERE journal_entry_id IN "
                     "(SELECT id FROM journal_entries WHERE "
                     "source_reference = 'test:atomicity:good')")
        conn.execute("DELETE FROM journal_entries WHERE "
                     "source_reference = 'test:atomicity:good'")

    before_real = count_journal_entries()
    try:
        ledger.create_sourced_journal_entries([good_spec, bad_spec])
        check("the REAL batch function raised as expected", False)
    except ledger.LedgerValidationError:
        check("the REAL batch function raised on the bad spec "
              "(expected)", True)
    after_real = count_journal_entries()
    check("REAL ATOMICITY CONFIRMED: with the real shared-transaction "
          "create_sourced_journal_entries(), the GOOD spec earlier in "
          "the SAME batch was ALSO rolled back -- zero net new journal "
          "entries from the failed batch call, unlike the buggy loop "
          "replica just proven above",
          after_real == before_real
          and entries_for("test:atomicity:good") == [])

    # ==================================================================
    print("\n--- Important 6 fix: an intra-batch DUPLICATE "
          "source_reference now raises loudly (a caller bug), while a "
          "reference already committed by an EARLIER, SEPARATE call "
          "still skips silently (the legitimate double-submit case) ---")
    dup_a = ledger.create_account("Dup Ref Line A", "asset",
                                  parent_code="1000")
    dup_b = ledger.create_account("Dup Ref Line B", "asset",
                                  parent_code="1000")

    def make_spec(reference, amount="5.00"):
        return {
            "date_value": date.today().isoformat(),
            "memo": f"Duplicate-reference test ({reference})",
            "lines": [
                {"account_id": dup_a["id"], "debit_amount": Decimal(amount),
                 "credit_amount": 0},
                {"account_id": dup_b["id"], "debit_amount": 0,
                 "credit_amount": Decimal(amount)},
            ],
            "source_reference": reference,
        }

    before_dup = count_journal_entries()
    try:
        ledger.create_sourced_journal_entries(
            [make_spec("test:dup:same-ref"), make_spec("test:dup:same-ref")])
        check("an intra-batch duplicate source_reference raises "
              "(expected)", False)
    except ledger.LedgerValidationError as exc:
        check("an intra-batch duplicate source_reference raises "
              "LedgerValidationError, loudly, as a caller bug",
              "duplicate source_reference" in str(exc))
    check("NOTHING was committed from that rejected batch -- checked "
          "up front, before any connection is even opened, so this is "
          "zero writes, not a rollback of a partial write",
          count_journal_entries() == before_dup
          and entries_for("test:dup:same-ref") == [])

    print("...and the legitimate case still works: a reference already "
          "COMMITTED by an earlier, separate call is still a silent, "
          "idempotent skip -- not confused with the caller-bug case "
          "above")
    first_call_results = ledger.create_sourced_journal_entries(
        [make_spec("test:dup:legit-retry")])
    check("the first, separate call posts a real entry",
          first_call_results[0] is not None
          and len(entries_for("test:dup:legit-retry")) == 1)
    second_call_results = ledger.create_sourced_journal_entries(
        [make_spec("test:dup:legit-retry"), make_spec("test:dup:brand-new")])
    check("a SECOND, separate call reusing that same reference "
          "alongside a genuinely new one does NOT raise -- it skips "
          "the already-committed one (None) and posts the new one",
          second_call_results[0] is None
          and second_call_results[1] is not None
          and len(entries_for("test:dup:legit-retry")) == 1
          and len(entries_for("test:dup:brand-new")) == 1)

    # ==================================================================
    print("\n--- setup_wizard integration: a mapped account's Ledger "
          "account vanishes between draft-creation and finish -- the "
          "WHOLE finish request refuses, zero entries for ANY account "
          "in that request, ACTIVE_BANK_KEY untouched ---")
    fresh_data_dir()  # this scenario drives the finish handler for real,
                      # so the stage must not already be "done"
    good_asset = ledger.create_account("Vanish Test Good Asset", "asset",
                                       parent_code="1000")
    doomed_asset = ledger.create_account("Vanish Test Doomed Asset", "asset",
                                         parent_code="1000")
    seed_mapped_account("acc-good", "Good Account", None, good_asset)
    seed_mapped_account("acc-doomed", "Doomed Account", None, doomed_asset)
    vanish_start = latest - timedelta(days=1)
    set_draft(vanish_start.isoformat(),
              {"acc-good": 50000, "acc-doomed": 75000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    # Simulate the doomed account's Ledger account vanishing (e.g. some
    # future maintenance/cleanup path) between draft-creation and
    # finish. There is no delete-account feature in the app today, so
    # this is done directly at the DB layer, exactly as the task's own
    # suggested reproduction describes.
    with db.get_connection() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (doomed_asset["id"],))

    before_vanish = full_snapshot()
    active_bank_before_vanish = db.get_setting(banks.ACTIVE_BANK_KEY)
    resp_vanish = setup_wizard.setup_simplefin_finish()
    check("the finish request is refused (not a 303 success)",
          not isinstance(resp_vanish, RedirectResponse))
    vanish_body = resp_vanish.body.decode("utf-8")
    check("the error names the doomed account",
          "Doomed Account" in vanish_body)
    check("ZERO journal entries were posted for EITHER account in this "
          "request -- including the otherwise-perfectly-valid "
          "'Good Account' -- proving the all-or-nothing pre-check, not "
          "just 'the bad one was skipped'",
          entries_for("simplefin:opening:acc-good") == []
          and entries_for("simplefin:opening:acc-doomed") == [])
    check("ZERO settings/meta/draft/stage changes from this failed "
          "attempt",
          full_snapshot() == before_vanish)
    check("ACTIVE_BANK_KEY is genuinely untouched by the failed attempt "
          "(proves it is written LAST, only after posting succeeds -- "
          "a real mid-request failure left it exactly as it was before)",
          db.get_setting(banks.ACTIVE_BANK_KEY) == active_bank_before_vanish)

    # Fix the mapping (map acc-doomed to a fresh, valid account) and
    # confirm a clean finish now succeeds, to prove the earlier failure
    # was genuinely about the deleted account, not a permanently broken
    # state.
    fixed_asset = ledger.create_account("Vanish Test Fixed Asset", "asset",
                                        parent_code="1000")
    ok = db.set_bank_ledger_account("simplefin", "acc-doomed", fixed_asset["id"])
    assert ok
    resp_fixed = setup_wizard.setup_simplefin_finish()
    check("after remapping the doomed account, finish succeeds cleanly",
          isinstance(resp_fixed, RedirectResponse) and resp_fixed.status_code == 303)
    check("both entries now exist",
          len(entries_for("simplefin:opening:acc-good")) == 1
          and len(entries_for("simplefin:opening:acc-doomed")) == 1)

    # ==================================================================
    print("\n--- Idempotent double-submit, scenario 1: retry after an "
          "INTERRUPTED first attempt (entries already posted, but the "
          "crash happened before the draft/stage/settings step) ---")
    fresh_data_dir()  # the interrupted-retry simulation below needs the
                      # stage to start below "done"
    retry_good = ledger.create_account("Retry Good Asset", "asset",
                                       parent_code="1000")
    retry_other = ledger.create_account("Retry Other Asset", "asset",
                                        parent_code="1000")
    seed_mapped_account("acc-retry-good", "Retry Good", None, retry_good)
    seed_mapped_account("acc-retry-other", "Retry Other", None, retry_other)
    retry_start = latest - timedelta(days=1)
    retry_opening = retry_start - timedelta(days=1)
    set_draft(retry_start.isoformat(),
              {"acc-retry-good": 10000, "acc-retry-other": 20000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    # Simulate the interrupted attempt: the batch step already committed
    # (a real ledger.create_sourced_journal_entries call, exactly as the
    # finish handler would have made), but the crash happened before the
    # settings/draft/stage step -- so those are all still at their
    # pre-finish values.
    equity = ledger.account_by_code("3000")
    ledger.create_sourced_journal_entries([{
        "date_value": retry_opening.isoformat(),
        "memo": "SimpleFIN opening balance: Retry Good",
        "lines": [
            {"account_id": retry_good["id"], "debit_amount": Decimal("100.00"),
             "credit_amount": 0},
            {"account_id": equity["id"], "debit_amount": 0,
             "credit_amount": Decimal("100.00")},
        ],
        "source_reference": "simplefin:opening:acc-retry-good",
        "created_by": "simplefin-setup",
    }])
    check("interrupted-attempt setup: one entry pre-exists, stage/draft "
          "still mid-flight (not yet 'done')",
          len(entries_for("simplefin:opening:acc-retry-good")) == 1
          and setup_wizard.simplefin_stage()
          != setup_wizard.SIMPLEFIN_STAGE_DONE)

    resp_retry = setup_wizard.setup_simplefin_finish()
    check("the retried finish completes successfully",
          isinstance(resp_retry, RedirectResponse) and resp_retry.status_code == 303)
    check("the already-posted account still has EXACTLY ONE entry (not "
          "two)",
          len(entries_for("simplefin:opening:acc-retry-good")) == 1)
    check("the other account, never posted before, now has exactly one "
          "entry",
          len(entries_for("simplefin:opening:acc-retry-other")) == 1)
    retry_banner = urllib.parse.unquote(
        resp_retry.headers.get("location", ""))
    check("the banner honestly distinguishes the 1 already-posted "
          "account from the 1 newly-posted one",
          "already had an opening balance" in retry_banner
          and "Posted opening balances for 1 account" in retry_banner)
    check("the retry completed the interrupted settings/draft/stage "
          "work",
          db.get_setting(banks.ACTIVE_BANK_KEY) == "simplefin"
          and not db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY)
          and setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_DONE)

    print("\n--- Idempotent double-submit, scenario 2: double-click AFTER "
          "a FULLY completed first attempt (stage already 'done', draft "
          "already cleared) ---")
    before_double_click = full_snapshot()
    resp_double = setup_wizard.setup_simplefin_finish()
    check("the second, redundant finish call still succeeds cleanly "
          "(short-circuits rather than erroring)",
          isinstance(resp_double, RedirectResponse) and resp_double.status_code == 303)
    check("setup is still not complete in this fresh DB (first-run), so "
          "this ALSO redirects to /setup/step/4, not /settings -- the "
          "same Critical-1 fix applies to the already-done short-"
          "circuit, not just the main success path",
          not setup_wizard.is_complete()
          and resp_double.headers.get("location", "").startswith(
              "/setup/step/4"))
    double_banner = urllib.parse.unquote(
        resp_double.headers.get("location", ""))
    check("the second submission's banner is honest about 'already "
          "connected'/'already posted' -- NOT a fresh 'Posted opening "
          "balances for N accounts' claim",
          "already" in double_banner.lower()
          and "Posted opening balances for" not in double_banner)
    check("NO additional journal entries were created by the double-"
          "click -- exactly one entry per account still",
          len(entries_for("simplefin:opening:acc-retry-good")) == 1
          and len(entries_for("simplefin:opening:acc-retry-other")) == 1)
    check("ACTIVE_BANK_KEY / cash_accounts / the cursor seed are "
          "unchanged (still correct) after the redundant double-click -- "
          "setting them again to the same values would have caused no "
          "harm, but here they were not even touched a second time",
          full_snapshot() == before_double_click)

    # GET after 'done' also short-circuits cleanly rather than trying to
    # re-validate a (deliberately) cleared draft.
    get_after_done = setup_wizard.setup_simplefin_confirm_screen()
    check("GET after 'done' redirects with an honest banner too, rather "
          "than a 'draft could not be read' error -- to /setup/step/4 "
          "here too, since setup still is not complete",
          isinstance(get_after_done, RedirectResponse)
          and get_after_done.status_code == 303
          and get_after_done.headers.get("location", "").startswith(
              "/setup/step/4"))

    # ==================================================================
    print("\n--- Critical 1 fix, the OTHER branch: once setup is ALREADY "
          "complete (a later reconnect visit, e.g. a future 'edit "
          "mapping' link from Connections -- not the first-run case "
          "tested everywhere else in this file), the redirect target "
          "must be /settings?tab=connections, matching the GA4 sub-"
          "wizard's own return_to == 'connections' branch ---")
    fresh_data_dir()
    db.set_setting(setup_wizard.SETUP_COMPLETE_KEY, "1")
    check("setup IS complete in this scenario (the reconnect case)",
          setup_wizard.is_complete())
    reconnect_target = ledger.create_account("Reconnect Asset", "asset",
                                             parent_code="1000")
    seed_mapped_account("acc-reconnect", "Reconnect Account", None,
                        reconnect_target)
    reconnect_start = latest - timedelta(days=1)
    set_draft(reconnect_start.isoformat(), {"acc-reconnect": 12345})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    step_before_reconnect_finish = setup_wizard.current_step()
    resp_reconnect = setup_wizard.setup_simplefin_finish()
    check("finish succeeds while setup is already complete",
          isinstance(resp_reconnect, RedirectResponse)
          and resp_reconnect.status_code == 303)
    check("...and THIS time redirects to /settings?tab=connections, not "
          "/setup/step/4 -- the reconnect branch",
          resp_reconnect.headers.get("location", "").startswith(
              "/settings?tab=connections"))
    check("the wizard step tracker was NOT touched (advance_to(4) has "
          "no reason to run once setup is already fully complete) -- "
          "unchanged from whatever it was before this call, not "
          "assumed to be any particular number",
          setup_wizard.current_step() == step_before_reconnect_finish)

    # The already-done short-circuit must ALSO redirect to Connections
    # once setup is complete, not just the main success path above.
    resp_reconnect_again = setup_wizard.setup_simplefin_finish()
    check("a second call (already 'done') also redirects to Connections "
          "in the reconnect case",
          isinstance(resp_reconnect_again, RedirectResponse)
          and resp_reconnect_again.status_code == 303
          and resp_reconnect_again.headers.get("location", "").startswith(
              "/settings?tab=connections"))
    get_reconnect_again = setup_wizard.setup_simplefin_confirm_screen()
    check("GET also redirects to Connections in the reconnect case",
          isinstance(get_reconnect_again, RedirectResponse)
          and get_reconnect_again.status_code == 303
          and get_reconnect_again.headers.get("location", "").startswith(
              "/settings?tab=connections"))

    # ==================================================================
    print("\n--- Reviewer-found gap, fixed: a NEW SimpleFIN account "
          "mapped AFTER finish already reached 'done' has no opening "
          "balance and no UI path to ever get one -- "
          "_already_done_banner() must say so explicitly, not fold it "
          "silently into 'already posted' ---")
    fresh_data_dir()
    gap_target_a = ledger.create_account("Gap Test Account A", "asset",
                                         parent_code="1000")
    seed_mapped_account("acc-gap-a", "Existing Account", None, gap_target_a)
    gap_start = latest - timedelta(days=1)
    set_draft(gap_start.isoformat(), {"acc-gap-a": 30000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)
    resp_gap_finish = setup_wizard.setup_simplefin_finish()
    check("normal finish for account A succeeds",
          isinstance(resp_gap_finish, RedirectResponse)
          and resp_gap_finish.status_code == 303)
    check("stage is 'done', account A has its entry",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_DONE
          and len(entries_for("simplefin:opening:acc-gap-a")) == 1)

    # Reproduce the reviewer's exact scenario: map a BRAND NEW SimpleFIN
    # account directly via db.set_bank_ledger_account -- exactly what
    # the mapping screen does -- WITHOUT touching the (already-spent)
    # draft or the (already-"done") stage. This is reachable in the real
    # app because _require_simplefin_stage is a >= check, so
    # /setup/simplefin/accounts stays open past "done" on purpose.
    gap_target_b = ledger.create_account("Gap Test Account B", "asset",
                                         parent_code="1000")
    db.upsert_bank_account({
        "source": "simplefin", "account_id": "acc-gap-b",
        "name": "New Unresolved Account", "official_name": None,
        "type": "", "subtype": "", "mask": None, "synced_at": synced_at(),
    })
    ok = db.set_bank_ledger_account("simplefin", "acc-gap-b", gap_target_b["id"])
    assert ok
    check("account B is now live-mapped, with NO opening-balance entry "
          "and NO resolved-account record -- exactly the reviewer's "
          "reproduction",
          any(row["account_id"] == "acc-gap-b"
              for row in setup_wizard._mapped_simplefin_accounts())
          and entries_for("simplefin:opening:acc-gap-b") == []
          and "acc-gap-b" not in setup_wizard._read_resolved_accounts())

    # Revisit Screen 5 -- both GET (short-circuits via the stage check)
    # and the banner-generating function directly.
    get_gap = setup_wizard.setup_simplefin_confirm_screen()
    check("GET still redirects to Connections (stage is 'done', no path "
          "back into Screens 4/5 in this task)",
          isinstance(get_gap, RedirectResponse) and get_gap.status_code == 303)
    gap_banner = urllib.parse.unquote(get_gap.headers.get("location", ""))
    check("the banner explicitly names 'New Unresolved Account' as "
          "mapped but missing an opening balance -- the exact gap the "
          "reviewer found, now surfaced rather than silently folded "
          "into 'already posted'",
          "New Unresolved Account" in gap_banner
          and "no opening balance" in gap_banner.lower())
    check("the banner ALSO still honestly reports account A as already "
          "posted, in the SAME message",
          "1 account" in gap_banner
          and "already" in gap_banner.lower()
          and "posted" in gap_banner.lower())
    check("the banner does NOT claim a fresh 'Posted opening balances' "
          "success, and does not silently omit the gap by only "
          "mentioning account A",
          "Posted opening balances for" not in gap_banner)

    # POST /finish again (double-click / forged) must show the exact
    # same honest gap, not silently succeed as if nothing were wrong.
    resp_gap_post = setup_wizard.setup_simplefin_finish()
    check("POST also redirects (short-circuit), never re-attempts "
          "posting for the unresolved account",
          isinstance(resp_gap_post, RedirectResponse)
          and resp_gap_post.status_code == 303)
    gap_post_banner = urllib.parse.unquote(
        resp_gap_post.headers.get("location", ""))
    check("POST's banner names the gap too, identically",
          "New Unresolved Account" in gap_post_banner
          and "no opening balance" in gap_post_banner.lower())
    check("account B still has ZERO journal entries -- the fix only "
          "changes what is REPORTED, it does not attempt to post "
          "anything for an account with no draft behind it",
          entries_for("simplefin:opening:acc-gap-b") == [])

    # ==================================================================
    print("\n--- Fix does not create a false positive: a mapped account "
          "that was GENUINELY $0 at finish time (correctly skipped, "
          "never posted) must NOT be reported as 'missing' ---")
    fresh_data_dir()
    fp_zero_target = ledger.create_account("False Positive Zero", "asset",
                                           parent_code="1000")
    fp_nonzero_target = ledger.create_account("False Positive Nonzero",
                                              "asset", parent_code="1000")
    seed_mapped_account("acc-fp-zero", "Zero Balance Account", None,
                        fp_zero_target)
    seed_mapped_account("acc-fp-nonzero", "Nonzero Balance Account", None,
                        fp_nonzero_target)
    fp_start = latest - timedelta(days=1)
    set_draft(fp_start.isoformat(),
              {"acc-fp-zero": 0, "acc-fp-nonzero": 40000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)
    resp_fp = setup_wizard.setup_simplefin_finish()
    check("finish succeeds with one genuine $0 account and one non-zero "
          "account",
          isinstance(resp_fp, RedirectResponse) and resp_fp.status_code == 303)
    check("the $0 account correctly has no journal entry, but IS in the "
          "resolved-accounts record (a decision WAS made for it -- just "
          "a $0 one)",
          entries_for("simplefin:opening:acc-fp-zero") == []
          and "acc-fp-zero" in setup_wizard._read_resolved_accounts())

    # Now revisit Screen 5 after 'done' -- the $0 account must NOT be
    # reported as a gap, only the genuinely-posted one should be
    # mentioned as "already posted".
    get_fp = setup_wizard.setup_simplefin_confirm_screen()
    check("GET redirects (stage is 'done')",
          isinstance(get_fp, RedirectResponse) and get_fp.status_code == 303)
    fp_banner = urllib.parse.unquote(get_fp.headers.get("location", ""))
    check("the genuinely-$0 account is NEVER named as missing/no "
          "opening balance -- it was correctly resolved, just to zero",
          "Zero Balance Account" not in fp_banner
          and "no opening balance" not in fp_banner.lower())
    check("the non-zero account IS reported as already posted",
          "1 account" in fp_banner and "already" in fp_banner.lower())

    print("\n--- False-positive guard against a MISSING/corrupt "
          "resolved-accounts record too (e.g. an installation that "
          "reached 'done' before this fix shipped): fails toward "
          "OVER-warning, not under-warning ---")
    db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY, "")
    check("with the resolved-accounts record missing, _read_resolved_accounts() "
          "returns an empty set (fails safe, not silently trusting)",
          setup_wizard._read_resolved_accounts() == set())
    fp_banner_missing_record = setup_wizard._already_done_banner()
    check("without a resolved-accounts record, the fix now (correctly, "
          "safely) treats even the genuine $0 account as unexplained -- "
          "over-warning rather than silently hiding a possible gap on "
          "an old installation",
          "Zero Balance Account" in fp_banner_missing_record)
    # Restore a correct record so it doesn't leak into anything after.
    db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                   json.dumps(["acc-fp-zero", "acc-fp-nonzero"]))

    # ==================================================================
    print("\n--- Critical 2 fix: Screen 4 (cutover) now short-circuits "
          "at 'done' too, exactly like Screen 5 already did -- closes "
          "the silent-data-loss dead end where a newly-mapped "
          "account's REAL opening balance, typed and submitted on "
          "Screen 4, used to be written into the draft and then "
          "orphaned forever (Screen 5's own 'done' short-circuit would "
          "bounce it away without ever reading it) ---")
    fresh_data_dir()
    c2_target_a = ledger.create_account("Critical2 Account A", "asset",
                                        parent_code="1000")
    seed_mapped_account("acc-c2-a", "Existing C2 Account", None, c2_target_a)
    c2_start = latest - timedelta(days=1)
    set_draft(c2_start.isoformat(), {"acc-c2-a": 20000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)
    resp_c2_finish = setup_wizard.setup_simplefin_finish()
    check("normal finish for account A succeeds, stage now 'done'",
          isinstance(resp_c2_finish, RedirectResponse)
          and resp_c2_finish.status_code == 303
          and setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_DONE)

    # Map a brand-new account B directly, exactly what the mapping
    # screen's own db.set_bank_ledger_account call would do -- reachable
    # because _require_simplefin_stage is a >= check, so
    # /setup/simplefin/accounts stays open past "done" on purpose.
    c2_target_b = ledger.create_account("Critical2 Account B", "asset",
                                        parent_code="1000")
    db.upsert_bank_account({
        "source": "simplefin", "account_id": "acc-c2-b",
        "name": "Newly Mapped C2 Account", "official_name": None,
        "type": "", "subtype": "", "mask": None, "synced_at": synced_at(),
    })
    ok = db.set_bank_ledger_account("simplefin", "acc-c2-b", c2_target_b["id"])
    assert ok

    before_c2_get = full_snapshot()
    get_c2 = setup_wizard.setup_simplefin_cutover_screen()
    check("Screen 4's GET now redirects instead of rendering a real "
          "opening-balance form for account B -- the fix",
          isinstance(get_c2, RedirectResponse) and get_c2.status_code == 303)
    check("ZERO writes from the GET -- byte-identical to before this "
          "request",
          full_snapshot() == before_c2_get)

    # The sharper reproduction: a forged/direct POST with a REAL
    # opening balance typed for account B -- exactly what the reviewer
    # reproduced (a person filling out a genuine number and submitting
    # it). Before this fix, this would have been written into
    # SIMPLEFIN_PENDING_CUTOVER_KEY and orphaned there forever, since
    # Screen 5 would bounce it away via its OWN "done" short-circuit
    # without ever reading it.
    before_c2_post = full_snapshot()
    resp_c2_post = asyncio.run(setup_wizard.setup_simplefin_cutover(FakeRequest({
        "start_date": c2_start.isoformat(),
        "balance__acc-c2-a": "200.00",
        "balance__acc-c2-b": "999.99",  # the real balance a person typed
    })))
    check("Screen 4's POST also redirects instead of accepting the "
          "form",
          isinstance(resp_c2_post, RedirectResponse)
          and resp_c2_post.status_code == 303)
    check("ZERO writes from the POST -- the draft is byte-identical to "
          "before this request; account B's $999.99 was NEVER written "
          "anywhere, orphaned or otherwise",
          full_snapshot() == before_c2_post)
    check("the pending-cutover draft specifically still holds nothing "
          "(confirmed directly, not just via the snapshot tuple)",
          not db.get_setting(setup_wizard.SIMPLEFIN_PENDING_CUTOVER_KEY))
    check("account A's own already-posted entry is completely "
          "unaffected by any of this",
          len(entries_for("simplefin:opening:acc-c2-a")) == 1)

    # ==================================================================
    print("\n--- Important 7 fix: strengthen the ACTIVE_BANK_KEY-"
          "written-last proof -- monkeypatch "
          "ledger.create_sourced_journal_entries itself to raise, "
          "forcing the failure INSIDE/AFTER the batch call (the only "
          "place that can actually distinguish 'written last' from "
          "'written anywhere before pre-validation') ---")
    fresh_data_dir()
    i7_target = ledger.create_account("Important7 Asset", "asset",
                                      parent_code="1000")
    seed_mapped_account("acc-i7", "Important7 Account", None, i7_target)
    i7_start = latest - timedelta(days=1)
    set_draft(i7_start.isoformat(), {"acc-i7": 15000})
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_CONFIRM)

    active_bank_before_i7 = db.get_setting(banks.ACTIVE_BANK_KEY)
    before_i7 = full_snapshot()

    original_batch_fn = ledger.create_sourced_journal_entries

    def _boom(specs):
        raise ledger.LedgerValidationError(
            "forced test failure -- simulates a genuine failure "
            "reachable only INSIDE the real batch call, not caught by "
            "any earlier pre-validation")

    ledger.create_sourced_journal_entries = _boom
    try:
        resp_i7 = setup_wizard.setup_simplefin_finish()
    finally:
        ledger.create_sourced_journal_entries = original_batch_fn

    check("the finish request is refused when the batch call ITSELF "
          "raises (not a 303 success)",
          not isinstance(resp_i7, RedirectResponse))
    check("STRENGTHENED PROOF: ACTIVE_BANK_KEY is genuinely untouched "
          "by a failure that only the batch call itself can produce -- "
          "this is the test the reviewer asked for, since the earlier "
          "'vanish' scenario's failure happens during PRE-VALIDATION "
          "(before the batch is ever called) and on its own cannot "
          "distinguish 'written last' from 'written anywhere before "
          "pre-validation'",
          db.get_setting(banks.ACTIVE_BANK_KEY) == active_bank_before_i7)
    check("the real (non-monkeypatched) function is genuinely restored "
          "afterward",
          ledger.create_sourced_journal_entries is original_batch_fn)
    check("ZERO other settings/draft/stage changes from this forced "
          "failure either",
          full_snapshot() == before_i7)

    # Prove the monkeypatch itself would have been caught if this test
    # were broken for some unrelated reason: a clean finish, with the
    # real function restored, still succeeds normally right after.
    resp_i7_clean = setup_wizard.setup_simplefin_finish()
    check("with the real function restored, a clean finish succeeds "
          "normally",
          isinstance(resp_i7_clean, RedirectResponse)
          and resp_i7_clean.status_code == 303)

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN confirm/finish "
              "checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN confirm/finish checks "
          "passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
