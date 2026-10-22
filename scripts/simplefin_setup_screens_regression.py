"""Regression coverage for the first two screens of the dedicated
/setup/simplefin flow (Task 2 of the SimpleFIN feature).

Covers:
- Screen 1 (GET /setup/simplefin): the cost/consent gate for Sub-step A
  (sign up + connect a bank on SimpleFIN's own site). Must disclose the
  $15/yr cost visibly (no collapsed <details>) and must NEVER mention the
  token-creation URL.
- POST /setup/simplefin/bank-connected: advances the stage with no network
  call, then redirects to Screen 2.
- Screen 2 (GET /setup/simplefin/token): Sub-step B (create the one-use
  setup token) + the paste. Must redirect back to Screen 1 if hit before
  Screen 1 is confirmed. Once gated open, it's the one place the
  token-creation link belongs.
- POST /setup/simplefin/token: the actual exchange, gated on
  exchange_setup_token() -> test_connection() -> secrets_store, with the
  raw token AND its decoded/derived forms (the claim URL, the Access
  URL's embedded credentials) never leaking into a stored value, an
  error banner, or a redirect target.
- Revisiting Screen 2 once the stage has already reached "mapping" shows
  an "already connected" notice instead of re-offering the paste form
  (which would only invite burning a second, unnecessary setup token),
  and a resubmitted POST at that stage is blocked before any network call.
- The ordered SIMPLEFIN_STAGES scheme itself: index lookup, fail-closed
  behavior for an unrecognized stage value, and that
  _simplefin_advance_stage only ever moves forward.

Adapter contract tests (token exchange logic, account normalization) live
in scripts/simplefin_adapter_regression.py; step-3 registration/branching
lives in scripts/simplefin_registration_regression.py. This script is the
route-level companion for the two new screens.
"""

import asyncio
import base64
import os
import re
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-screens-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import banks  # noqa: E402
from banks import simplefin  # noqa: E402
import db  # noqa: E402
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


class Response:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


def no_network_post(*_args, **_kwargs):
    raise AssertionError("no network call should have been made")


def no_network_get(*_args, **_kwargs):
    raise AssertionError("no network call should have been made")


# Fixtures used to prove the raw token and the exchanged Access URL never
# leak anywhere they shouldn't. Each carries an obviously-fake secret
# marker so a substring match is unambiguous.
FIXTURE_CLAIM_URL = "https://bridge.simplefin.test/simplefin/claim/topsecretclaim"
FIXTURE_TOKEN = base64.b64encode(FIXTURE_CLAIM_URL.encode()).decode()
FIXTURE_ACCESS_URL = "https://user:topsecretpass@api.simplefin.test/simplefin"

ACCOUNTS_PAYLOAD_OK = {
    "accounts": [
        {"id": "checking", "name": "Checking", "currency": "USD",
         "balance": "100.00"},
    ],
}


def secret_stored():
    return secrets_store.get_secret("bank.simplefin.access_url")


def run():
    db.init_db()
    original_post = simplefin.requests.post
    original_get = simplefin.requests.get

    try:
        print("Stage default")
        check("stage defaults to intro when unset",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_INTRO)

        print("Screen 1: cost/consent gate")
        html1 = setup_wizard.render_simplefin_intro()
        check("cost is disclosed visibly ($15/year)", "$15/year" in html1)
        # The page's shared <style> block always mentions <details> in a CSS
        # comment/selector (details.disclosure) — strip it before checking
        # for an actual collapsed-disclosure ELEMENT in the rendered body.
        body_only1 = re.sub(r"<style[^>]*>.*?</style>", "", html1,
                            flags=re.S | re.I)
        check("cost disclosure is not inside a collapsed <details> element",
              "<details" not in body_only1.lower())
        check("cost lives in an open callout info div",
              '<div class="callout info"><strong>SimpleFIN costs $15/year.'
              in html1)
        check("Screen 1 links to BRIDGE_URL (Sub-step A)",
              simplefin.BRIDGE_URL in html1)
        check("Screen 1 contains ZERO mention of the token-creation URL",
              simplefin.CREATE_URL not in html1
              and "bridge.simplefin.org/simplefin/create" not in html1)
        check("Screen 1 has a confirmation control, not a live check",
              'action="/setup/simplefin/bank-connected"' in html1
              and "continue" in html1.lower())
        check("Screen 1 back link returns to step 3",
              'href="/setup/step/3"' in html1)

        print("Screen 2 before Screen 1 is confirmed")
        simplefin.requests.post = no_network_post
        simplefin.requests.get = no_network_get
        gated = setup_wizard.setup_simplefin_token_screen()
        check("Screen 2 redirects back to Screen 1 when ungated",
              isinstance(gated, RedirectResponse)
              and gated.status_code == 303
              and gated.headers.get("location", "").startswith("/setup/simplefin")
              and "/setup/simplefin/token" not in gated.headers.get("location", ""))
        post_gated = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        check("POST to the token exchange also redirects back when ungated",
              isinstance(post_gated, RedirectResponse)
              and post_gated.status_code == 303)
        check("nothing was stored by the ungated POST attempt",
              secret_stored() is None)

        print("POST /setup/simplefin/bank-connected: the gate advance")
        response = setup_wizard.setup_simplefin_bank_connected()
        check("bank-connected makes no network call and returns a 303",
              response.status_code == 303)
        check("bank-connected redirects to the token screen",
              response.headers.get("location") == "/setup/simplefin/token")
        check("stage advanced to 'token'",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_TOKEN)

        print("Screen 2 once gated open")
        html2 = setup_wizard.setup_simplefin_token_screen()
        check("Screen 2 renders (a string, not a redirect) once gated",
              isinstance(html2, str))
        check("Screen 2 is the one screen with the token-creation link",
              simplefin.CREATE_URL in html2)
        check("Screen 2 has a paste field for the setup token",
              'name="setup_token"' in html2 and "<textarea" in html2)
        check("Screen 2 back link returns to Screen 1",
              'href="/setup/simplefin"' in html2)

        print("POST /setup/simplefin/token: empty token")
        before = secret_stored()
        resp_empty = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": "   "})))
        rendered_empty = resp_empty.body.decode("utf-8")
        check("empty token re-renders Screen 2 with an error",
              "Paste your SimpleFIN setup token." in rendered_empty)
        check("nothing stored for an empty token", secret_stored() == before)
        check("stage unchanged by the empty-token attempt",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_TOKEN)
        # Nothing about this fixture data is even in scope for an empty
        # submission, but assert it anyway: any future change that logged
        # or echoed "the last thing we tried" onto this page would show up
        # here immediately, on every failure branch, not just the ones
        # that happen to exercise a real fixture today.
        check("no fixture secret leaks onto the empty-token error page",
              FIXTURE_TOKEN not in rendered_empty
              and "topsecretclaim" not in rendered_empty
              and FIXTURE_ACCESS_URL not in rendered_empty
              and "topsecretpass" not in rendered_empty)

        print("POST /setup/simplefin/token: invalid (non-base64) token")
        simplefin.requests.post = no_network_post
        resp_bad = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": "not-valid-base64-!!!"})))
        rendered_bad = resp_bad.body.decode("utf-8")
        check("the adapter's own base64 error surfaces verbatim",
              "not valid base64" in rendered_bad)
        check("nothing stored for an invalid token", secret_stored() == before)
        check("no fixture secret leaks onto the invalid-token error page",
              FIXTURE_TOKEN not in rendered_bad
              and "topsecretclaim" not in rendered_bad
              and FIXTURE_ACCESS_URL not in rendered_bad
              and "topsecretpass" not in rendered_bad)

        print("POST /setup/simplefin/token: simulated 403 (already claimed)")
        simplefin.requests.post = lambda *_a, **_k: Response(status=403)
        resp_403 = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        rendered_403 = resp_403.body.decode("utf-8")
        check("the adapter's claimed-token recovery message surfaces",
              "disable it" in rendered_403 and "fresh token" in rendered_403)
        check("nothing stored after a 403 claim rejection",
              secret_stored() == before)
        # The raw base64 token AND its decoded claim URL are both the
        # one-use secret — decoding it is trivial, so checking only the
        # encoded form is not enough. "topsecretclaim" is the marker baked
        # into FIXTURE_CLAIM_URL, which FIXTURE_TOKEN encodes.
        check("the raw token never appears in the 403 error page",
              FIXTURE_TOKEN not in rendered_403)
        check("the DECODED claim URL never appears in the 403 error page",
              FIXTURE_CLAIM_URL not in rendered_403
              and "topsecretclaim" not in rendered_403)

        print("POST /setup/simplefin/token: exchange OK, test_connection fails")
        simplefin.requests.post = lambda *_a, **_k: Response(text=FIXTURE_ACCESS_URL)
        simplefin.requests.get = lambda *_a, **_k: Response(status=403)
        resp_testfail = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        rendered_testfail = resp_testfail.body.decode("utf-8")
        check("a working-exchange-but-failing-test result surfaces an error",
              "Reconnect it with a fresh setup token." in rendered_testfail)
        check("nothing stored when test_connection fails",
              secret_stored() == before)
        check("the raw token never appears in the test-connection failure page",
              FIXTURE_TOKEN not in rendered_testfail)
        # The full Access URL AND its embedded basic-auth credentials are
        # both sensitive — "topsecretpass" is the marker baked into
        # FIXTURE_ACCESS_URL's password, checked on its own in case a
        # future change extracts and displays just the credentials rather
        # than the whole URL.
        check("the Access URL never appears in the test-connection failure page",
              FIXTURE_ACCESS_URL not in rendered_testfail)
        check("the embedded Access URL credentials never appear in the "
              "test-connection failure page",
              "topsecretpass" not in rendered_testfail)

        print("Full success")
        before_active_bank = db.get_setting(banks.ACTIVE_BANK_KEY)
        before_cash_accounts = db.get_setting("cash_accounts")
        simplefin.requests.post = lambda *_a, **_k: Response(text=FIXTURE_ACCESS_URL)
        simplefin.requests.get = _ok_get
        resp_ok = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        check("success is a 303 redirect", resp_ok.status_code == 303)
        location = resp_ok.headers.get("location", "")
        check("access_url IS stored via secrets_store under the right name",
              secret_stored() == FIXTURE_ACCESS_URL)
        check("ACTIVE_BANK_KEY is NOT set by this task's code",
              db.get_setting(banks.ACTIVE_BANK_KEY) == before_active_bank)
        check("cash_accounts is NOT touched by this task's code",
              db.get_setting("cash_accounts") == before_cash_accounts)
        check("stage advanced to 'mapping'",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
        check("the redirect message reports the right account count",
              "1 USD account(s)" in location or "1%20USD%20account" in location)
        check("the redirect target does not leak the raw token",
              FIXTURE_TOKEN not in location
              and "topsecretclaim" not in location)
        check("the redirect target does not leak the Access URL",
              FIXTURE_ACCESS_URL not in location
              and "topsecretpass" not in location)

        print("Screen 2 revisited after success: no re-invitation to burn a token")
        already = setup_wizard.setup_simplefin_token_screen()
        check("stage is now index >= mapping",
              setup_wizard._simplefin_stage_index(setup_wizard.simplefin_stage())
              >= setup_wizard._simplefin_stage_index(setup_wizard.SIMPLEFIN_STAGE_MAPPING))
        check("a mapping-stage GET renders (not a redirect)",
              isinstance(already, str))
        check("the already-connected notice, not the paste form, is shown",
              'name="setup_token"' not in already
              and "<textarea" not in already
              and "Already connected" in already)
        check("the already-connected notice does not send the user back to "
              "the token-creation link (nothing to paste anymore)",
              simplefin.CREATE_URL not in already)
        simplefin.requests.post = no_network_post
        simplefin.requests.get = no_network_get
        resubmit = asyncio.run(setup_wizard.setup_simplefin_token(
            FakeRequest({"setup_token": FIXTURE_TOKEN})))
        check("resubmitting the token form post-success makes no network "
              "call and does not process a second exchange",
              isinstance(resubmit, RedirectResponse)
              and resubmit.status_code == 303)
        check("the stored access_url is unchanged by the blocked resubmit",
              secret_stored() == FIXTURE_ACCESS_URL)

        print("Ordered stage scheme: index lookup, monotonic advance, fail-closed")
        check("known stages resolve to their list position",
              setup_wizard._simplefin_stage_index("intro") == 0
              and setup_wizard._simplefin_stage_index("token") == 1
              and setup_wizard._simplefin_stage_index("mapping") == 2)
        check("an unrecognized/corrupt stage value fails closed to index 0",
              setup_wizard._simplefin_stage_index("some-future-stage-v7") == 0
              and setup_wizard._simplefin_stage_index("") == 0
              and setup_wizard._simplefin_stage_index(None) == 0)
        check("current stage is 'mapping' going into the monotonicity check",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
        setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_INTRO)
        check("_simplefin_advance_stage refuses to move the stage BACKWARD",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
        setup_wizard._simplefin_advance_stage(setup_wizard.SIMPLEFIN_STAGE_MAPPING)
        check("re-advancing to the same stage is a harmless no-op",
              setup_wizard.simplefin_stage() == setup_wizard.SIMPLEFIN_STAGE_MAPPING)
        check("_require_simplefin_stage is satisfied for stages already reached",
              setup_wizard._require_simplefin_stage(setup_wizard.SIMPLEFIN_STAGE_TOKEN)
              is None
              and setup_wizard._require_simplefin_stage(
                  setup_wizard.SIMPLEFIN_STAGE_MAPPING) is None)
        check("_require_simplefin_stage redirects for a stage not yet reached",
              isinstance(setup_wizard._require_simplefin_stage("cutover"),
                         RedirectResponse))

    finally:
        simplefin.requests.post = original_post
        simplefin.requests.get = original_get

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN setup-screens checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN setup-screens checks passed.")
    return 0


def _ok_get(*_args, **_kwargs):
    return _OkResponse()


class _OkResponse:
    status_code = 200
    text = ""

    def json(self):
        return ACCOUNTS_PAYLOAD_OK


if __name__ == "__main__":
    raise SystemExit(run())
