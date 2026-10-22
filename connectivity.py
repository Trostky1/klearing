"""
Shared classification of "Test Connection" failures.

Every integration's test path funnels errors through here so the user
always gets the same, specific guidance: an auth problem points at the
credential; a connection-level problem gets the network-blocking hint
(ad-blockers and DNS privacy tools silently break API calls, which
cost real debugging time in the original setup).
"""

import requests
from datetime import datetime, timezone

import paths

NETWORK_HINT = (
    "If this looks correct but still fails, a browser extension, "
    "ad-blocker, or DNS privacy tool on your network may be silently "
    "blocking the request. Try disabling ad-blockers or testing from "
    "a different network."
)


RATE_LIMIT_MESSAGE = "rate limited by {service}, will retry next run"


class RateLimitError(RuntimeError):
    """A remote service asked Klearing to wait for the next scheduled run."""

    def __init__(self, service):
        self.service = service
        super().__init__(RATE_LIMIT_MESSAGE.format(service=service))


def log_rate_limit(service):
    """Record the concise, actionable 429 message in error.log."""
    message = RATE_LIMIT_MESSAGE.format(service=service)
    try:
        with open(paths.log_path("error"), "a", encoding="utf-8") as log_file:
            stamp = datetime.now(timezone.utc).isoformat()
            log_file.write(f"{stamp} {message}\n")
    except OSError:
        pass
    return message


def raise_for_rate_limit(service, status_code):
    """Raise the shared error for an HTTP 429, with one error-log entry."""
    if status_code == 429:
        log_rate_limit(service)
        raise RateLimitError(service)


def rate_limit_message(service):
    """Log and return the user-facing next-run message."""
    return log_rate_limit(service)


class TestResult:
    """Outcome of one connection test, ready to render as JSON."""

    def __init__(self, ok, message, hint=None):
        self.ok = ok
        self.message = message
        self.hint = hint

    def as_dict(self):
        return {"ok": self.ok, "message": self.message, "hint": self.hint}


def ok(message):
    return TestResult(True, message)


def fail(message, hint=None):
    return TestResult(False, message, hint)


def is_connection_error(exc):
    """True for failures that happened before any HTTP response —
    DNS, refused/reset connections, TLS, timeouts."""
    return isinstance(exc, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    ))


def from_exception(service_name, exc):
    """A TestResult for an unexpected exception during a live test."""
    if isinstance(exc, RateLimitError):
        return fail(str(exc))
    if is_connection_error(exc):
        return fail(
            f"Could not reach {service_name} at all (the request never "
            f"got a response).",
            hint=NETWORK_HINT,
        )
    return fail(f"{service_name} test failed: {exc}")
