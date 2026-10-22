"""Regression coverage for Task 7 of the SimpleFIN feature: the dedicated
"Bank — SimpleFIN Bridge" card on the Connections page (app.py), the gaps
prior tasks' reviews surfaced, and the 4 further Important issues an
end-to-end review of the whole 7-task feature found in this task's own
first pass.

Covers:
- The card's states: never set up (cost disclosure, incl. the $15/year
  figure and that Klearing credits it against the subscription, plus a
  /setup/simplefin link), configured but not finished (a "Continue
  setup" link, which ALSO now offers "Reconnect with a new token"), and
  fully connected (account count + start date, with Re-test / Edit
  account mapping / Reconnect actions).
- Gap 1 fix (Task 6 reviewer finding): the persistent unresolved-account
  warning. Reproduces Task 6's exact scenario (finish normally, then map
  a brand-new account directly -- reachable because the mapping
  screen's stage gate is a >= check) and confirms the card shows a
  warning line visually DISTINCT from the connected-status sentence
  (its own <div class="callout"><strong>Needs attention</strong>...),
  not folded into it -- and that no such warning appears when nothing
  is unresolved. Also pins the exact pluralized/joined wording for
  THREE missing accounts at once.
- setup_wizard.simplefin_resolution_summary() -- the structured
  extraction pulled out of _already_done_banner() -- tested directly
  for all FOUR classifications (posted, zero_resolved, missing,
  remapped).
- _already_done_banner()'s exact prose proven BYTE-FOR-BYTE unchanged
  for every NON-remap scenario, against a frozen reference
  reimplementation of the algorithm as it stood before this feature's
  final review round (empty, all-posted, all-missing with a corrupt
  resolved-accounts record, a 1-posted/1-zero/1-missing mix), and once
  more through the real Screen 5 GET route itself.
- REVIEWER-FOUND GAP (real-money-correctness, Important #1): a post-
  finish remap (via "Edit account mapping", one click away on this same
  Connections page) used to silently orphan the old opening balance
  with NO warning at all, because ledger_source_import_exists() alone
  stays true forever once an entry posts, regardless of which account
  it hit. Fixed via db.source_import_account_codes() cross-checked
  against the account's CURRENT mapped code, surfaced as a new
  "remapped" bucket. Proven end-to-end: finish -> remap -> the
  resolution summary, the Screen 5 banner (a DELIBERATE behavior change
  from the frozen reference, proven by their outputs now differing),
  and the Connections card warning all correctly flag it -- while
  confirming the old posted entry itself is left untouched (migrating
  it is explicitly out of scope).
- REVIEWER-FOUND GAP (Important #2): the duplicate, contradictory
  Connections card. Once SimpleFIN is the active bank, the generic
  "Bank — {bank_name}" card used to render an identically-titled
  second card with a DIFFERENT (and wrong) freshness line. Fixed by
  suppressing the generic card specifically when the active adapter is
  SimpleFIN, and by dropping "simplefin" from _CONN_SYNC_SERVICES
  (nothing ever syncs it, only a manual Re-test tests connectivity).
  Proven: exactly one "Bank — SimpleFIN Bridge" heading when SimpleFIN
  is active; the generic card is untouched for any OTHER active bank.
- REVIEWER-FOUND GAP (Important #3): the try/except around
  is_configured() was both too broad (a bare `except Exception` would
  also swallow a real, fixable secrets_store.SecretsError) and didn't
  even protect the page in the case that matters most, since the
  GENERIC bank card's own unguarded is_configured() call raises FIRST
  when SimpleFIN is the active bank. Fixed by narrowing to
  `except secrets_store.SecretsError` on BOTH the generic and dedicated
  cards, rendering an honest "credentials could not be read" state
  instead of a misleading "not connected". Proven: the page renders
  without crashing in the reviewer's exact reproduction (SimpleFIN
  active, its is_configured() raising SecretsError), for Mercury active
  too (proving the generic card's own guard independently), and that a
  genuinely different exception (ValueError) is NOT swallowed.
- REVIEWER-FOUND GAP (Important #4): a real dead-end loop when the
  stage is past "mapping" but the credential is entirely MISSING (not
  just failing a live test) -- is_configured() is False, so the card
  used to fall into "never set up", which only offers "Set up
  SimpleFIN", not "Reconnect". Fixed by offering the reconnect link
  whenever the stage has reached "mapping", independent of
  is_configured(). Proven for that exact state, for the
  "configured-but-not-done" branch (which also gained the link), and
  that a genuinely fresh install (stage "intro") does NOT show a
  reconnect link with nothing to reconnect.
- Optional cleanups also verified: a plain (non-reconnect) render of
  Screen 2 genuinely OMITS the hidden reconnect field entirely (not
  just as an empty value) -- the reviewer's own concern that an
  unconditional field would still pass every other check while
  silently defeating the double-submit protection.
- Gap 2 fix (from the earlier round): the reconnect bypass end-to-end,
  and the direct-POST-without-the-field protection unchanged.
- connections_test(service="simplefin"): not-connected -> error banner;
  connected -> calls adapter.test_connection with the stored credentials
  and reports the real result (success and failure both); proven
  independent of banks.active_adapter() by running with Mercury set as
  the active bank throughout.
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import urllib.parse
from decimal import Decimal
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-conn-card-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import banks  # noqa: E402
from banks import simplefin  # noqa: E402
import connectivity  # noqa: E402
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
    """Minimal stand-in for a FastAPI Request, matching every other
    SimpleFIN test file's own convention exactly."""
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


class Response:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


FIXTURE_CLAIM_URL = "https://bridge.simplefin.test/simplefin/claim/reconnecttest"
FIXTURE_TOKEN = base64.b64encode(FIXTURE_CLAIM_URL.encode()).decode()
FIXTURE_ACCESS_URL_1 = "https://user:firstpass@api.simplefin.test/simplefin"
FIXTURE_ACCESS_URL_2 = "https://user:secondpass@api.simplefin.test/simplefin"

ACCOUNTS_PAYLOAD_OK = {
    "accounts": [
        {"id": "checking", "name": "Checking", "currency": "USD",
         "balance": "100.00"},
    ],
}


class _OkResponse:
    status_code = 200
    text = ""

    def json(self):
        return ACCOUNTS_PAYLOAD_OK


def _ok_get(*_args, **_kwargs):
    return _OkResponse()


def _raise_secrets_error():
    """Stand-in for a bound is_configured() that fails with a real,
    fixable credential-store problem -- used to prove the Connections
    page narrowly catches secrets_store.SecretsError specifically
    (issue 3), not a bare `except Exception`, and to prove that catch
    is what actually stops the page from 500ing when the affected
    adapter is the currently active bank."""
    raise secrets_store.SecretsError(
        "The stored master key is malformed and can't be used.")


def synced_at():
    return "2026-08-25T00:00:00Z"


def seed_mapped_account(account_id, name, ledger_account, mask=None):
    db.upsert_bank_account({
        "source": "simplefin", "account_id": account_id, "name": name,
        "official_name": None, "type": "", "subtype": "", "mask": mask,
        "synced_at": synced_at(),
    })
    ok = db.set_bank_ledger_account("simplefin", account_id, ledger_account["id"])
    assert ok, f"failed to seed mapping for {account_id}"


def post_opening_entry(account_id, ledger_account, cents, date_value="2026-04-30"):
    equity = ledger.account_by_code("3000")
    ledger.create_sourced_journal_entries([{
        "date_value": date_value,
        "memo": f"SimpleFIN opening balance: {account_id}",
        "lines": [
            {"account_id": ledger_account["id"],
             "debit_amount": Decimal(cents) / 100, "credit_amount": 0},
            {"account_id": equity["id"], "debit_amount": 0,
             "credit_amount": Decimal(cents) / 100},
        ],
        "source_reference": f"simplefin:opening:{account_id}",
        "created_by": "simplefin-setup",
    }])


def fresh_data_dir():
    """Point KLEARING_DATA_DIR at a brand-new temp directory and re-init
    -- several scenarios below need the stage to start below 'done'
    again, and the stage is monotonic-forward-only. Mirrors the same
    swap-mid-script pattern the other SimpleFIN test files already use."""
    os.environ["KLEARING_DATA_DIR"] = tempfile.mkdtemp(
        prefix="klearing-simplefin-conn-card-")
    db.init_db()


def secret_stored():
    return secrets_store.get_secret(
        banks.get_adapter("simplefin").secret_name("access_url"))


def _reference_already_done_banner():
    """A frozen copy of the PRE-REFACTOR _already_done_banner() algorithm
    -- structure and wording copied verbatim from commit 502b5f8, the
    commit this task started from, BEFORE the classification loop was
    pulled out into simplefin_resolution_summary(). Comparing the REAL
    function's output against this frozen copy, scenario by scenario, is
    the byte-for-byte proof the refactor changed no observable behavior
    -- not just "the code looks equivalent".

    This frozen copy also has NO concept of "remapped" (an account
    posted correctly once, then remapped to a different Ledger account
    afterward) -- exactly the reviewer-found gap fixed after this task's
    initial refactor: ledger_source_import_exists() alone stays true
    forever once an entry posts, so this OLD algorithm would count a
    remapped account as "posted" and never warn. It is used BOTH to
    prove non-remap scenarios are unchanged (real == reference) AND, in
    the remap scenario below, to prove the fix actually changes
    detection where it needs to (real != reference, and only the real
    one names the problem)."""
    mapped_rows = setup_wizard._mapped_simplefin_accounts()
    resolved_ids = setup_wizard._read_resolved_accounts()
    posted_count = 0
    missing_names = []
    for row in mapped_rows:
        account_id = row["account_id"]
        if db.ledger_source_import_exists(f"simplefin:opening:{account_id}"):
            posted_count += 1
        elif account_id not in resolved_ids:
            missing_names.append(row.get("name") or account_id)
        # else: resolved but unposted -- a genuine, deliberate $0 at
        # finish time. Correct and silent, nothing to report.

    parts = ["SimpleFIN is already connected."]
    if posted_count:
        plural = "" if posted_count == 1 else "s"
        parts.append(
            f"{posted_count} account{plural} already has an opening "
            "balance posted from a previous attempt.")
    if missing_names:
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
    return " ".join(parts)


def run():
    db.init_db()

    # ==================================================================
    print("Card state: never set up (no SimpleFIN credentials at all)")
    check("adapter reports not configured in a fresh DB",
          not banks.get_adapter("simplefin").is_configured())
    html_never = app.render_connections_page()
    check("the card is present, correctly named",
          "Bank — SimpleFIN Bridge" in html_never)
    check("the $15/year cost is disclosed plainly",
          "SimpleFIN Bridge costs $15/year, billed by SimpleFIN "
          "directly." in html_never)
    check("states Klearing credits that cost against the subscription",
          "Klearing credits that against your subscription." in html_never)
    check("links to /setup/simplefin to start setup",
          '<a class="save-btn" href="/setup/simplefin">Set up SimpleFIN</a>'
          in html_never)
    check("does not yet show the mid-setup or connected copy",
          "Continue setup" not in html_never
          and "Connected via SimpleFIN Bridge" not in html_never)

    # ==================================================================
    print("Card state: configured, but setup not finished")
    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        FIXTURE_ACCESS_URL_1)
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_MAPPING)
    check("adapter now reports configured",
          banks.get_adapter("simplefin").is_configured())
    check("stage is 'mapping', not yet 'done'",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
    html_partial = app.render_connections_page()
    check("shows the 'started but not finished' status",
          "Setup started but not finished." in html_partial)
    check("shows a 'Continue setup' link back to the entry point",
          '<a class="save-btn" href="/setup/simplefin">Continue setup</a>'
          in html_partial)
    check("no longer shows the never-set-up cost-disclosure copy",
          "SimpleFIN Bridge costs $15/year" not in html_partial)
    check("does not yet show the fully-connected status",
          "Connected via SimpleFIN Bridge" not in html_partial)
    check("ALSO offers 'Reconnect with a new token' in this state -- "
          "not only in the fully-connected branch (issue 4's fix: the "
          "stage has already reached mapping here, so a way back to "
          "the paste form must exist even before finishing)",
          '<a class="btn-link" href="/setup/simplefin/token?reconnect=1">'
          "Reconnect with a new token</a>" in html_partial)

    # ==================================================================
    print("Card state: fully connected, nothing unresolved")
    checking_target = ledger.create_account(
        "Conn Card Checking", "asset", parent_code="1000")
    savings_target = ledger.create_account(
        "Conn Card Savings", "asset", parent_code="1000")
    seed_mapped_account("acc-conn-checking", "Checking", checking_target,
                        mask="1111")
    seed_mapped_account("acc-conn-savings", "Savings", savings_target,
                        mask="2222")
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
    db.set_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY, "2026-05-01")
    db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                   json.dumps(["acc-conn-checking", "acc-conn-savings"]))
    # acc-conn-checking gets a real posted entry; acc-conn-savings is
    # resolved but unposted -- a genuine, deliberate $0 at finish time.
    # Neither should ever be reported as "missing".
    post_opening_entry("acc-conn-checking", checking_target, 50000)

    html_full = app.render_connections_page()
    check("shows the fully-connected status with the right account count",
          "Connected via SimpleFIN Bridge. Reading 2 accounts since "
          "2026-05-01." in html_full)
    check("no persistent unresolved-account warning -- nothing is missing",
          "no opening balance recorded" not in html_full)
    check("Re-test posts to the dedicated simplefin test route",
          'action="/connections/test/simplefin"' in html_full)
    check("Edit account mapping links to the mapping screen",
          '<a class="btn-link" href="/setup/simplefin/accounts">Edit '
          "account mapping</a>" in html_full)
    check("Reconnect with a new token links with the reconnect flag",
          '<a class="btn-link" href="/setup/simplefin/token?reconnect=1">'
          "Reconnect with a new token</a>" in html_full)

    # ==================================================================
    print("Persistent unresolved-account warning: Task 6's exact "
          "reviewer-found gap, reproduced, now surfaced on the card "
          "instead of a one-shot redirect banner")
    gap_target = ledger.create_account(
        "Conn Card Gap Target", "asset", parent_code="1000")
    seed_mapped_account("acc-conn-gap", "New Unmapped-Balance Account",
                        gap_target)
    # Deliberately NOT added to SIMPLEFIN_RESOLVED_ACCOUNTS_KEY and no
    # ledger_source_imports row -- mapped AFTER setup already reached
    # "done", exactly Task 6's reviewer reproduction.
    html_gap = app.render_connections_page()
    check("the connected-status line still reports correctly, now with "
          "3 accounts",
          "Connected via SimpleFIN Bridge. Reading 3 accounts since "
          "2026-05-01." in html_gap)
    check("the warning names the unresolved account",
          "New Unmapped-Balance Account" in html_gap)
    check("the warning states the fact plainly",
          "1 account (New Unmapped-Balance Account) is mapped but has "
          "no opening balance recorded." in html_gap)
    check("the warning renders in its OWN distinct block (a callout, "
          "with a strong lead-in matching the page's other callouts), "
          "not folded into the connected-status sentence",
          '<div class="callout"><strong>Needs attention</strong> 1 '
          "account (New Unmapped-Balance Account) is mapped but has no "
          "opening balance recorded.</div>" in html_gap
          and ("Reading 3 accounts since 2026-05-01.<div "
               'class="callout">' in html_gap))
    check("this warning appears on a PLAIN render (no msg=/err= passed "
          "in) -- proving it is read fresh from live state on every "
          "visit, not a one-shot redirect banner that vanishes on the "
          "next navigation",
          True)

    print("...and simplefin_resolution_summary() classifies all four "
          "outcomes correctly in this exact scenario")
    summary = setup_wizard.simplefin_resolution_summary()
    check("posted contains exactly the one account with a real entry "
          "at its CURRENT mapped account",
          {row["account_id"] for row in summary["posted"]}
          == {"acc-conn-checking"})
    check("zero_resolved contains exactly the deliberately-$0 account",
          {row["account_id"] for row in summary["zero_resolved"]}
          == {"acc-conn-savings"})
    check("missing contains exactly the genuinely-unresolved account",
          {row["account_id"] for row in summary["missing"]}
          == {"acc-conn-gap"})
    check("remapped is empty -- nothing has been remapped in this "
          "scenario yet",
          summary["remapped"] == [])

    print("...and _already_done_banner() on this exact mixed scenario "
          "matches the frozen pre-refactor reference byte-for-byte")
    check("mixed scenario (1 posted, 1 zero-resolved, 1 missing)",
          setup_wizard._already_done_banner()
          == _reference_already_done_banner())

    # ==================================================================
    print("\n--- REVIEWER-FOUND GAP: a post-finish remap silently "
          "orphans the opening balance, with NO warning (fixed) ---")
    fresh_data_dir()
    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        FIXTURE_ACCESS_URL_1)
    old_target = ledger.create_account(
        "Remap Old Checking", "asset", parent_code="1000")
    new_target = ledger.create_account(
        "Remap New Checking", "asset", parent_code="1000")
    seed_mapped_account("acc-remap", "Remap Checking", old_target)
    db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                   json.dumps(["acc-remap"]))
    post_opening_entry("acc-remap", old_target, 50000)
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
    db.set_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY, "2026-05-01")

    summary_before = setup_wizard.simplefin_resolution_summary()
    check("BEFORE remap: correctly classified as posted, remapped empty",
          {row["account_id"] for row in summary_before["posted"]}
          == {"acc-remap"}
          and summary_before["remapped"] == [])
    banner_before = setup_wizard._already_done_banner()
    check("BEFORE remap: banner reports it as posted, no warning",
          "1 account already has an opening balance posted" in banner_before
          and "DIFFERENT Ledger account" not in banner_before)

    # Remap acc-remap to a DIFFERENT Ledger account -- exactly what the
    # "Edit account mapping" screen (one click away on the Connections
    # page as of this task) does. The OLD posted entry is left exactly
    # where it was -- this only changes which account the SimpleFIN
    # account currently points at. Reproduces the reviewer's exact
    # scenario end to end.
    ok = db.set_bank_ledger_account("simplefin", "acc-remap", new_target["id"])
    assert ok

    summary_after = setup_wizard.simplefin_resolution_summary()
    check("AFTER remap: no longer silently counted as 'posted'",
          summary_after["posted"] == [])
    check("AFTER remap: correctly bucketed as 'remapped', not 'missing' "
          "-- a real entry DOES exist, just not at the current mapping",
          {row["account_id"] for row in summary_after["remapped"]}
          == {"acc-remap"}
          and summary_after["missing"] == [])

    banner_after = setup_wizard._already_done_banner()
    check("AFTER remap: the Screen 5 banner now names the account and "
          "explicitly says the balance posted to a DIFFERENT account",
          "Remap Checking" in banner_after
          and "DIFFERENT Ledger account" in banner_after
          and "1 account already has an opening balance posted"
          not in banner_after)
    check("the OLD (pre-fix) algorithm would have missed this entirely "
          "-- the real banner now genuinely differs from the frozen "
          "reference, which still calls it 'posted' with no warning at "
          "all (proving this is a real behavior CHANGE, deliberately, "
          "for exactly the case that needed one)",
          banner_after != _reference_already_done_banner()
          and _reference_already_done_banner()
          == "SimpleFIN is already connected. 1 account already has an "
             "opening balance posted from a previous attempt.")

    html_remap = app.render_connections_page()
    check("the Connections card ALSO shows the distinct 'Needs "
          "attention' warning for the remapped account, instead of a "
          "false 'all good' status",
          '<div class="callout"><strong>Needs attention</strong> 1 '
          "account (Remap Checking) has an opening balance posted to a "
          "different account than where it's currently mapped.</div>"
          in html_remap)
    check("the card's connected-status line is unaffected (still 1 "
          "mapped account, correctly counted)",
          "Connected via SimpleFIN Bridge. Reading 1 account since "
          "2026-05-01." in html_remap)

    old_codes = db.source_import_account_codes("simplefin:opening:acc-remap")
    check("the OLD posted entry itself is untouched -- still hits the "
          "OLD account's code (alongside the 3000 equity offset line, "
          "the entry's other side), proving this fix changes DETECTION "
          "only, never migrates the entry or prevents the remap (both "
          "explicitly out of scope), and NOT the NEW account's code",
          old_target["code"] in old_codes
          and new_target["code"] not in old_codes)

    # ==================================================================
    print("\n--- _already_done_banner(): byte-for-byte pre/post-refactor "
          "comparison across further scenarios (empty, all-posted, "
          "all-missing with a corrupt resolved-accounts record) ---")

    fresh_data_dir()
    check("empty scenario (no mapped accounts at all)",
          setup_wizard._already_done_banner()
          == _reference_already_done_banner()
          == "SimpleFIN is already connected.")

    fresh_data_dir()
    posted_a = ledger.create_account("Banner Posted A", "asset",
                                     parent_code="1000")
    posted_b = ledger.create_account("Banner Posted B", "asset",
                                     parent_code="1000")
    seed_mapped_account("acc-banner-p1", "Posted One", posted_a)
    seed_mapped_account("acc-banner-p2", "Posted Two", posted_b)
    post_opening_entry("acc-banner-p1", posted_a, 10000)
    post_opening_entry("acc-banner-p2", posted_b, 20000)
    check("all-posted scenario (plural wording path)",
          setup_wizard._already_done_banner()
          == _reference_already_done_banner())

    fresh_data_dir()
    missing_a = ledger.create_account("Banner Missing A", "asset",
                                      parent_code="1000")
    missing_b = ledger.create_account("Banner Missing B", "asset",
                                      parent_code="1000")
    seed_mapped_account("acc-banner-m1", "Missing One", missing_a)
    seed_mapped_account("acc-banner-m2", "Missing Two", missing_b)
    # A corrupt resolved-accounts record -- _read_resolved_accounts()
    # must fail toward over-warning (treat as empty), and the reference
    # implementation must agree exactly.
    db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                   "not valid json {{{")
    check("all-missing scenario with a corrupt resolved-accounts record "
          "(plural wording path, over-warning fallback)",
          setup_wizard._already_done_banner()
          == _reference_already_done_banner())
    banner_corrupt = setup_wizard._already_done_banner()
    check("both accounts are actually named in that banner",
          "Missing One" in banner_corrupt and "Missing Two" in banner_corrupt)

    print("...and once more through the REAL Screen 5 GET route itself "
          "(not just the raw function) -- proving the short-circuit's "
          "actual redirect banner is unchanged too")
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
    confirm_resp = setup_wizard.setup_simplefin_confirm_screen()
    check("GET at stage 'done' redirects (the short-circuit)",
          isinstance(confirm_resp, RedirectResponse)
          and confirm_resp.status_code == 303)
    redirected_banner = urllib.parse.parse_qs(
        urllib.parse.urlsplit(confirm_resp.headers.get("location", "")).query
    ).get("msg", [""])[0]
    check("the real route's redirect banner matches the frozen reference "
          "exactly, byte-for-byte",
          redirected_banner == _reference_already_done_banner())

    # ==================================================================
    print("\n--- Connections card wording with MULTIPLE missing "
          "accounts (pluralization/joining, pinned by a test) ---")
    fresh_data_dir()
    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        FIXTURE_ACCESS_URL_1)
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
    db.set_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY, "2026-05-01")
    alpha_target = ledger.create_account("Multi Missing Alpha Target",
                                         "asset", parent_code="1000")
    beta_target = ledger.create_account("Multi Missing Beta Target",
                                        "asset", parent_code="1000")
    gamma_target = ledger.create_account("Multi Missing Gamma Target",
                                         "asset", parent_code="1000")
    seed_mapped_account("acc-multi-alpha", "Alpha", alpha_target)
    seed_mapped_account("acc-multi-beta", "Beta", beta_target)
    seed_mapped_account("acc-multi-gamma", "Gamma", gamma_target)
    # All three deliberately unresolved -- no ledger_source_imports row,
    # not in SIMPLEFIN_RESOLVED_ACCOUNTS_KEY for any of them.
    html_multi = app.render_connections_page()
    check("the card joins and pluralizes correctly for 3 missing "
          "accounts: '3 accounts (Alpha, Beta, Gamma) are mapped but "
          "have no opening balance recorded.'",
          "3 accounts (Alpha, Beta, Gamma) are mapped but have no "
          "opening balance recorded." in html_multi)
    check("rendered inside the same distinct, strong-lead-in callout "
          "as the single-account case",
          '<div class="callout"><strong>Needs attention</strong> 3 '
          "accounts (Alpha, Beta, Gamma) are mapped but have no opening "
          "balance recorded.</div>" in html_multi)

    # ==================================================================
    print("\n--- Reconnect bypass (Gap 2 fix) ---")
    print("  A plain (non-reconnect) render of the paste form genuinely "
          "OMITS the hidden reconnect field entirely, not just as an "
          "empty value -- the reviewer's own finding: an unconditional "
          "field would still pass every other check in this file while "
          "silently defeating the double-submit protection")
    plain_render = setup_wizard.render_simplefin_token()
    check("no 'reconnect' field of any kind on a plain render",
          'name="reconnect"' not in plain_render)
    reconnect_render = setup_wizard.render_simplefin_token(reconnect=True)
    check("...while a reconnect=True render DOES carry it -- proving "
          "the field is conditional, not just absent by coincidence",
          'name="reconnect"' in reconnect_render)

    original_post = simplefin.requests.post
    original_get = simplefin.requests.get
    try:
        fresh_data_dir()
        adapter = banks.get_adapter("simplefin")
        secrets_store.set_secret(adapter.secret_name("access_url"),
                                 FIXTURE_ACCESS_URL_1)
        setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
        reconnect_target = ledger.create_account(
            "Reconnect Test Asset", "asset", parent_code="1000")
        seed_mapped_account("acc-reconnect", "Reconnect Test Account",
                            reconnect_target)
        db.set_setting(setup_wizard.SIMPLEFIN_RESOLVED_ACCOUNTS_KEY,
                       json.dumps(["acc-reconnect"]))
        check("stage is 'done' (well past 'mapping') going into this "
              "scenario",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_DONE)

        print("  GET without ?reconnect=1, past 'mapping': unchanged "
              "safety notice")
        bare_get = setup_wizard.setup_simplefin_token_screen()
        check("shows the already-connected notice, not the paste form",
              isinstance(bare_get, str)
              and "Already connected" in bare_get
              and 'name="setup_token"' not in bare_get)

        print("  GET with ?reconnect=1, past 'mapping': the real paste "
              "form, with the hidden field the POST checks for")
        reconnect_get = setup_wizard.setup_simplefin_token_screen(
            reconnect="1")
        check("shows the real paste form, not the already-connected "
              "notice",
              isinstance(reconnect_get, str)
              and 'name="setup_token"' in reconnect_get
              and "<textarea" in reconnect_get
              and "Already connected" not in reconnect_get)
        check("carries the hidden reconnect=1 field",
              '<input type="hidden" name="reconnect" value="1">'
              in reconnect_get)
        check("a bare/bookmarked visit (checked again, no param) is "
              "genuinely unaffected by the reconnect branch existing",
              "Already connected" in setup_wizard.setup_simplefin_token_screen())

        print("  Direct POST that never carries the reconnect field, "
              "past 'mapping': still refuses to process (unchanged "
              "protection against accidentally burning a token)")
        before_direct = secret_stored()
        direct_post = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        check("redirects without processing (no reconnect field present)",
              isinstance(direct_post, RedirectResponse)
              and direct_post.status_code == 303
              and direct_post.headers.get("location") == "/setup/simplefin/token")
        check("the stored Access URL is untouched by the blocked POST",
              secret_stored() == before_direct == FIXTURE_ACCESS_URL_1)

        print("  A FAILED reconnect attempt (blank token, but WITH the "
              "reconnect field) re-shows the paste form, not the "
              "already-connected notice, and stores nothing")
        empty_reconnect = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": "   ", "reconnect": "1"})))
        rendered_empty_reconnect = empty_reconnect.body.decode("utf-8")
        check("re-shows the paste form with the paste-a-token error",
              "Paste your SimpleFIN setup token." in rendered_empty_reconnect
              and 'name="setup_token"' in rendered_empty_reconnect)
        check("the reconnect field is preserved on the re-rendered form",
              '<input type="hidden" name="reconnect" value="1">'
              in rendered_empty_reconnect)
        check("nothing stored by the failed reconnect attempt",
              secret_stored() == FIXTURE_ACCESS_URL_1)

        print("  Full reconnect end-to-end: GET (flagged) -> POST a new "
              "valid token -> the stored Access URL actually updates")
        simplefin.requests.post = lambda *_a, **_k: Response(
            text=FIXTURE_ACCESS_URL_2)
        simplefin.requests.get = _ok_get
        reconnect_post = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN, "reconnect": "1"})))
        check("the reconnect POST succeeds (a 303, not a re-rendered "
              "form)",
              isinstance(reconnect_post, RedirectResponse)
              and reconnect_post.status_code == 303)
        check("the stored Access URL is now the NEW one",
              secret_stored() == FIXTURE_ACCESS_URL_2)
        check("existing account mapping is untouched by the reconnect",
              any(row["account_id"] == "acc-reconnect"
                  for row in setup_wizard._mapped_simplefin_accounts()))
        check("the resolved-accounts record is untouched by the "
              "reconnect",
              setup_wizard._read_resolved_accounts() == {"acc-reconnect"})
        check("the stage did not regress -- still at least 'mapping' "
              "(here, still 'done')",
              setup_wizard._simplefin_stage_index(setup_wizard.simplefin_stage())
              >= setup_wizard._simplefin_stage_index(
                  setup_wizard.SIMPLEFIN_STAGE_MAPPING))
        check("it lands on the account-mapping screen next, exactly "
              "like a first-time connection would",
              reconnect_post.headers.get("location", "").startswith(
                  "/setup/simplefin/accounts"))
    finally:
        simplefin.requests.post = original_post
        simplefin.requests.get = original_get

    # ==================================================================
    print("\n--- connections_test(service='simplefin'): independent of "
          "banks.active_adapter() ---")
    fresh_data_dir()
    db.set_setting(banks.ACTIVE_BANK_KEY, "mercury")
    check("Mercury is genuinely the active bank for this scenario",
          banks.active_adapter() is not None
          and banks.active_adapter().id == "mercury")

    check("before any test, the card's freshness line says 'Not yet "
          "tested' -- simplefin is deliberately NOT in "
          "_CONN_SYNC_SERVICES, since check_bank.py's daily sync never "
          "stamps it, only a manual Re-test does, and that only tests "
          "connectivity rather than syncing anything",
          "Not yet tested" in app._conn_tested_line("simplefin")
          and "simplefin" not in app._CONN_SYNC_SERVICES)

    not_connected_resp = app.connections_test("simplefin")
    check("not connected -> a 303 redirect with an error, even with "
          "Mercury active",
          isinstance(not_connected_resp, RedirectResponse)
          and not_connected_resp.status_code == 303)
    not_connected_loc = urllib.parse.unquote(
        not_connected_resp.headers.get("location", ""))
    check("the error names SimpleFIN specifically",
          "SimpleFIN isn't connected yet." in not_connected_loc)
    check("Mercury is STILL the active bank -- untouched by this call",
          banks.active_adapter().id == "mercury")

    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        FIXTURE_ACCESS_URL_1)
    adapter = banks.get_adapter("simplefin")
    original_test_connection = adapter.test_connection
    called_with = {}

    def fake_ok_test_connection(credentials):
        called_with["credentials"] = credentials
        return connectivity.ok("Connected. SimpleFIN returned 1 USD account(s).")

    adapter.test_connection = fake_ok_test_connection
    try:
        connected_resp = app.connections_test("simplefin")
        check("connected -> adapter.test_connection is called with the "
              "REAL stored credentials",
              called_with.get("credentials", {}).get("access_url")
              == FIXTURE_ACCESS_URL_1)
        check("connected -> the real result is reported via a 303 msg "
              "banner",
              isinstance(connected_resp, RedirectResponse)
              and connected_resp.status_code == 303
              and "Connected. SimpleFIN returned 1 USD account(s)."
              in urllib.parse.unquote(connected_resp.headers.get("location", "")))
    finally:
        adapter.test_connection = original_test_connection
    check("Mercury is STILL the active bank after a successful SimpleFIN "
          "test -- proves the simplefin branch never routed through "
          "banks.active_adapter()",
          banks.active_adapter().id == "mercury")

    def fake_fail_test_connection(credentials):
        return connectivity.fail("SimpleFIN rejected the connection.",
                                 hint="Reconnect it with a fresh setup token.")

    adapter.test_connection = fake_fail_test_connection
    try:
        fail_resp = app.connections_test("simplefin")
        fail_loc = urllib.parse.unquote(fail_resp.headers.get("location", ""))
        check("a failing test_connection surfaces as an error banner, "
              "with the hint appended",
              isinstance(fail_resp, RedirectResponse)
              and fail_resp.status_code == 303
              and "SimpleFIN rejected the connection." in fail_loc
              and "Reconnect it with a fresh setup token." in fail_loc)
    finally:
        adapter.test_connection = original_test_connection
    check("Mercury is STILL the active bank after a failing SimpleFIN "
          "test too",
          banks.active_adapter().id == "mercury")

    # ==================================================================
    print("\n--- Duplicate-card / contradictory-status fix (issue 2) ---")
    fresh_data_dir()
    secrets_store.set_secret(
        banks.get_adapter("simplefin").secret_name("access_url"),
        FIXTURE_ACCESS_URL_1)
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_DONE)
    db.set_setting(setup_wizard.SIMPLEFIN_START_DATE_KEY, "2026-05-01")
    db.set_setting(banks.ACTIVE_BANK_KEY, "simplefin")
    check("SimpleFIN is genuinely the active bank for this scenario",
          banks.active_adapter() is not None
          and banks.active_adapter().id == "simplefin")
    html_active = app.render_connections_page()
    check("the generic 'Bank — {bank_name}' card is suppressed when "
          "SimpleFIN is active -- only ONE 'Bank — SimpleFIN Bridge' "
          "heading on the whole page, not two contradictory cards",
          html_active.count("Bank — SimpleFIN Bridge") == 1)
    check("the one card that remains still renders its own connected "
          "state correctly",
          "Connected via SimpleFIN Bridge." in html_active)

    print("...and a DIFFERENT active bank (Mercury) still shows its own "
          "generic card exactly as before -- this fix is scoped to "
          "SimpleFIN specifically, never a blanket suppression")
    db.set_setting(banks.ACTIVE_BANK_KEY, "mercury")
    html_mercury_active = app.render_connections_page()
    check("Mercury's generic bank card still renders (not suppressed), "
          "and SimpleFIN's dedicated card ALSO still renders -- exactly "
          "one heading each",
          html_mercury_active.count("Bank — Mercury") == 1
          and html_mercury_active.count("Bank — SimpleFIN Bridge") == 1)

    # ==================================================================
    print("\n--- Narrowed exception handling actually prevents the 500 "
          "when SimpleFIN is active with a bad credential (issue 3) ---")
    fresh_data_dir()

    print("  First, the GENERIC bank card's own guard on its own "
          "(Mercury active, Mercury's is_configured() raising)")
    db.set_setting(banks.ACTIVE_BANK_KEY, "mercury")
    mercury_adapter = banks.get_adapter("mercury")
    original_mercury_is_configured = mercury_adapter.is_configured
    mercury_adapter.is_configured = _raise_secrets_error
    try:
        html_mercury_bad_cred = app.render_connections_page()
    finally:
        mercury_adapter.is_configured = original_mercury_is_configured
    check("the page renders without crashing",
          isinstance(html_mercury_bad_cred, str)
          and bool(html_mercury_bad_cred.strip()))
    check("the generic bank card (Mercury is active, not SimpleFIN) "
          "shows the honest credential-error state instead of a bare "
          "'not connected'",
          "stored credentials could not be read right now"
          in html_mercury_bad_cred)

    print("  Now the reviewer's EXACT scenario: SimpleFIN is the "
          "active bank (the normal end state of this whole feature) "
          "and its is_configured() raises -- this used to 500 the "
          "WHOLE page before the dedicated SimpleFIN card was ever "
          "reached, since the generic card's unguarded call ran first")
    db.set_setting(banks.ACTIVE_BANK_KEY, "simplefin")
    simplefin_adapter_instance = banks.get_adapter("simplefin")
    original_simplefin_is_configured = simplefin_adapter_instance.is_configured
    simplefin_adapter_instance.is_configured = _raise_secrets_error
    try:
        html_simplefin_bad_cred = app.render_connections_page()
    finally:
        simplefin_adapter_instance.is_configured = original_simplefin_is_configured
    check("the page renders without crashing even with SimpleFIN "
          "active AND its is_configured() raising",
          isinstance(html_simplefin_bad_cred, str)
          and bool(html_simplefin_bad_cred.strip()))
    check("only the dedicated card renders here (the generic one is "
          "correctly suppressed per issue 2's fix), and IT shows its "
          "own honest credential-error state",
          html_simplefin_bad_cred.count("Bank — SimpleFIN Bridge") == 1
          and "SimpleFIN's stored credentials could not be read right "
              "now" in html_simplefin_bad_cred)
    check("it does not fall back to the misleading never-set-up cost "
          "disclosure",
          "SimpleFIN Bridge costs $15/year" not in html_simplefin_bad_cred)
    check("it still offers a way forward: reconnect with a new token",
          '<a class="btn-link" href="/setup/simplefin/token?reconnect=1">'
          "Reconnect with a new token</a>" in html_simplefin_bad_cred)

    print("  ...and a GENUINELY different exception (not SecretsError) "
          "is NOT swallowed -- proving the except clause is narrow, not "
          "a bare `except Exception` in disguise")

    def _raise_value_error():
        raise ValueError("something else entirely broke")

    simplefin_adapter_instance.is_configured = _raise_value_error
    raised = False
    try:
        app.render_connections_page()
    except ValueError:
        raised = True
    finally:
        simplefin_adapter_instance.is_configured = original_simplefin_is_configured
    check("a non-SecretsError exception propagates rather than being "
          "silently treated as 'not connected'",
          raised)

    # ==================================================================
    print("\n--- Dead-end loop fix: reconnect link offered even when "
          "the credential is entirely MISSING (not just failing a live "
          "test), as long as setup previously reached mapping "
          "(issue 4) ---")
    fresh_data_dir()
    # Deliberately do NOT store any access_url secret -- simulates the
    # secret being deleted, or a restored backup missing it -- while
    # the stage setting itself (a separate DB row) still reflects that
    # setup genuinely got past account mapping before.
    setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_MAPPING)
    check("is_configured() is genuinely False here -- the secret is "
          "absent, not just failing a live test",
          not banks.get_adapter("simplefin").is_configured())
    check("but the stage still reflects having reached mapping",
          setup_wizard._simplefin_stage_index(setup_wizard.simplefin_stage())
          >= setup_wizard._simplefin_stage_index(
              setup_wizard.SIMPLEFIN_STAGE_MAPPING))

    html_deadend = app.render_connections_page()
    check("the card still shows the 'never set up' framing (since "
          "is_configured() is False) ...",
          "Set up SimpleFIN" in html_deadend)
    check("...but ALSO explains setup previously reached mapping",
          "Setup previously reached account mapping, but the stored "
          "Access URL is missing now." in html_deadend)
    check("CRITICALLY: it ALSO offers 'Reconnect with a new token' -- "
          "before this fix, this state was a genuine dead-end loop "
          "with no escape except hand-typing ?reconnect=1 in the URL "
          "bar",
          '<a class="btn-link" href="/setup/simplefin/token?reconnect=1">'
          "Reconnect with a new token</a>" in html_deadend)

    print("...and a GENUINE first-time 'never set up' state (stage "
          "still at 'intro', nothing ever attempted) does NOT show a "
          "reconnect link -- there is nothing to reconnect yet")
    fresh_data_dir()
    check("stage is 'intro' by default in a brand-new DB",
          setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_INTRO)
    html_genuine_never = app.render_connections_page()
    check("no reconnect link offered for a genuinely fresh install",
          "Reconnect with a new token" not in html_genuine_never)

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN connections-card "
              "checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN connections-card checks "
          "passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
