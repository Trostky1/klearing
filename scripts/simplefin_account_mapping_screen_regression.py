"""Regression coverage for Screen 3 of the dedicated /setup/simplefin flow
(Task 4 of the SimpleFIN feature): GET/POST /setup/simplefin/accounts, the
ONE place a SimpleFIN account ever gets a Ledger mapping, plus the
carried-forward fix to ``db.ensure_bank_ledger_account`` (it now returns
None early for any row with ``mapping_state == 'excluded'``, so an
excluded Plaid account can no longer be silently re-mapped on its next
sync).

Covers:
- GET renders one row per SimpleFIN account; an unanswered account is
  never preselected onto a real option (only the empty placeholder); an
  account with a real prior answer (mapping OR exclusion) shows that
  answer selected on a repeat visit.
- GET with SimpleFIN unreachable renders the "could not reach" state
  instead of crashing, and makes no bank_accounts writes.
- The Ledger dropdown only ever offers asset/liability accounts.
- A Ledger account already claimed by a DIFFERENT bank account is
  disabled in the rendered HTML; an account's OWN current mapping is
  never disabled in its own dropdown.
- POST with every account answered validly (a mix of an existing-account
  mapping, an exclusion, and a "__new__" account) succeeds: correct rows
  land in bank_accounts, the "__new__" account really exists in the Chart
  of Accounts afterward, the stage advances to "cutover", and a 303
  redirect fires.
- POST missing an answer for any one account -> error, ZERO writes
  (every bank_accounts row and every Ledger account is byte-identical
  before/after).
- POST with two accounts choosing the same existing ledger account
  (forged, bypassing client-side disabling) -> error naming both
  accounts, zero writes.
- POST with two accounts both requesting "__new__" with the same name
  -> a clear "named the same thing" error, zero writes, and no raw/ugly
  LedgerValidationError message leaking through.
- POST with one "__new__" name colliding with a PRE-EXISTING Chart of
  Accounts entry (case-insensitive), alongside another otherwise-valid
  "__new__" request in the SAME batch -> rejected with a specific error,
  and the Chart of Accounts gains ZERO new rows -- including the OTHER,
  otherwise-valid "__new__" request, proving true "validate everything
  before writing anything" rather than just "the colliding one didn't get
  created."
- POST at the wrong stage (still "token") -> redirected, no network call,
  no writes.
- The carried-forward fix itself: excluding a Plaid row via
  db.exclude_bank_account, then calling db.ensure_bank_ledger_account on
  it, now returns None instead of a freshly (re-)created mapping -- and a
  NON-excluded Plaid row is unaffected (still auto-maps as before).
"""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-accounts-screen-", ignore_cleanup_errors=True)
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


class FakeGetResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


def no_network_get(*_args, **_kwargs):
    raise AssertionError("no network call should have been made")


def no_network_post(*_args, **_kwargs):
    raise AssertionError("no network call should have been made")


def accounts_response(accounts):
    return FakeGetResponse(200, {"accounts": accounts})


def unreachable(*_args, **_kwargs):
    raise RuntimeError("simulated network failure")


FIXTURE_ACCESS_URL = "https://user:pass@api.simplefin.test/simplefin"


def select_block(html, account_id):
    """The inner HTML of one account's <select> element (raw options
    markup), or None if that account's row isn't on the page at all."""
    match = re.search(
        r'name="mapping__' + re.escape(account_id) + r'">(.*?)</select>',
        html, re.S)
    return match.group(1) if match else None


def snapshot():
    """Every simplefin bank_accounts row (deep dict copies) plus the full
    Chart of Accounts, for a byte-identical before/after comparison."""
    return (
        [dict(row) for row in db.list_bank_accounts("simplefin")],
        [dict(row) for row in ledger.list_accounts()],
    )


def run():
    db.init_db()
    original_get = simplefin.requests.get
    original_post = simplefin.requests.post

    try:
        adapter = banks.get_adapter("simplefin")

        print("Wrong stage (still 'token'): redirected, no network, no writes")
        simplefin.requests.get = no_network_get
        simplefin.requests.post = no_network_post
        before_wrong_stage = snapshot()
        get_wrong_stage = setup_wizard.setup_simplefin_accounts_screen()
        check("GET at the wrong stage redirects (not a render)",
              isinstance(get_wrong_stage, RedirectResponse)
              and get_wrong_stage.status_code == 303)
        post_wrong_stage = asyncio.run(setup_wizard.setup_simplefin_accounts(
            FakeRequest({"mapping__whatever": "__exclude__"})))
        check("POST at the wrong stage redirects too (and the no_network_* "
              "stand-ins above prove no network call happened along the way)",
              isinstance(post_wrong_stage, RedirectResponse)
              and post_wrong_stage.status_code == 303)
        check("no writes at all from either wrong-stage attempt",
              snapshot() == before_wrong_stage)

        # Gate open: pretend Screens 1-2 already ran (credentials stored,
        # stage advanced), the way setup_simplefin_token's success path does.
        secrets_store.set_secret(adapter.secret_name("access_url"),
                                 FIXTURE_ACCESS_URL)
        setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_MAPPING)

        print("GET: SimpleFIN unreachable shows the 'could not reach' state, no crash")
        simplefin.requests.get = unreachable
        html_unreachable = setup_wizard.render_simplefin_accounts()
        check("the unreachable state renders (a string, not a crash)",
              isinstance(html_unreachable, str))
        check("'Could not reach ... to list accounts' is shown",
              "Could not reach" in html_unreachable
              and "to list accounts" in html_unreachable
              and "SimpleFIN Bridge" in html_unreachable)
        check("no bank_accounts rows were created while unreachable",
              db.list_bank_accounts("simplefin") == [])
        get_unreachable_route = setup_wizard.setup_simplefin_accounts_screen()
        check("the GET route itself also renders the unreachable state "
              "(not a crash) when gated open",
              isinstance(get_unreachable_route, str)
              and "Could not reach" in get_unreachable_route)

        # ---------------------------------------------------------------
        print("GET: one row per account; nothing preselected for a fresh account")
        ACC1 = {"id": "acc-checking", "name": "Checking", "currency": "USD",
                "balance": "1500.00"}
        ACC2 = {"id": "acc-savings", "name": "Savings", "currency": "USD",
                "balance": "3000.00"}
        ACC3 = {"id": "acc-third", "name": "Business Credit", "currency": "USD",
                "balance": "42.50"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC1, ACC2, ACC3])

        revenue_acct = ledger.create_account("Test Revenue Category", "revenue")
        expense_acct = ledger.create_account("Test Expense Category", "expense")
        equity_acct = ledger.create_account("Test Equity Line", "equity")
        target_checking = ledger.create_account(
            "Test Checking Target", "asset", parent_code="1000")
        target_other = ledger.create_account(
            "Test Other Target", "asset", parent_code="1000")

        html_fresh = setup_wizard.render_simplefin_accounts()
        check("one row exists per SimpleFIN account",
              'name="mapping__acc-checking"' in html_fresh
              and 'name="mapping__acc-savings"' in html_fresh
              and 'name="mapping__acc-third"' in html_fresh)
        for account_id in ("acc-checking", "acc-savings", "acc-third"):
            block = select_block(html_fresh, account_id)
            check(f"{account_id}: no option is preselected (relies on the "
                  "empty placeholder as the implicit default)",
                  block is not None and " selected" not in block)
        check("exclude and create-new sentinels are present on every row",
              html_fresh.count("__exclude__") >= 3
              and html_fresh.count("__new__") >= 3)
        check("revenue/expense/equity accounts never appear as ledger "
              "choices in ANY row",
              f'value="{revenue_acct["code"]}"' not in html_fresh
              and f'value="{expense_acct["code"]}"' not in html_fresh
              and f'value="{equity_acct["code"]}"' not in html_fresh)
        check("eligible asset accounts DO appear as ledger choices",
              f'value="{target_checking["code"]}"' in html_fresh
              and f'value="{target_other["code"]}"' in html_fresh)

        # ---------------------------------------------------------------
        print("POST: full valid submission (existing mapping + exclude + __new__)")
        before_full_success = snapshot()
        resp_full = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-checking": target_checking["code"],
            "mapping__acc-savings": "__exclude__",
            "mapping__acc-third": "__new__",
            "new_account_name__acc-third": "Business Credit Card",
            "new_account_type__acc-third": "liability",
        })))
        check("a fully valid submission succeeds with a 303",
              isinstance(resp_full, RedirectResponse)
              and resp_full.status_code == 303)
        check("...and redirects to the cutover screen",
              resp_full.headers.get("location") == "/setup/simplefin/cutover")
        check("stage advanced to 'cutover'",
              setup_wizard.simplefin_stage()
              == setup_wizard.SIMPLEFIN_STAGE_CUTOVER)
        rows_after_success = {row["account_id"]: row
                              for row in db.list_bank_accounts("simplefin")}
        check("the existing-account mapping landed correctly",
              rows_after_success["acc-checking"]["ledger_account_id"]
              == target_checking["id"]
              and rows_after_success["acc-checking"]["mapping_state"] == "")
        check("the exclusion landed correctly",
              rows_after_success["acc-savings"]["mapping_state"] == "excluded"
              and rows_after_success["acc-savings"]["ledger_account_id"] is None)
        new_card = ledger.account_by_code(
            rows_after_success["acc-third"]["ledger_account_id"]
            and next((a["code"] for a in ledger.list_accounts()
                      if a["id"] == rows_after_success["acc-third"]["ledger_account_id"]),
                     None))
        check("the __new__ account really exists in the Chart of Accounts, "
              "as a liability, and is mapped to acc-third",
              new_card is not None
              and new_card["name"] == "Business Credit Card"
              and new_card["type"] == "liability"
              and rows_after_success["acc-third"]["ledger_account_id"]
              == new_card["id"])
        check("that write actually changed something versus the pre-POST "
              "snapshot (sanity check on the snapshot helper itself)",
              snapshot() != before_full_success)

        print("GET (repeat visit): the real prior answers are shown selected, "
              "not the empty placeholder")
        html_revisit = setup_wizard.render_simplefin_accounts()
        checking_block = select_block(html_revisit, "acc-checking")
        savings_block = select_block(html_revisit, "acc-savings")
        third_block = select_block(html_revisit, "acc-third")
        check("acc-checking's own existing mapping shows selected",
              checking_block is not None
              and f'value="{target_checking["code"]}" selected' in checking_block)
        check("acc-savings' exclusion shows selected",
              savings_block is not None
              and '__exclude__" selected' in savings_block)
        check("acc-third's newly-created account shows selected",
              third_block is not None
              and f'value="{new_card["code"]}" selected' in third_block)

        print("GET (repeat visit): a Ledger account claimed by ANOTHER "
              "account is disabled; the claiming account's OWN dropdown "
              "shows it selected and NOT disabled")
        check("target_checking is disabled in acc-savings' dropdown "
              "(claimed by acc-checking, a different account)",
              f'value="{target_checking["code"]}" disabled' in savings_block)
        check("target_checking is disabled in acc-third's dropdown too",
              f'value="{target_checking["code"]}" disabled' in third_block)
        check("target_checking is NOT disabled in acc-checking's OWN "
              "dropdown (it is that account's own current mapping)",
              f'value="{target_checking["code"]}" selected' in checking_block
              and f'value="{target_checking["code"]}" disabled'
              not in checking_block)

        # ---------------------------------------------------------------
        print("POST: missing an answer for one account -> error, ZERO writes")
        ACC4 = {"id": "acc-four", "name": "Fourth Account", "currency": "USD",
                "balance": "10.00"}
        ACC5 = {"id": "acc-five", "name": "Fifth Account", "currency": "USD",
                "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response([ACC4, ACC5])
        # Prime the rows via one GET, exactly like a real user would see
        # this pair before ever submitting anything.
        setup_wizard.render_simplefin_accounts()
        before_missing = snapshot()
        resp_missing = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-four": "__exclude__",
            # acc-five deliberately omitted entirely -- a forged/incomplete POST.
        })))
        body_missing = resp_missing.body.decode("utf-8")
        check("a missing answer re-renders with an error (not a redirect)",
              not isinstance(resp_missing, RedirectResponse))
        check("the error names the account still needing an answer",
              "Fifth Account" in body_missing
              and "still needs an answer" in body_missing)
        check("ZERO writes: every bank_accounts row and every Ledger "
              "account is byte-identical before/after the failed attempt",
              snapshot() == before_missing)

        # ---------------------------------------------------------------
        print("POST: two accounts choose the same existing ledger account "
              "(forged, bypassing client-side disabling) -> error naming "
              "both, zero writes")
        ACC6 = {"id": "acc-six", "name": "Sixth Account", "currency": "USD",
                "balance": "10.00"}
        ACC7 = {"id": "acc-seven", "name": "Seventh Account", "currency": "USD",
                "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response([ACC6, ACC7])
        setup_wizard.render_simplefin_accounts()
        shared_target = ledger.create_account(
            "Shared Collision Target", "asset", parent_code="1000")
        before_dup_code = snapshot()
        resp_dup_code = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-six": shared_target["code"],
            "mapping__acc-seven": shared_target["code"],
        })))
        body_dup_code = resp_dup_code.body.decode("utf-8")
        check("a same-submission duplicate ledger choice re-renders with "
              "an error (not a redirect)",
              not isinstance(resp_dup_code, RedirectResponse))
        check("the error names BOTH colliding accounts",
              "Sixth Account" in body_dup_code
              and "Seventh Account" in body_dup_code
              and "same ledger account" in body_dup_code)
        check("ZERO writes from the duplicate-ledger-choice attempt",
              snapshot() == before_dup_code)

        # ---------------------------------------------------------------
        print('POST: two accounts both request "__new__" with the same '
              "name -> a clear error, zero writes, no raw "
              "LedgerValidationError leaking through")
        ACC8 = {"id": "acc-eight", "name": "Eighth Account", "currency": "USD",
                "balance": "10.00"}
        ACC9 = {"id": "acc-nine", "name": "Ninth Account", "currency": "USD",
                "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response([ACC8, ACC9])
        setup_wizard.render_simplefin_accounts()
        before_dup_name = snapshot()
        resp_dup_name = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-eight": "__new__",
            "new_account_name__acc-eight": "Twin Named Account",
            "new_account_type__acc-eight": "asset",
            "mapping__acc-nine": "__new__",
            "new_account_name__acc-nine": "Twin Named Account",
            "new_account_type__acc-nine": "liability",
        })))
        body_dup_name = resp_dup_name.body.decode("utf-8")
        check("a same-submission duplicate __new__ name re-renders with "
              "an error (not a redirect)",
              not isinstance(resp_dup_name, RedirectResponse))
        check('the error clearly says both named their new account the '
              "same thing (not a raw/generic LedgerValidationError)",
              "Eighth Account" in body_dup_name
              and "Ninth Account" in body_dup_name
              and "named their new account" in body_dup_name
              and "already exists in your Chart of Accounts" not in body_dup_name)
        check("ZERO writes from the duplicate-new-name attempt (no Ledger "
              "account created, no bank_accounts row touched)",
              snapshot() == before_dup_name)
        check("no duplicate 'Twin Named Account' exists in the Chart",
              len([a for a in ledger.list_accounts()
                   if a["name"] == "Twin Named Account"]) == 0)

        # ---------------------------------------------------------------
        print('POST: one "__new__" name collides with a PRE-EXISTING Chart '
              "account (from a prior visit / anything else in the app), "
              "alongside another otherwise-valid __new__ request in the "
              "SAME batch -> rejected, ZERO new Chart rows at all "
              "(not even the otherwise-valid one)")
        ACC_P = {"id": "acc-p-existing-collision", "name": "P Account",
                 "currency": "USD", "balance": "10.00"}
        ACC_Q = {"id": "acc-q-existing-collision", "name": "Q Account",
                 "currency": "USD", "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_P, ACC_Q])
        setup_wizard.render_simplefin_accounts()
        pre_existing = ledger.create_account(
            "Pre-Existing Chart Name", "asset", parent_code="1000")
        before_existing_collision = snapshot()
        before_chart_count = len(ledger.list_accounts())
        resp_existing_collision = asyncio.run(setup_wizard.setup_simplefin_accounts(
            FakeRequest({
                "mapping__acc-p-existing-collision": "__new__",
                "new_account_name__acc-p-existing-collision": "Brand New Fresh Name",
                "new_account_type__acc-p-existing-collision": "asset",
                "mapping__acc-q-existing-collision": "__new__",
                # Case-insensitive collision against the pre-existing name,
                # deliberately typed with different casing.
                "new_account_name__acc-q-existing-collision":
                    "pre-existing chart name",
                "new_account_type__acc-q-existing-collision": "asset",
            })))
        body_existing_collision = resp_existing_collision.body.decode("utf-8")
        after_chart_count = len(ledger.list_accounts())
        check("a __new__ name colliding with a PRE-EXISTING Chart account "
              "re-renders with an error (not a redirect)",
              not isinstance(resp_existing_collision, RedirectResponse))
        check("the error is specific: names the colliding account and its "
              "clashing name",
              "Q Account" in body_existing_collision
              and "Pre-Existing Chart Name" in body_existing_collision
              and "already exists in your Chart of Accounts"
              in body_existing_collision)
        check("the Chart of Accounts gained ZERO rows -- including the "
              "OTHER, otherwise-valid __new__ request in the same batch "
              "('Brand New Fresh Name' was never created just because it "
              "happened to be validated/created before the colliding one)",
              after_chart_count == before_chart_count)
        check("'Brand New Fresh Name' specifically does not exist",
              not any(a["name"] == "Brand New Fresh Name"
                     for a in ledger.list_accounts()))
        check("ZERO writes overall (bank_accounts + full Chart) from this "
              "attempt, proving true all-or-nothing validation-before-any-"
              "write, not just 'the colliding one didn't get created'",
              snapshot() == before_existing_collision)

        # ---------------------------------------------------------------
        print("ledger.create_accounts() itself is atomic -- tested "
              "DIRECTLY, independent of any setup_wizard.py pre-check, to "
              "prove the TRANSACTION is the real safety net, not the "
              "pre-checks (which only exist for a nicer error message)")
        before_direct = len(ledger.list_accounts())
        direct_created = ledger.create_accounts(
            [("Direct Batch Alpha", "asset"), ("Direct Batch Beta", "liability")])
        check("a fully valid direct batch creates every account, in order",
              [a["name"] for a in direct_created]
              == ["Direct Batch Alpha", "Direct Batch Beta"]
              and direct_created[1]["type"] == "liability")
        check("...and the Chart grew by exactly that many rows",
              len(ledger.list_accounts()) == before_direct + 2)

        # The 150-char name from the reviewer's exact reproduction: the
        # SAME failure ledger.create_account() would raise for a single
        # call, but here it must roll back an EARLIER, otherwise-valid
        # spec in the same batch too -- something a per-item try/except
        # loop around create_account() cannot do, no matter how many
        # individual validation rules it tries to mirror.
        before_direct_150 = len(ledger.list_accounts())
        raised_150 = None
        try:
            ledger.create_accounts([
                ("Direct Valid Unrelated Name", "asset"),
                ("Y" * 150, "asset"),
            ])
        except ledger.LedgerValidationError as exc:
            raised_150 = exc
        check("a batch with a 150-char name raises LedgerValidationError",
              raised_150 is not None)
        after_direct_150 = len(ledger.list_accounts())
        check("the Chart gained ZERO rows -- the EARLIER, valid spec in "
              "the same call was rolled back along with the failing one",
              after_direct_150 == before_direct_150)
        check("'Direct Valid Unrelated Name' specifically was never "
              "left behind",
              not any(a["name"] == "Direct Valid Unrelated Name"
                     for a in ledger.list_accounts()))

        # A failure mode create_account() has never had a dedicated
        # pre-check for anywhere in this codebase (band exhaustion) --
        # proving the transaction covers a failure mode nobody wrote a
        # special case for, exactly the "retires the whole class of bug"
        # claim. Exhaust the liability band (2000) by direct SQL (cheap;
        # avoids actually calling create_account 99 times), then prove a
        # valid spec ahead of the exhausted one in the same batch is
        # still rolled back.
        with db.get_connection() as _conn:
            for _candidate in range(2010, 3000, 10):
                # INSERT OR IGNORE: an earlier test in this same shared
                # database run may already have claimed a code in this
                # band (e.g. "Business Credit Card" from the earlier
                # full-submission test) -- code is UNIQUE, so a blind
                # INSERT would raise on that one collision instead of
                # finishing the fill.
                _conn.execute(
                    "INSERT OR IGNORE INTO accounts (code, name, type, "
                    "parent_account_id, tax_category) VALUES (?, ?, ?, "
                    "NULL, NULL)",
                    (str(_candidate), f"Band Filler {_candidate}", "liability"))
        before_direct_band = len(ledger.list_accounts())
        raised_band = None
        try:
            ledger.create_accounts([
                ("Direct Valid Before Band Exhaustion", "asset"),
                ("Direct Doomed By Band Exhaustion", "liability"),
            ])
        except ledger.LedgerValidationError as exc:
            raised_band = exc
        check("a batch whose SECOND spec hits code-band exhaustion (a "
              "failure mode with no dedicated setup_wizard.py pre-check "
              "at all) still raises",
              raised_band is not None and "full" in str(raised_band).lower())
        after_direct_band = len(ledger.list_accounts())
        check("...and the Chart gained ZERO rows -- the FIRST, valid "
              "asset spec was rolled back too, even though nothing in "
              "setup_wizard.py or ledger.py specifically anticipated "
              "band exhaustion as a reason to reject it",
              after_direct_band == before_direct_band)

        print("...and create_account() (the ORIGINAL, singular function) "
              "still behaves exactly as before this refactor")
        solo_before = len(ledger.list_accounts())
        solo_account = ledger.create_account(
            "Direct Solo Untouched Behavior", "expense")
        check("a normal single create_account() call still succeeds and "
              "returns the created account dict",
              solo_account["name"] == "Direct Solo Untouched Behavior"
              and solo_account["type"] == "expense"
              and len(ledger.list_accounts()) == solo_before + 1)
        solo_dup_raised = None
        try:
            ledger.create_account("Direct Solo Untouched Behavior", "expense")
        except ledger.LedgerValidationError as exc:
            solo_dup_raised = exc
        check("a duplicate-name single create_account() call still "
              "raises the same message as before",
              solo_dup_raised is not None
              and "already exists in your Chart of Accounts"
              in str(solo_dup_raised))

        # ---------------------------------------------------------------
        print('POST: the reviewer\'s exact reproduction -- a valid __new__ '
              "name alongside a 150-character __new__ name, through the "
              "real setup_wizard.py screen end-to-end")
        ACC_LONG1 = {"id": "acc-long-name-1", "name": "Long1 Account",
                    "currency": "USD", "balance": "10.00"}
        ACC_LONG2 = {"id": "acc-long-name-2", "name": "Long2 Account",
                    "currency": "USD", "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_LONG1, ACC_LONG2])
        setup_wizard.render_simplefin_accounts()
        before_long_snapshot = snapshot()
        before_long_chart_count = len(ledger.list_accounts())
        long_name = "Z" * 150
        resp_long = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-long-name-1": "__new__",
            "new_account_name__acc-long-name-1": "Screen Valid Unrelated Name",
            "new_account_type__acc-long-name-1": "asset",
            "mapping__acc-long-name-2": "__new__",
            "new_account_name__acc-long-name-2": long_name,
            "new_account_type__acc-long-name-2": "asset",
        })))
        body_long = resp_long.body.decode("utf-8")
        after_long_chart_count = len(ledger.list_accounts())
        check("the oversized-name submission is rejected (not a redirect)",
              not isinstance(resp_long, RedirectResponse))
        check("Chart of Accounts row count is IDENTICAL before and after "
              "(reviewer's reproduction: was 32->33-and-stuck-at-33 before "
              "this fix; must now be N->N)",
              before_long_chart_count == after_long_chart_count)
        check("'Screen Valid Unrelated Name' -- the OTHER, otherwise-valid "
              "__new__ request in the same batch -- was never created",
              not any(a["name"] == "Screen Valid Unrelated Name"
                     for a in ledger.list_accounts()))
        check("ZERO writes overall (bank_accounts + full Chart)",
              snapshot() == before_long_snapshot)

        # ---------------------------------------------------------------
        print("Swap coverage: simple two-account swap in one submission "
              "(A gives up code X, B claims X, same submission)")
        ACC_SWAP_A = {"id": "acc-swap-a", "name": "Swap A Account",
                     "currency": "USD", "balance": "10.00"}
        ACC_SWAP_B = {"id": "acc-swap-b", "name": "Swap B Account",
                     "currency": "USD", "balance": "20.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_SWAP_A, ACC_SWAP_B])
        setup_wizard.render_simplefin_accounts()
        swap_target_1 = ledger.create_account(
            "Swap Target One", "asset", parent_code="1000")
        swap_target_2 = ledger.create_account(
            "Swap Target Two", "asset", parent_code="1000")
        # Baseline: A -> target 1, B -> target 2.
        baseline_swap = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-swap-a": swap_target_1["code"],
            "mapping__acc-swap-b": swap_target_2["code"],
        })))
        check("swap baseline submission succeeds",
              isinstance(baseline_swap, RedirectResponse))
        # The actual swap: A -> target 2 (B's old code), B -> target 1
        # (A's old code), in the SAME submission.
        swap_resp = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-swap-a": swap_target_2["code"],
            "mapping__acc-swap-b": swap_target_1["code"],
        })))
        check("a genuine two-account swap succeeds (not rejected as a "
              "false 'just claimed by another connection' collision)",
              isinstance(swap_resp, RedirectResponse)
              and swap_resp.status_code == 303)
        swap_rows = {row["account_id"]: row
                    for row in db.list_bank_accounts("simplefin")}
        check("account A now holds B's OLD target",
              swap_rows["acc-swap-a"]["ledger_account_id"]
              == swap_target_2["id"])
        check("account B now holds A's OLD target",
              swap_rows["acc-swap-b"]["ledger_account_id"]
              == swap_target_1["id"])

        # ---------------------------------------------------------------
        print("Swap coverage: three-account rotation in one submission "
              "(A->B's old code, B->C's old code, C->A's old code)")
        ACC_ROT_A = {"id": "acc-rot-a", "name": "Rot A Account",
                    "currency": "USD", "balance": "10.00"}
        ACC_ROT_B = {"id": "acc-rot-b", "name": "Rot B Account",
                    "currency": "USD", "balance": "20.00"}
        ACC_ROT_C = {"id": "acc-rot-c", "name": "Rot C Account",
                    "currency": "USD", "balance": "30.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_ROT_A, ACC_ROT_B, ACC_ROT_C])
        setup_wizard.render_simplefin_accounts()
        rot_target_a = ledger.create_account(
            "Rotation Target A", "asset", parent_code="1000")
        rot_target_b = ledger.create_account(
            "Rotation Target B", "asset", parent_code="1000")
        rot_target_c = ledger.create_account(
            "Rotation Target C", "asset", parent_code="1000")
        baseline_rot = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-rot-a": rot_target_a["code"],
            "mapping__acc-rot-b": rot_target_b["code"],
            "mapping__acc-rot-c": rot_target_c["code"],
        })))
        check("rotation baseline submission succeeds",
              isinstance(baseline_rot, RedirectResponse))
        rotation_resp = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-rot-a": rot_target_b["code"],
            "mapping__acc-rot-b": rot_target_c["code"],
            "mapping__acc-rot-c": rot_target_a["code"],
        })))
        check("a three-account rotation succeeds in one submission",
              isinstance(rotation_resp, RedirectResponse)
              and rotation_resp.status_code == 303)
        rot_rows = {row["account_id"]: row
                   for row in db.list_bank_accounts("simplefin")}
        check("A now holds B's old target, B now holds C's old target, C "
              "now holds A's old target",
              rot_rows["acc-rot-a"]["ledger_account_id"] == rot_target_b["id"]
              and rot_rows["acc-rot-b"]["ledger_account_id"] == rot_target_c["id"]
              and rot_rows["acc-rot-c"]["ledger_account_id"] == rot_target_a["id"])

        # ---------------------------------------------------------------
        print("Swap coverage: a swap combined with an exclusion in the "
              "same submission (A excludes itself, freeing its code for B)")
        ACC_SWAPEX_A = {"id": "acc-swapex-a", "name": "SwapEx A Account",
                        "currency": "USD", "balance": "10.00"}
        ACC_SWAPEX_B = {"id": "acc-swapex-b", "name": "SwapEx B Account",
                        "currency": "USD", "balance": "20.00"}
        # B listed BEFORE A deliberately: account_ids (and so the final
        # write loop's order) follows live_accounts' order, and
        # exclude_bank_account happens to release its own code as a side
        # effect. If A were processed first, B's claim would incidentally
        # succeed with no pre-clear step at all, masking the very thing
        # this test exists to prove. Listing B first forces B's claim to
        # run BEFORE A's exclusion has run -- so it can only succeed if
        # the pre-clear step actually released A's code in advance.
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_SWAPEX_B, ACC_SWAPEX_A])
        setup_wizard.render_simplefin_accounts()
        swapex_target_1 = ledger.create_account(
            "SwapEx Target One", "asset", parent_code="1000")
        swapex_target_2 = ledger.create_account(
            "SwapEx Target Two", "asset", parent_code="1000")
        baseline_swapex = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-swapex-a": swapex_target_1["code"],
            "mapping__acc-swapex-b": swapex_target_2["code"],
        })))
        check("swap+exclude baseline submission succeeds",
              isinstance(baseline_swapex, RedirectResponse))
        swapex_resp = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-swapex-a": "__exclude__",
            "mapping__acc-swapex-b": swapex_target_1["code"],
        })))
        check("excluding A while B claims A's freed-up code in the same "
              "submission succeeds",
              isinstance(swapex_resp, RedirectResponse)
              and swapex_resp.status_code == 303)
        swapex_rows = {row["account_id"]: row
                      for row in db.list_bank_accounts("simplefin")}
        check("A is now excluded, with no ledger_account_id",
              swapex_rows["acc-swapex-a"]["mapping_state"] == "excluded"
              and swapex_rows["acc-swapex-a"]["ledger_account_id"] is None)
        check("B now holds A's freed-up target",
              swapex_rows["acc-swapex-b"]["ledger_account_id"]
              == swapex_target_1["id"])

        # ---------------------------------------------------------------
        print("Stale/vanished account: a SimpleFIN account that stops "
              "being returned no longer blocks its old code from a live "
              "account, and clear_bank_ledger_account (not "
              "exclude_bank_account) is what releases it")
        ACC_STALE = {"id": "acc-stale-vanishing", "name": "Stale Account",
                    "currency": "USD", "balance": "10.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response([ACC_STALE])
        setup_wizard.render_simplefin_accounts()
        stale_target = ledger.create_account(
            "Stale Claim Target", "asset", parent_code="1000")
        stale_map_resp = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-stale-vanishing": stale_target["code"],
        })))
        check("the soon-to-vanish account's initial mapping succeeds",
              isinstance(stale_map_resp, RedirectResponse))

        # Now SimpleFIN stops returning acc-stale-vanishing entirely (a
        # different live account shows up instead) -- simulating the
        # institution disconnecting it or the person closing it.
        ACC_LIVE_AFTER_VANISH = {"id": "acc-live-after-vanish",
                                 "name": "Live After Vanish Account",
                                 "currency": "USD", "balance": "5.00"}
        simplefin.requests.get = lambda *a, **k: accounts_response(
            [ACC_LIVE_AFTER_VANISH])
        html_after_vanish = setup_wizard.render_simplefin_accounts()
        live_block = select_block(html_after_vanish, "acc-live-after-vanish")
        check("the vanished account's old target is NOT disabled in a "
              "live account's dropdown (it's claimable again)",
              live_block is not None
              and f'value="{stale_target["code"]}" disabled' not in live_block)
        stale_row_before_claim = next(
            row for row in db.list_bank_accounts("simplefin")
            if row["account_id"] == "acc-stale-vanishing")
        check("the vanished account's own row is untouched by the GET "
              "render alone (still mapped, mapping_state unchanged)",
              stale_row_before_claim["ledger_account_id"] == stale_target["id"]
              and stale_row_before_claim["mapping_state"] == "")

        reclaim_resp = asyncio.run(setup_wizard.setup_simplefin_accounts(FakeRequest({
            "mapping__acc-live-after-vanish": stale_target["code"],
        })))
        check("a live account can successfully claim the vanished "
              "account's old target",
              isinstance(reclaim_resp, RedirectResponse)
              and reclaim_resp.status_code == 303)
        live_row_after_claim = next(
            row for row in db.list_bank_accounts("simplefin")
            if row["account_id"] == "acc-live-after-vanish")
        stale_row_after_claim = next(
            row for row in db.list_bank_accounts("simplefin")
            if row["account_id"] == "acc-stale-vanishing")
        check("the live account now holds the target",
              live_row_after_claim["ledger_account_id"] == stale_target["id"])
        check("the vanished account's ledger_account_id was released to "
              "NULL",
              stale_row_after_claim["ledger_account_id"] is None)
        check("the vanished account's mapping_state was left exactly as "
              "it was ('') -- NOT rewritten to 'excluded', proving "
              "clear_bank_ledger_account (not exclude_bank_account) was "
              "used to release it",
              stale_row_after_claim["mapping_state"] == "")

        # ---------------------------------------------------------------
        print("Carried-forward fix: excluding a Plaid row blocks "
              "ensure_bank_ledger_account's auto-mapping path")
        db.upsert_bank_account({
            "source": "plaid", "account_id": "plaid-excluded-test",
            "name": "Plaid Test Checking", "official_name": None,
            "type": "depository", "subtype": "checking", "mask": "4321",
            "synced_at": "2026-08-21T00:00:00Z",
        })
        excluded_ok = db.exclude_bank_account("plaid", "plaid-excluded-test")
        check("exclude_bank_account succeeds on the Plaid row",
              excluded_ok is True)
        remap_result = db.ensure_bank_ledger_account(
            "plaid", "plaid-excluded-test")
        check("ensure_bank_ledger_account now returns None for an excluded "
              "Plaid row, instead of silently re-mapping it",
              remap_result is None)
        excluded_row = next(
            row for row in db.list_bank_accounts("plaid")
            if row["account_id"] == "plaid-excluded-test")
        check("the excluded row's ledger_account_id stays NULL after the "
              "ensure_bank_ledger_account call (proves nothing was created "
              "or attached behind the scenes)",
              excluded_row["ledger_account_id"] is None
              and excluded_row["mapping_state"] == "excluded")

        print("...and a NON-excluded Plaid row is unaffected -- the fix "
              "changes behavior for excluded rows only")
        db.upsert_bank_account({
            "source": "plaid", "account_id": "plaid-normal-test",
            "name": "Plaid Normal Checking", "official_name": None,
            "type": "depository", "subtype": "checking", "mask": "8765",
            "synced_at": "2026-08-21T00:00:00Z",
        })
        normal_result = db.ensure_bank_ledger_account(
            "plaid", "plaid-normal-test")
        check("a non-excluded Plaid row still auto-maps exactly as before",
              normal_result is not None)
        normal_row = next(
            row for row in db.list_bank_accounts("plaid")
            if row["account_id"] == "plaid-normal-test")
        check("...and its ledger_account_id is now set to that mapping",
              normal_row["ledger_account_id"] == normal_result)

    finally:
        simplefin.requests.get = original_get
        simplefin.requests.post = original_post

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN account-mapping "
              "screen checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN account-mapping screen "
          "checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
