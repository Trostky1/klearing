"""Offline regression coverage for the on-request continuity publication."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DATA = tempfile.TemporaryDirectory(
    prefix="klearing-continuity-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = DATA.name
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import continuity_artifact as artifact  # noqa: E402
import db  # noqa: E402
import ledger  # noqa: E402


def check(label, condition):
    print(f"PASS: {label}" if condition else f"FAIL: {label}")
    return 0 if condition else 1


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    failures = 0
    db.init_db()
    first_started = time.perf_counter()
    first = artifact.publish_if_needed()
    first_latency = time.perf_counter() - first_started
    first_hash = digest(first["path"])
    generation = first["generation"]
    overview = (generation / "index.html").read_text(encoding="utf-8")
    about = (generation / "ABOUT-THIS-BOOK.txt").read_text(encoding="utf-8")
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    failures += check("first request publishes a verified generation",
                      first["regenerated"] and manifest["format"] == "klearing-continuity-v1")
    failures += check("rendered output visibly discloses its freshness",
                      "Book record last updated:" in overview
                      and "reflects posted entries through" in overview
                      and "Generated timestamp:" in about)
    failures += check("output explains scope and local possession honestly",
                      "Pending or denied items" in overview
                      and "does not, by itself, prove whether any software has transmitted data" in overview)

    original_builder = artifact._build_generation
    artifact._build_generation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("clean request rebuilt the artifact"))
    try:
        cached_started = time.perf_counter()
        cached = artifact.publish_if_needed()
        cached_latency = time.perf_counter() - cached_started
    finally:
        artifact._build_generation = original_builder
    failures += check("clean request serves the exact cached start file",
                      not cached["regenerated"] and digest(cached["path"]) == first_hash)
    failures += check("cached request avoids rebuild latency",
                      cached_latency < first_latency or cached_latency < 0.1)

    accounts = {row["code"]: row for row in ledger.list_accounts()}
    ledger.create_journal_entry(
        "2026-08-19", "Continuity fixture sale", [
            {"account_id": accounts["1010"]["id"], "debit_amount": "29.87", "credit_amount": 0},
            {"account_id": accounts["4000"]["id"], "debit_amount": 0, "credit_amount": "29.87"},
        ], created_by="continuity-regression", source_reference="continuity:real-posted-fixture",
    )
    marker = artifact.dirty_marker()
    failures += check("canonical ledger mutation durably marks publication dirty", marker is not None)

    probe = subprocess.run(
        [sys.executable, "-c",
         "import continuity_artifact as c; print('DIRTY' if c.dirty_marker() else 'CLEAN')"],
        cwd=REPO_ROOT, env=os.environ.copy(), text=True, capture_output=True, check=False,
    )
    failures += check("dirty marker survives a fresh app process",
                      probe.returncode == 0 and probe.stdout.strip() == "DIRTY")

    second = artifact.publish_if_needed()
    second_overview = (second["generation"] / "index.html").read_text(encoding="utf-8")
    year_page = (second["generation"] / "years" / "2026.html").read_text(encoding="utf-8")
    failures += check("next explicit request regenerates and clears dirty marker",
                      second["regenerated"] and artifact.dirty_marker() is None)
    failures += check("actual readable HTML contains memo, source, amounts, and as-of date",
                      "Continuity fixture sale" in year_page
                      and "continuity:real-posted-fixture" in year_page
                      and "$29.87" in year_page
                      and "2026-08-19" in second_overview)

    ledger.create_journal_entry(
        "2026-08-20", "Failure preservation fixture", [
            {"account_id": accounts["1010"]["id"], "debit_amount": "1.00", "credit_amount": 0},
            {"account_id": accounts["4000"]["id"], "debit_amount": 0, "credit_amount": "1.00"},
        ], created_by="continuity-regression",
    )
    safe_hash = digest(second["path"])
    artifact._build_generation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("simulated publication failure"))
    try:
        try:
            artifact.publish_if_needed()
            failed_safely = False
        except artifact.ContinuityArtifactError:
            failed_safely = True
    finally:
        artifact._build_generation = original_builder
    failures += check("failed rebuild preserves prior complete copy and dirty state",
                      failed_safely and digest(second["path"]) == safe_hash
                      and artifact.dirty_marker() is not None)

    print(f"INFO: first-request publication latency (small fixture): {first_latency:.4f}s")
    print(f"INFO: clean cached-request latency: {cached_latency:.4f}s")
    if failures:
        print(f"FAIL: {failures} continuity artifact regression check(s) failed.")
        return 1
    print("PASS: all continuity artifact regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
