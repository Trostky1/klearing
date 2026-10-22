"""Regression checks for the live tax projection settings UI and save path."""

import asyncio
import gc
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path


DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-tax-settings-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = DATA_DIR.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import db  # noqa: E402
from backend.routers import tax as tax_router  # noqa: E402
from backend.services import tax_projection, tax_settings  # noqa: E402


class FakeRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def check(label, condition, detail=""):
    if condition:
        print(f"PASS  {label}")
        return 0
    print(f"FAIL  {label}" + (f" - {detail}" if detail else ""))
    return 1


def save(form):
    return asyncio.run(app.save_settings(
        FakeRequest(form),
        balance_threshold="", runway_days="", vendor_jump_threshold="",
        business_name="", sender_name="", cash_accounts=[],
    ))


def valid_form(**overrides):
    form = {
        "tax_business_type": "sole_prop",
        "tax_filing_status": "single",
        "tax_state": "IA",
        "tax_other_income": "0.00",
        app.ledger.ACCOUNTING_BASIS_KEY: "accrual",
    }
    form.update(overrides)
    return form


def main():
    failures = 0
    db.init_db()

    defaults = tax_settings.get()
    failures += check(
        "missing row uses the projection's explicit defaults",
        defaults == tax_settings.DEFAULTS,
        str(defaults),
    )

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, estimated_other_income, tax_year, business_type) "
            "VALUES (1, 'married_filing_jointly', 'IA', 123456, 2025, 's_corp')"
        )

    html = app.render_settings_page([])
    failures += check(
        "Settings names the fields as live projection inputs",
        "These settings change the live" in html
        and "not profile details saved for a future feature" in html,
    )
    failures += check(
        "all persisted live assumptions render truthfully",
        'value="s_corp" selected' in html
        and 'value="married_filing_jointly" selected' in html
        and 'value="IA" selected' in html
        and 'value="1234.56"' in html,
    )
    failures += check(
        "unsupported saved year is disclosed and 2026 stays read-only",
        "Saved tax year is unsupported" in html
        and '<span class="mono">2025</span>' in html
        and 'id="tax_year" value="2026"' in html
        and "readonly" in html,
    )
    failures += check(
        "Settings UI follows the no-inline-style guard",
        re.search(r"\sstyle\s*=", app._tax_settings_fields(), re.I) is None,
    )

    response = save(valid_form(
        tax_business_type="single_member_llc",
        tax_filing_status="head_of_household",
        tax_state="mn",
        tax_other_income="9876.54",
    ))
    persisted = tax_settings.get()
    failures += check(
        "valid save uses POST-303-banner and exact integer cents",
        response.status_code == 303
        and "msg=Settings%20saved." in response.headers.get("location", "")
        and persisted == {
            "filing_status": "head_of_household",
            "state": "MN",
            "estimated_other_income": 987654,
            "tax_year": 2026,
            "business_type": "single_member_llc",
        },
        f'{response.headers.get("location", "")} {persisted}',
    )
    failures += check(
        "projection API reads the same persisted settings row",
        tax_router._tax_settings() == persisted,
    )

    response = save({app.ledger.ACCOUNTING_BASIS_KEY: "cash"})
    failures += check(
        "an already-open legacy Settings form preserves live tax values",
        response.status_code == 303 and tax_settings.get() == persisted,
        str(tax_settings.get()),
    )

    statement = {
        "basis": "accrual", "net_income_cents": 10_000_000,
        "source_entry_count": 2,
    }
    non_iowa = tax_projection.projection(
        persisted, date(2026, 12, 31), statement)
    failures += check(
        "a non-Iowa selection omits state tax instead of guessing",
        non_iowa["taxes"]["iowa_income_tax_cents"] is None
        and non_iowa["state"]["available"] is False
        and any("Minnesota state tax is not available" in warning
                for warning in non_iowa["warnings"]),
    )

    before_invalid = dict(persisted)
    response = save(valid_form(
        tax_filing_status="not-a-status", tax_other_income="100.00"))
    failures += check(
        "unsupported filing status fails visibly without changing tax settings",
        response.status_code == 303
        and "err=Choose%20a%20supported%20filing%20status." in response.headers.get("location", "")
        and tax_settings.get() == before_invalid,
        response.headers.get("location", ""),
    )

    response = save(valid_form(tax_other_income="12.345"))
    failures += check(
        "sub-cent other income is rejected rather than rounded",
        response.status_code == 303
        and "at%20most%20two%20decimal%20places" in response.headers.get("location", "")
        and tax_settings.get() == before_invalid,
        response.headers.get("location", ""),
    )

    response = save(valid_form(tax_other_income="-1"))
    failures += check(
        "negative other income is rejected rather than silently clamped",
        response.status_code == 303
        and "zero%20or%20more" in response.headers.get("location", "")
        and tax_settings.get() == before_invalid,
        response.headers.get("location", ""),
    )

    if failures:
        print(f"FAIL: {failures} tax settings regression check(s) failed.")
        gc.collect()
        DATA_DIR.cleanup()
        return 1
    print("PASS: all tax settings regression checks passed.")
    gc.collect()
    DATA_DIR.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
