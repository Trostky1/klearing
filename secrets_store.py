"""
Credential storage for Klearing. Replaces both the old .env files and
the old password-gated vault — every credential goes through here, no
exceptions.

How it works:
- A random Fernet master key is created once and stored in the
  operating system's credential store (Windows Credential Manager on
  Windows, Keychain on macOS, Secret Service on Linux) via the
  `keyring` library.
- Every credential value is encrypted with that key and kept in the
  SQLite database's `credentials` table.

Why this design (and not a password-gated vault):
- The daily 8am check runs unattended. A vault that needs a human to
  type a password every morning can't serve it; a key in the OS store
  can be read by a background task running as the same user with no
  prompt, while still being encrypted at rest and protected by the
  OS login.
- The OS stores hold small blobs reliably (Windows caps entries at
  2560 bytes), so large values like GA4 service-account JSON files go
  in SQLite encrypted, and only the 44-byte key lives in the OS store.

If no OS credential store is available (rare — some Linux setups),
the key falls back to a permission-restricted file next to the
database, and used_fallback_keyfile() lets the UI say so honestly.
"""

from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

import db
import paths
from config import APP_NAME

KEYRING_SERVICE = APP_NAME
LEGACY_KEYRING_ENTRY = "master-key"   # v0.1.0 used one shared entry
FALLBACK_KEY_FILENAME = "master.key"


def _keyring_entry():
    """The OS-store entry name, scoped to THIS data directory.

    v0.1.0 used a single machine-wide entry, which meant a second
    instance (a test run, a scratch profile) shared — and on cleanup
    could delete — the real install's key, silently orphaning its
    credentials. Scoping by data-dir hash makes instances independent.
    Legacy entries are still read and migrated by copy (the old entry
    is left in place so an old binary keeps working too)."""
    import hashlib
    digest = hashlib.sha256(
        str(paths.data_dir()).lower().encode("utf-8")
    ).hexdigest()[:12]
    return f"master-key-{digest}"

_master_key = None          # cached per process
_used_fallback = False


class SecretsError(RuntimeError):
    """Clear, user-facing credential-store failure. A RuntimeError
    subclass so web routes that already catch RuntimeError show the
    message instead of a 500."""


def _fallback_key_path():
    return paths.data_dir() / FALLBACK_KEY_FILENAME


def _load_or_create_fallback_key():
    global _used_fallback
    _used_fallback = True
    path = _fallback_key_path()
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    # Exclusive create with owner-only permissions from the first byte
    # (chmod-after-write would leave a readable window on Linux).
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as key_file:
        key_file.write(key)
    return key


def _get_master_key():
    """The Fernet master key, creating and storing it on first use."""
    global _master_key
    if _master_key is not None:
        return _master_key

    try:
        import keyring
        entry = _keyring_entry()
        stored = keyring.get_password(KEYRING_SERVICE, entry)
        if not stored:
            # Migrate a v0.1.0 shared entry by copy: this data dir
            # adopts the key under its own scoped name; the legacy
            # entry stays so nothing else that used it breaks.
            stored = keyring.get_password(KEYRING_SERVICE,
                                          LEGACY_KEYRING_ENTRY)
            if stored:
                keyring.set_password(KEYRING_SERVICE, entry, stored)
        if stored:
            _master_key = stored.encode("ascii")
        else:
            key = Fernet.generate_key()
            keyring.set_password(KEYRING_SERVICE, entry,
                                 key.decode("ascii"))
            _master_key = key
    except Exception:
        # No usable OS credential store right now. Reusing an existing
        # fallback key file is fine — but minting a brand-new key while
        # encrypted credentials already exist would just make them
        # permanently unreadable, so a TRANSIENT keyring outage must
        # fail loudly instead of quietly forking the key.
        db.init_db()
        if not _fallback_key_path().exists() and db.list_credentials():
            raise SecretsError(
                "The system credential store isn't reachable right now, "
                "and your saved credentials are encrypted under the key "
                "it holds. Sign out and back in (or restart), then try "
                "again."
            )
        _master_key = _load_or_create_fallback_key()

    return _master_key


def _fernet():
    """A Fernet for the master key, with corruption mapped to a clear
    user-facing error instead of a bare ValueError."""
    try:
        return Fernet(_get_master_key())
    except (ValueError, TypeError):
        raise SecretsError(
            "The stored master key is malformed and can't be used. "
            "Delete the credential-store entry (or the master.key file) "
            "and re-enter your credentials on the Connections page."
        )


def export_master_key():
    """The master key as text — for the encrypted backup bundle ONLY.
    Anyone holding this plus the database can read every credential."""
    return _get_master_key().decode("ascii")


def install_master_key(key_text):
    """Put a restored master key into the OS credential store (or the
    fallback file), replacing whatever is there. Used by backup
    restore; validates before touching anything."""
    global _master_key, _used_fallback
    key = (key_text or "").strip().encode("ascii")
    try:
        Fernet(key)
    except (ValueError, TypeError):
        raise SecretsError("The backup's encryption key is malformed — "
                           "the file may be corrupted.")
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, _keyring_entry(),
                             key.decode("ascii"))
    except Exception:
        path = _fallback_key_path()
        if path.exists():
            path.unlink()
        import os
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as key_file:
            key_file.write(key)
        _used_fallback = True
    _master_key = key


def used_fallback_keyfile():
    """True if the master key lives in a file instead of the OS store
    (so the UI can surface it). Only meaningful after first use."""
    return _used_fallback


def is_available():
    """Can we encrypt/decrypt at all? Initializes the key if needed."""
    try:
        _get_master_key()
        return True
    except Exception:
        return False


def set_secret(name, value, is_file=False):
    """Encrypt and store one credential, replacing any existing value.
    `value` may be str or bytes."""
    name = (name or "").strip()
    if not name:
        raise SecretsError("The credential needs a name.")
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not value:
        raise SecretsError("The credential value is empty.")
    encrypted = _fernet().encrypt(value)
    db.init_db()
    db.upsert_credential(
        name=name,
        encrypted_value=encrypted,
        is_file=is_file,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def get_secret_bytes(name):
    """Decrypted bytes for one credential, or None if not stored."""
    db.init_db()
    row = db.get_credential(name)
    if row is None:
        return None
    try:
        return _fernet().decrypt(row["encrypted_value"])
    except InvalidToken:
        raise SecretsError(
            f'Stored credential "{name}" could not be decrypted. The '
            f"master key in your system's credential store no longer "
            f"matches — re-enter this credential on the Connections page."
        )


def get_secret(name):
    """Decrypted text for one credential, or None if not stored."""
    raw = get_secret_bytes(name)
    return raw.decode("utf-8") if raw is not None else None


def has_secret(name):
    db.init_db()
    return db.get_credential(name) is not None


def delete_secret(name):
    db.init_db()
    return db.delete_credential(name) > 0


def list_secrets():
    """Names + metadata only — never decrypted values."""
    db.init_db()
    return db.list_credentials()
