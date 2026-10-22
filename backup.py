"""
Backup and restore for Klearing.

One backup file carries everything: the SQLite database (settings,
history, drafts, encrypted credentials) AND the master encryption key
from the OS credential store — without the key, restored credentials
would be unreadable ciphertext on a new machine. Because the key is
inside, the bundle itself is encrypted: a PBKDF2-derived Fernet key
from a backup password the user chooses at export time.

File format (.klearingbackup): an outer JSON envelope
  {"format": "klearing-backup-v1", "salt": hex, "iterations": N,
   "payload": <Fernet token>}
whose decrypted payload is JSON
  {"created_at", "app_version", "master_key", "db_b64"}.

Restore is a fresh-install path (an alternative to the Setup wizard):
it writes the database back, seeds the credential store with the
master key, and the restored app password takes over from there.
"""

import base64
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import db
import paths
import secrets_store
from config import APP_VERSION

BACKUP_FORMAT = "klearing-backup-v1"
KDF_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
FILE_EXTENSION = ".klearingbackup"


class BackupError(RuntimeError):
    """Clear, user-facing backup/restore failure."""


def _derive_key(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _clean_db_copy():
    """A consistent snapshot of the live database via SQLite's backup
    API (safe while the app is running), as bytes."""
    db.init_db()
    fd, tmp_name = tempfile.mkstemp(suffix=".db", dir=paths.data_dir())
    os.close(fd)
    try:
        source = sqlite3.connect(paths.db_path())
        try:
            destination = sqlite3.connect(tmp_name)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        with open(tmp_name, "rb") as tmp:
            return tmp.read()
    finally:
        os.unlink(tmp_name)


def create_backup(password, confirm_password):
    """Build the encrypted bundle. Returns (filename, file_bytes)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise BackupError(
            f"Backup password must be at least {MIN_PASSWORD_LENGTH} "
            f"characters — it protects every credential in the file."
        )
    if password != confirm_password:
        raise BackupError("Backup passwords do not match.")

    payload = json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "master_key": secrets_store.export_master_key(),
        "db_b64": base64.b64encode(_clean_db_copy()).decode("ascii"),
    }).encode("utf-8")

    salt = os.urandom(16)
    token = Fernet(_derive_key(password, salt, KDF_ITERATIONS)).encrypt(payload)
    envelope = json.dumps({
        "format": BACKUP_FORMAT,
        "salt": salt.hex(),
        "iterations": KDF_ITERATIONS,
        "payload": token.decode("ascii"),
    }, indent=1).encode("utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"klearing-backup-{stamp}{FILE_EXTENSION}", envelope


def restore_backup(file_bytes, password):
    """Decrypt and apply a backup: master key into the credential
    store, database onto disk. Returns the payload's created_at."""
    try:
        envelope = json.loads(file_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError(
            "That file isn't a Klearing backup — it should end in "
            f"{FILE_EXTENSION} and come from the Export backup button."
        )
    if not isinstance(envelope, dict) or envelope.get("format") != BACKUP_FORMAT:
        raise BackupError(
            "That file isn't a Klearing backup (or is from a newer "
            "version of the app than this one)."
        )
    try:
        salt = bytes.fromhex(envelope["salt"])
        iterations = int(envelope["iterations"])
        token = envelope["payload"].encode("ascii")
    except (KeyError, ValueError, TypeError):
        raise BackupError("The backup file is damaged (bad envelope).")

    try:
        payload = Fernet(_derive_key(password, salt, iterations)).decrypt(token)
    except InvalidToken:
        raise BackupError(
            "Wrong backup password (or the file is corrupted). This is "
            "the password you chose when exporting the backup — it may "
            "differ from your app password."
        )

    try:
        inner = json.loads(payload.decode("utf-8"))
        master_key = inner["master_key"]
        db_bytes = base64.b64decode(inner["db_b64"])
    except (ValueError, KeyError, TypeError):
        raise BackupError("The backup file is damaged (bad payload).")

    # Key first: if seeding the credential store fails, nothing on
    # disk has changed yet.
    secrets_store.install_master_key(master_key)
    paths.db_path().write_bytes(db_bytes)
    # The restored database may describe different canonical books.  Keep
    # any existing on-disk continuity copy visibly stale until a person asks
    # Klearing to open it and publish a new verified generation.
    import continuity_artifact
    continuity_artifact.mark_dirty(reason="database-restored")
    return inner.get("created_at", "")
