"""
Bank adapters: the one swappable integration in Klearing.

Klaviyo and GA4 are fixed, built-in integrations.
The bank is not — every bank-facing call goes through a BankAdapter,
so supporting a new bank means writing one adapter file, not touching
the rest of the app. Mercury is the first implementation
(banks/mercury.py); docs/BANK-ADAPTERS.md explains how to add more.

An adapter provides:
- identity (id, display_name, where to get an API token)
- credential field specs, including format rules the Setup wizard
  checks BEFORE any live API call (prefix, length, charset)
- test_connection() for the wizard's Test Connection button
- list_accounts() for the business-cash picker in Settings
- fetch_transactions() for the daily check, normalized to a common
  shape so the rest of the app never sees bank-specific JSON

Adapters are discovered two ways:
1. Built-ins registered below.
2. Drop-in plugins: any *.py file in <data dir>/bank_adapters/ that
   defines ADAPTER (an instance). This lets a rebuilt-nothing install
   gain a bank. Files there run with full app permissions — only add
   adapters you wrote or read.
"""

import importlib.util
import re

import paths

# ------------------------------------------------------------------ fields


class CredentialField:
    """
    One credential input in the Setup wizard, with format rules that
    run before any live API call — catching dropped prefixes and
    truncated pastes, the single biggest time sink in the original
    setup.
    """

    def __init__(self, key, label, help_text="", placeholder="",
                 prefix=None, min_length=None, max_length=None,
                 pattern=None, format_error=None, secret=True,
                 strip_spaces=False, normalizer=None):
        self.key = key
        self.label = label
        self.help_text = help_text
        self.placeholder = placeholder
        self.prefix = prefix
        self.min_length = min_length
        self.max_length = max_length
        self.pattern = pattern
        self.format_error = format_error
        self.secret = secret
        self.strip_spaces = strip_spaces
        self.normalizer = normalizer

    def clean(self, value):
        """The value as it will be validated and stored: trimmed, and
        repaired by the normalizer where a safe repair exists (e.g.
        re-adding a documented prefix lost in the copy)."""
        value = (value or "").strip()
        if self.strip_spaces:
            value = value.replace(" ", "")
        if self.normalizer:
            value = self.normalizer(value)
        return value

    def validate_format(self, value):
        """None if the value looks right; otherwise a specific,
        actionable error string. Never makes a network call."""
        value = self.clean(value)
        if not value:
            return f"{self.label} is empty. Paste it in (don't retype it)."
        if self.prefix and not value.startswith(self.prefix):
            return (
                f'{self.label} should start with "{self.prefix}" — yours '
                f'starts with "{value[:len(self.prefix)]}". The beginning '
                f"was probably lost in the copy. Go back to where you "
                f"created it and copy the whole thing again."
            )
        if self.min_length and len(value) < self.min_length:
            return (
                f"{self.label} looks too short ({len(value)} characters; "
                f"expected at least {self.min_length}). Part of it was "
                f"probably cut off in the paste — copy it again in one go."
            )
        if self.max_length and len(value) > self.max_length:
            return (
                f"{self.label} looks too long ({len(value)} characters; "
                f"expected at most {self.max_length}). Extra text may have "
                f"come along in the copy — check for leading/trailing junk."
            )
        if self.pattern and not re.fullmatch(self.pattern, value):
            return self.format_error or (
                f"{self.label} contains unexpected characters. Paste it "
                f"exactly as shown where you created it."
            )
        return None


# ------------------------------------------------------------------ adapter


class BankAdapter:
    """
    Base class for bank integrations. Subclasses set the class
    attributes and implement the three methods.

    fetch_transactions must return dicts with exactly these keys:
      external_id   the bank's unique transaction id (dedup key)
      date          YYYY-MM-DD
      amount        signed float; negative = money out
      counterparty  merchant / payer name, best effort
    """

    id = ""                # short, filesystem-safe, e.g. "mercury"
    display_name = ""      # shown in the Setup dropdown
    token_url = ""         # where the user creates an API token
    token_help = ""        # one line: how to get the token

    def credential_fields(self):
        raise NotImplementedError

    def test_connection(self, credentials):
        """Live check. Returns connectivity.TestResult."""
        raise NotImplementedError

    def list_accounts(self, credentials):
        """[{'id': ..., 'name': ..., 'balance': float}, ...]"""
        raise NotImplementedError

    def fetch_transactions(self, credentials, since_date=None):
        raise NotImplementedError

    # ---- credential storage conventions (shared by all adapters)

    def secret_name(self, field_key):
        return f"bank.{self.id}.{field_key}"

    def stored_credentials(self):
        """Credentials for this adapter from the secrets store, or None
        if any required field is missing."""
        import secrets_store
        creds = {}
        for field in self.credential_fields():
            value = secrets_store.get_secret(self.secret_name(field.key))
            if value is None:
                return None
            creds[field.key] = value
        return creds

    def is_configured(self):
        return self.stored_credentials() is not None


# ------------------------------------------------------------------ registry

_adapters = {}
_plugins_loaded = False


def register(adapter):
    _adapters[adapter.id] = adapter


def _load_dropin_plugins():
    """Import ADAPTER from every *.py in <data dir>/bank_adapters/."""
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    plugin_dir = paths.data_dir() / "bank_adapters"
    if not plugin_dir.is_dir():
        return
    for path in sorted(plugin_dir.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(
                f"bank_adapter_{path.stem}", path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            adapter = getattr(module, "ADAPTER", None)
            if not (isinstance(adapter, BankAdapter) and adapter.id):
                continue
            if adapter.id in _adapters:
                # A plugin must not silently replace a built-in (or
                # another plugin) — first registration wins.
                print(f"Bank adapter plugin {path.name} skipped: id "
                      f'"{adapter.id}" is already registered.')
                continue
            register(adapter)
        except Exception as exc:
            # A broken plugin must not take the app down; it just
            # doesn't appear in the dropdown.
            print(f"Bank adapter plugin {path.name} failed to load: {exc}")


def all_adapters():
    """Every available adapter, built-ins first."""
    _load_dropin_plugins()
    return list(_adapters.values())


def get_adapter(adapter_id):
    _load_dropin_plugins()
    adapter = _adapters.get(adapter_id)
    if adapter is None:
        raise KeyError(f'No bank adapter named "{adapter_id}".')
    return adapter


ACTIVE_BANK_KEY = "active_bank"


def active_adapter():
    """The adapter chosen during Setup, or None if none configured."""
    import db
    db.init_db()
    adapter_id = db.get_setting(ACTIVE_BANK_KEY)
    if not adapter_id:
        return None
    try:
        return get_adapter(adapter_id)
    except KeyError:
        return None


# Built-ins. Import at the bottom so the module-level names above
# exist when the adapter module imports this package.
from banks.mercury import MercuryAdapter  # noqa: E402
from banks.plaid import PlaidAdapter  # noqa: E402
from banks.simplefin import SimpleFINAdapter  # noqa: E402

register(MercuryAdapter())
register(PlaidAdapter())
register(SimpleFINAdapter())
