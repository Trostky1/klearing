# Contributing to Klearing

Thanks for looking under the hood. Klearing is deliberately small —
a supervised shell/backend pair, server-rendered HTML, SQLite — and contributions
that keep it small are the ones that fit best.

## Dev environment

```
git clone https://github.com/Trostky1/klearing
cd klearing
python -m venv venv
venv/Scripts/pip install -r requirements.txt    # macOS/Linux: venv/bin/pip
venv/Scripts/python main.py
```

That starts the app at <http://localhost:8642> and opens the Setup
wizard. Useful dev facts:

- `KLEARING_DATA_DIR=some/empty/dir` points the app at a throwaway
  data directory — use it so experiments never touch your real data.
  `KLEARING_PORT` overrides the port.
- There is no build step. The whole UI is server-rendered HTML from
  Python f-strings with one CSS block (`ui.py`) and a few lines of
  vanilla JS. Reload the page, see your change.
- `main.py --daily-check` and `--weekly-digest` run the scheduled
  jobs in the foreground.
- `main.py --serve` runs only the development backend, without a window.
  Packaged builds reject it: `Klearing.exe` with no flags is the one normal
  user launch target, and its shell owns the private backend child's lifetime.
- The "Insert test item" button on Pending exercises the whole
  approve flow without touching the ledger or a bank.

## The bank-adapter pattern (adding a bank)

The bank connection is the one pluggable integration — Klaviyo,
and GA4 are fixed by design. A new bank is ONE file implementing
`banks.BankAdapter`: credential field specs (with format rules the
wizard checks before any live call), `test_connection`,
`list_accounts`, and `fetch_transactions` normalized to
`{external_id, date, amount, counterparty}`. The Setup dropdown, Test
Connection flow, Settings pickers, reconciliation, and the daily
check all pick it up automatically.

Read [docs/BANK-ADAPTERS.md](docs/BANK-ADAPTERS.md) — it's the full
contract — with `banks/mercury.py` open next to it as the reference
implementation (~150 lines). `banks/plaid.py` is a second built-in and
a good second example: it shows the harder case where the credential
isn't a single pasted token but an OAuth-style Link flow (paste keys →
open Plaid Link → exchange for a per-bank access token), with the
extra connect step handled in the Setup wizard and every credential
still going through the vault. Ground rules: read-only tokens only
(an adapter must never be able to move money), never store
credentials yourself, and raise freely in `fetch_transactions` (the
daily runner logs and alerts without taking other steps down).

## Code style

Match what's already there rather than importing your own:

- Plain Python, standard library first. New runtime dependencies need
  a strong reason — the dependency list is seven packages and that's
  what keeps the packaged app small and PyInstaller reliable.
- No type-annotation sweeps, no framework layers, no client-side
  build tooling.
- Comments explain constraints the code can't show (provider-specific
  authentication quirks), not what the next line
  does.
- Error messages are specific and actionable — "looks too short
  (6 characters; expected at least 20)" — never a bare "invalid".
- Anything that touches money or sends email must sit behind an
  explicit human click. This is the product's one non-negotiable.

## Submitting a PR

1. Fork, branch from `main`.
2. Make the change; run the app and exercise the affected flow (for
   adapters: the wizard's Test Connection, a fetch, and the Settings
   account picker).
3. `python -m compileall .` should be clean; keep the diff tight.
4. Open the PR with: what it does, how you tested it, and — for
   adapters — which token type/permissions you tested with. Never
   include real tokens, account ids, or balances in the PR.

Bug reports and bank requests: open an issue. For anything
security-sensitive (credential handling, the lock, the backup
format), please flag it as such in the issue title.
