# Adding a bank to Klearing

The bank connection is Klearing's one deliberately swappable
integration (Klaviyo and GA4 are fixed and built in). All
bank-facing behavior goes through a `BankAdapter`, so supporting a
new bank means writing one Python file and nothing else: the Setup
wizard, Test Connection flow, Settings cash-account picker, cash
alert, and daily fetch all pick it up automatically.

Mercury (`banks/mercury.py`) is the reference implementation — read
it alongside this page; it's under 150 lines.

## The interface

Subclass `banks.BankAdapter` and provide:

```python
class MyBankAdapter(banks.BankAdapter):
    id = "mybank"                 # short, filesystem-safe, unique
    display_name = "MyBank"       # what the Setup dropdown shows
    token_url = "https://mybank.example/settings/api"
    token_help = "Where to create the token, in one sentence."

    def credential_fields(self):
        # Each field renders in the wizard with a Test Connection
        # button. The format rules run BEFORE any live call and catch
        # truncated pastes with specific errors — fill in whatever
        # your bank's tokens actually look like.
        return [banks.CredentialField(
            key="api_token",
            label="MyBank API token",
            help_text="Paste it, don't retype it.",
            prefix="mybank_",         # or None
            min_length=40,
            max_length=120,
            pattern=r"[A-Za-z0-9_\-]+",
        )]

    def test_connection(self, credentials):
        # Live check. Return connectivity.ok("...") or
        # connectivity.fail("specific, actionable message").
        # For unexpected exceptions, connectivity.from_exception(
        # "MyBank", exc) adds the ad-blocker/DNS hint on
        # connection-level failures automatically.
        ...

    def list_accounts(self, credentials):
        # [{"id": str, "name": str, "balance": float}, ...]
        ...

    def fetch_transactions(self, credentials, since_date=None):
        # since_date is "YYYY-MM-DD" or None (first run: return what
        # you reasonably can). Overlap is fine — the caller dedups by
        # external_id. Return dicts with EXACTLY these keys:
        #   external_id   the bank's unique transaction id
        #   date          "YYYY-MM-DD"
        #   amount        signed float; negative = money out
        #   counterparty  merchant / payer name, best effort
        ...
```

Ground rules:

- **Read-only.** An adapter must never move money; ask users for the
  most restricted token the bank offers.
- Credentials are handed to you decrypted as `{field_key: value}` —
  never store them yourself; the app keeps them in the encrypted
  store under `bank.<id>.<field_key>`.
- Raise exceptions freely in `fetch_transactions` — the daily check
  logs them and emails a failure alert without crashing other steps.

## Shipping it

Two options:

1. **Built-in (preferred):** add the file as `banks/mybank.py`,
   register it at the bottom of `banks/__init__.py`, and open a pull
   request.
2. **Drop-in plugin (no rebuild):** put the file in the app's data
   directory under `bank_adapters/` (create the folder;
   `%LOCALAPPDATA%\Klearing\bank_adapters\` on Windows,
   `~/Library/Application Support/Klearing/bank_adapters/` on macOS)
   with a module-level instance named `ADAPTER`:

   ```python
   ADAPTER = MyBankAdapter()
   ```

   It appears in the Setup dropdown on the next app start. Plugin
   files run with the app's full permissions — only install adapters
   you wrote or have read.

   Two plugin constraints: an id that collides with a built-in (or an
   earlier plugin) is skipped, never replaces it; and inside the
   packaged app, plugins can only import the standard library plus
   what Klearing already bundles (`requests`, `cryptography`,
   `rapidfuzz`, `keyring`) — a plugin needing some other third-party
   package must ship as a built-in via pull request instead.
