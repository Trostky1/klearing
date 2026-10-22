"""
Failure logging and alerting for Klearing's scheduled runs.

Each step of a scheduled run executes inside run_step(): if it throws,
the full traceback is appended to error.log, a short readable summary
is collected, and the run CONTINUES with the remaining independent
steps instead of aborting. At the end, notify_failures() emails one
consolidated alert listing everything that failed.
"""

import traceback
from datetime import datetime, timezone

import email_utils
import connectivity
import paths
from config import APP_NAME

ERROR_LOG_PATH = paths.log_path("error")


def record_failure(step_name, exc):
    """Append the full traceback to error.log; return a short summary."""
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(f"{'=' * 60}\n{stamp}  step: {step_name}\n")
            log_file.write(traceback.format_exc())
            log_file.write("\n")
    except OSError:
        pass  # logging must never take the run down with it

    return f"{type(exc).__name__}: {exc}"


def run_step(step_name, fn, failures, default=None):
    """
    Run one step of a scheduled check. On an uncaught exception the
    error is logged + collected in `failures` and `default` is
    returned, so later steps still get a chance to run.
    """
    try:
        return fn()
    except Exception as exc:
        if isinstance(exc, connectivity.RateLimitError):
            summary = str(exc)
        else:
            summary = record_failure(step_name, exc)
        failures.append((step_name, summary))
        print(f"STEP FAILED [{step_name}]: {summary}")
        return default


def notify_failures(failures, context="Daily check"):
    """One consolidated failure email. Short messages only; the full
    tracebacks live in error.log."""
    lines = [
        f"{context} hit {len(failures)} error(s) this run. Steps that",
        "failed are listed below; everything else was still attempted.",
        "",
    ]
    for step_name, summary in failures:
        lines.append(f"  - {step_name}: {summary}")
    lines += [
        "",
        "Full tracebacks are on the Logs page (error.log).",
    ]

    subject = f"⚠️ {APP_NAME}: {context.title()} Failed"
    try:
        email_utils.send_email(subject, "\n".join(lines))
    except Exception as exc:
        # Even the alert email can fail (e.g. no network). Record it;
        # the heartbeat staying stale is the remaining signal.
        record_failure("Failure alert email", exc)
