"""Regression coverage for the durable Feature C contractor handoff contract."""

import os
import sys
import tempfile
from pathlib import Path


DATA_DIR = tempfile.TemporaryDirectory(
    prefix="klearing-feature-c-handoff-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = DATA_DIR.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from backend.services import contractor_handoff  # noqa: E402


def check(label, condition):
    if condition:
        print(f"PASS  {label}")
        return 0
    print(f"FAIL  {label}")
    return 1


def main():
    failures = 0
    original_candidates = app.contractor_candidates
    original_merge = app._merge_gusto_contractors
    try:
        app.contractor_candidates = lambda: ([{
            "vendor": "Example Contractor", "total": 750.0,
            "count": 2, "categories": "contract_labor",
        }], 2026)
        app._merge_gusto_contractors = lambda rows: (rows, "")
        html = app.render_contractors()
        failures += check(
            "candidate tracker exposes the shared positive handoff status",
            contractor_handoff.checklist_status(1) in html
            and contractor_handoff.TRACKER_PATH in html
            and contractor_handoff.ELIGIBILITY_NOTE.lower() in html.lower(),
        )
        failures += check(
            "candidate tracker does not claim eligibility",
            "eligibility not verified" in html.lower()
            and "1099-nec eligibility" in html.lower(),
        )

        app.contractor_candidates = lambda: ([], 2026)
        empty_html = app.render_contractors()
        failures += check(
            "empty tracker state remains honest",
            contractor_handoff.checklist_status(0) in empty_html
            and contractor_handoff.TRACKER_PATH in empty_html,
        )
    finally:
        app.contractor_candidates = original_candidates
        app._merge_gusto_contractors = original_merge

    spec_path = (Path(__file__).resolve().parent.parent / "docs" /
                 "superpowers" / "specs" /
                 "2026-08-05-feature-c-tax-handoff.md")
    spec = spec_path.read_text(encoding="utf-8")
    failures += check(
        "handoff requirements are durable in the repository",
        "/tools/contractors" in spec
        and "Candidates identified; eligibility not verified" in spec
        and "not tracked" in spec,
    )

    if failures:
        print(f"FAIL: {failures} Feature C handoff check(s) failed.")
        return 1
    print("PASS: Feature C contractor handoff contract is durable and green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
