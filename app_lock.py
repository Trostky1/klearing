"""
The app password: a lock on Klearing's web pages, nothing more.

It exists so that someone else using this computer can't open
localhost and browse your finances or approve transactions. It is
deliberately NOT an encryption key — credentials are protected by the
OS credential store (see secrets_store.py) — so the unattended daily
automation never needs it, and resetting it never loses data.

The password is never stored: a PBKDF2-SHA256 hash (random salt,
600k iterations) goes in the database. Sessions are random tokens
held in this process's memory only, handed to the browser as an
HttpOnly cookie; restarting the app locks it again.
"""

import hashlib
import hmac
import os
import secrets
import time

import db

SALT_KEY = "app_lock_salt"
HASH_KEY = "app_lock_hash"
ITERATIONS_KEY = "app_lock_iterations"

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8

SESSION_COOKIE = "klearing_session"

_sessions = set()

# Simple brute-force brake for /unlock: after MAX_FAILURES consecutive
# wrong passwords, refuse attempts for LOCKOUT_SECONDS. In-memory only —
# a restart clears it, which is fine for a localhost lock whose job is
# stopping casual walk-ups, not nation-states.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30
_failed_attempts = 0
_locked_until = 0.0


def locked_out():
    """Seconds remaining in the lockout, or 0 when attempts are allowed."""
    remaining = _locked_until - time.monotonic()
    return max(0, int(remaining) + 1) if remaining > 0 else 0


def note_failed_attempt():
    global _failed_attempts, _locked_until
    _failed_attempts += 1
    if _failed_attempts >= MAX_FAILURES:
        _locked_until = time.monotonic() + LOCKOUT_SECONDS
        _failed_attempts = 0


def note_successful_attempt():
    global _failed_attempts, _locked_until
    _failed_attempts = 0
    _locked_until = 0.0


class LockError(Exception):
    """Clear, user-facing app-lock failure."""


def _hash(password, salt, iterations):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )


def is_configured():
    db.init_db()
    return db.get_meta(HASH_KEY) is not None


def set_password(password, confirm_password):
    """First-time password creation (Setup step 1)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise LockError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if password != confirm_password:
        raise LockError("Passwords do not match.")
    salt = os.urandom(16)
    digest = _hash(password, salt, PBKDF2_ITERATIONS)
    db.init_db()
    db.set_meta(SALT_KEY, salt.hex())
    db.set_meta(HASH_KEY, digest.hex())
    db.set_meta(ITERATIONS_KEY, str(PBKDF2_ITERATIONS))


def verify(password):
    db.init_db()
    salt_hex = db.get_meta(SALT_KEY)
    hash_hex = db.get_meta(HASH_KEY)
    if not salt_hex or not hash_hex:
        return False
    iterations = int(db.get_meta(ITERATIONS_KEY) or PBKDF2_ITERATIONS)
    digest = _hash(password, bytes.fromhex(salt_hex), iterations)
    return hmac.compare_digest(digest, bytes.fromhex(hash_hex))


def change_password(current, new, confirm_new):
    if not verify(current):
        raise LockError("Current password is wrong.")
    set_password(new, confirm_new)
    revoke_all_sessions()


def issue_session():
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    return token


def session_valid(token):
    return bool(token) and token in _sessions


def revoke_session(token):
    _sessions.discard(token)


def revoke_all_sessions():
    _sessions.clear()
