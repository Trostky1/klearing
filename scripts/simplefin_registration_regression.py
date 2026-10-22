"""Regression coverage for SimpleFIN's adapter registration and its step-3
Setup wizard branch.

SimpleFIN's one credential field (Access URL) cannot be typed in by a user —
it only exists after a server-side setup-token exchange. This script proves
the adapter is registered and reachable like any other bank, while the
generic Setup wizard's step 3 never renders that field as a paste box and
instead points the user at the dedicated (not-yet-built) /setup/simplefin
flow. It also proves Mercury and Plaid's step-3 rendering is unaffected.
"""

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path


_DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-simplefin-registration-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import banks  # noqa: E402
from banks import simplefin  # noqa: E402
import db  # noqa: E402
import secrets_store  # noqa: E402
import setup_wizard  # noqa: E402


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


def run():
    db.init_db()

    print("Registry")
    check("banks.get_adapter('simplefin') resolves to the real adapter",
          isinstance(banks.get_adapter("simplefin"), simplefin.SimpleFINAdapter))
    check("banks.all_adapters() includes simplefin",
          "simplefin" in {adapter.id for adapter in banks.all_adapters()})

    print("Docstring / comment accuracy")
    module_doc = simplefin.__doc__ or ""
    check("module docstring no longer claims the adapter is unregistered",
          "not registered yet" not in module_doc
          and "intentionally not registered" not in module_doc)
    check("module docstring points at the dedicated setup flow",
          "/setup/simplefin" in module_doc)
    credential_fields_source = inspect.getsource(
        simplefin.SimpleFINAdapter.credential_fields)
    check("credential_fields() comment no longer claims this field is "
          "simply 'not rendered yet' with no explanation of where it is",
          "This field is deliberately not rendered yet."
          not in credential_fields_source)

    print("Step 3 rendering")
    html = setup_wizard.render_step_bank()
    check("SimpleFIN option is offered in the bank dropdown",
          '<option value="simplefin"' in html)
    check("SimpleFIN gets its own fieldset panel",
          'id="bank-fields-simplefin"' in html)
    check("SimpleFIN renders zero credential input fields",
          'name="simplefin__access_url"' not in html)
    check("SimpleFIN renders no Test Connection wiring (nothing to test)",
          "bank-test-simplefin" not in html
          and "wireTest('bank-test-simplefin')" not in html)
    check("SimpleFIN's panel links to the dedicated setup flow",
          'href="/setup/simplefin"' in html)
    check("SimpleFIN's panel does not lead with the token-creation link "
          "(that belongs inside the dedicated flow, not step 3)",
          "bridge.simplefin.org" not in html)
    check("Continue button is hidden client-side when SimpleFIN is chosen "
          "(nothing on step 3 to Continue with — the link IS the action)",
          "bankSelect.value === 'simplefin' ? 'none' : ''" in html)

    print("Mercury and Plaid step 3 rendering is unchanged")
    check("Mercury still renders its API token paste box",
          'name="mercury__api_token"' in html
          and "wireTest('bank-test-mercury')" in html)
    check("Plaid still renders its Client ID / Secret paste boxes",
          'name="plaid__client_id"' in html
          and 'name="plaid__secret"' in html
          and "wireTest('bank-test-plaid')" in html)
    check("Plaid's environment field is still a select, not a text box",
          '<select id="in-plaid__environment" name="plaid__environment">' in html)

    print("Save path: adapter_id=simplefin redirects unconditionally")
    before_secret = secrets_store.get_secret("bank.simplefin.access_url")
    before_active_bank = db.get_setting(banks.ACTIVE_BANK_KEY)
    response = asyncio.run(setup_wizard.setup_save_bank(
        FakeRequest({"adapter_id": "simplefin",
                     "simplefin__access_url": "not a real access url"})))
    check("response is a 303 redirect", getattr(response, "status_code", None) == 303)
    check("redirect target is /setup/simplefin",
          response.headers.get("location") == "/setup/simplefin")
    check("no SimpleFIN secret was written by the redirect",
          secrets_store.get_secret("bank.simplefin.access_url") == before_secret)
    check("active bank setting is untouched by the redirect",
          db.get_setting(banks.ACTIVE_BANK_KEY) == before_active_bank)

    # Same outcome even with no extraneous fields at all, and even if a
    # SimpleFIN connection happens to already be configured — the redirect
    # must be unconditional, unlike Plaid's "only if not configured" branch.
    secrets_store.set_secret("bank.simplefin.access_url",
                             "https://user:pass@bridge.example/simplefin")
    response2 = asyncio.run(setup_wizard.setup_save_bank(
        FakeRequest({"adapter_id": "simplefin"})))
    check("redirect still fires when SimpleFIN is already configured",
          getattr(response2, "status_code", None) == 303
          and response2.headers.get("location") == "/setup/simplefin")
    check("active bank setting is still untouched even when configured",
          db.get_setting(banks.ACTIVE_BANK_KEY) == before_active_bank)

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN registration checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN registration checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
