# Klearing

Local-first bookkeeping clarity for solo business owners.

> **Release status:** Klearing is under active preparation for a public
> small-business release. The current setup requires a bank connection;
> a complete no-provider statement import and reconciliation workflow is
> still being built. Do not use this version as the sole record of a business.
> See [the release workplan](docs/DECEMBER-RELEASE-WORKPLAN.md).

Your transactions, reports, and AI analysis stay on your machine.
External integrations are optional — you choose what connects.

## Start here

- **[Set up Klearing](docs/SETUP.md)** in ten minutes, step by step.
- **[Make it yours](docs/CUSTOMIZE.md)**: settings, screens, and code.
- **[Troubleshooting](docs/TROUBLESHOOTING.md)**: what went wrong for us, and the fix.
- **[Security](SECURITY.md)**: how your data and credentials are protected.

## What Klearing does

- Connects to your bank and records approved transactions in a native ledger
- Routes transactions through an approval queue — nothing is
  silently categorized
- Detects vendor spend jumps, recurring charges, and cash runway
- Offers bank balance and transaction coverage checks; statement-based
  reconciliation still needs end-to-end release validation
- Drafts invoice follow-ups and overdue reminders
- Exports a PDF monthly close package and QuickBooks-compatible CSV
- Runs a local AI assistant (Ollama) that explains your financials
  without sending data anywhere

## Privacy

Core data lives on your machine. Always.

Optional integrations connect to external services — Plaid, Stripe,
Gusto, Gmail, GA4, Klaviyo. Each is opt-in and explicitly scoped.

The AI assistant uses Ollama by default (runs on your machine). OpenAI and
Anthropic are available as optional cloud providers — you'll see a
clear warning before any financial data leaves your machine.

## What you need

- **A supported bank**, one of:
  - **Mercury** — paste a read-only API token.
  - **Plaid** — connects **Wells Fargo plus ~12,000 other banks**
    through Plaid's secure Link flow. You bring your **own free Plaid
    account** (see "Connecting a bank with Plaid" below; Plaid may charge
    for production use).

  The bank connection is a plugin: see
  [docs/BANK-ADAPTERS.md](docs/BANK-ADAPTERS.md) to add another bank,
  or open an issue to request one.
- Optional: **Klaviyo** (lead follow-up drafts), **Google Analytics
  4** (website insights), **Gmail** (notifications and sending — an
  app password, not your real password).

## Install — Windows

1. Download `Klearing-windows.zip` from the latest release and
   extract it anywhere (your Documents folder is fine).
2. Open the `Klearing` folder and double-click **Klearing.exe**.
3. Windows will show a blue **"Windows protected your PC"** screen the
   first time — this is expected for an unsigned app. Click
   **More info → Run anyway**. Full details and why:
   [docs/FIRST-LAUNCH.md](docs/FIRST-LAUNCH.md).
4. Klearing opens in its own app window with the **Setup** flow (about
   ten minutes). No terminal, ever.

## Install — macOS

The released .app is built for Apple Silicon (M1 and later). On an
Intel Mac, build from source instead (two commands, see below).

1. Download `Klearing-macos.zip` from the latest release, unzip, and
   drag **Klearing.app** to Applications.
2. On first open, macOS will refuse with **"Apple could not verify…"**.
   Click Done, then: **System Settings → Privacy & Security →** scroll
   to Security **→ Open Anyway**. Exact steps with what each screen
   looks like: [docs/FIRST-LAUNCH.md](docs/FIRST-LAUNCH.md).
3. Klearing opens in its own app window with **Setup**.

## Setup, in one paragraph

Step 1 sets an app password (a lock on the pages, so someone else on
your computer can't read your finances). Step 2 names your local business,
and step 3 connects your bank with a **Test Connection** button that checks
the pasted key's format instantly and then tries it live, with errors that
say exactly what's wrong. Steps 4–6 (Klaviyo, GA4,
email) are optional and skippable; anything skipped can be connected
later from **Settings → Connections**. GA4 has its own guided
walkthrough because Google makes it genuinely fiddly. Paste keys,
never retype them.

After Setup, open **Settings → Automation** and click **Turn
automation on**: the app starts at login, the bank check runs every
morning at 8 (or when the machine wakes), and the digest goes out
Monday mornings.

## Connecting a bank with Plaid

Mercury hands out API tokens directly. Most other banks (Wells Fargo,
Chase, local credit unions, ~12,000 in all) don't — they connect
through **Plaid**, which brokers a secure, read-only link between your
bank and apps like Klearing.

Each person supplies their own Plaid account and keys; Klearing does not
provide a shared key. Plaid's Trial offering has limits, and production
Transactions access can incur charges. Check Plaid's current pricing and
eligibility before connecting a real bank. Klearing stores your Plaid keys
locally in its encrypted vault and does not receive your bank password.

How to connect, in Setup step 3 (Bank):

1. Pick **Plaid** from the bank dropdown.
2. In your Plaid dashboard, go to **Developers → Keys** and copy your
   **Client ID** and a **Secret**. Paste both into Klearing.
   - Choose **environment**: `production` for your real bank, or
     `sandbox` to try the whole flow first with fake test banks (zero
     risk). Sandbox and Production have **different Secrets** — use the
     one that matches.
3. Click **Test Connection**, then **Continue**.
4. Click **Connect your bank with Plaid**. Plaid's own window opens;
   sign in to your bank there. When it finishes, Klearing stores a
   read-only access token (in the vault) and you're connected.
   - In `sandbox`, you can instead click **Connect a Sandbox test bank
     (no login)** to see it work immediately.

After that, Plaid behaves exactly like Mercury: new transactions show up
on the Pending tab for your approval. Nothing is ever posted or moved
automatically.

> Note on real banks that use OAuth (many big banks, including Wells
> Fargo): Plaid may need a redirect URI registered in your Plaid
> dashboard for the in-app window. Sandbox and non-OAuth banks work with
> no extra setup. If a real-bank login won't complete in the window,
> that registration is the usual fix.

## Where your data and credentials live

- Data: `%LOCALAPPDATA%\Klearing` on Windows,
  `~/Library/Application Support/Klearing` on macOS — one SQLite
  database plus log files (readable in-app under **Logs**).
- Credentials: encrypted in that database. The encryption key lives
  in your operating system's credential store (Windows Credential
  Manager / macOS Keychain), protected by your OS login. That's what
  lets the unattended 8 AM check run without you typing a password,
  while keys still sit encrypted at rest.
- The app password gates the web pages only. Resetting it never loses
  data or credentials.

## Troubleshooting

Something not working? See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
It covers rejected tokens, Plaid environment mismatches, blocked requests,
GA4, Gmail app passwords, a dashboard that will not open, and a morning check
that did not run.

## Building from source

```
python -m venv venv
venv/bin/pip install -r requirements.txt      # Windows: venv\Scripts\pip
venv/bin/python main.py
```

The no-flag command is the normal launch target. It starts a small native
shell which owns a separate local backend process; closing or crashing the
shell also ends that backend. Developers who deliberately need the server
without a window can run `venv/bin/python main.py --serve` (Windows:
`venv\Scripts\python main.py --serve`). `--serve` is not available in the
packaged app, whose only normal launch target is `Klearing.exe` with no flags.

Packaging: `build/build_windows.ps1` on Windows,
`build/build_macos.sh` on a Mac, or run the `build` GitHub Actions
workflow to get both from free runners (PyInstaller cannot
cross-compile). Builds are unsigned by design — no paid certificates —
so first launches hit SmartScreen/Gatekeeper as documented in
[docs/FIRST-LAUNCH.md](docs/FIRST-LAUNCH.md).

## Updates

Klearing checks GitHub Releases once a day and shows a small banner
when a newer version exists, linking to the download. It never
auto-updates — silently replacing an unsigned binary on your machine
would be a security anti-pattern, so updating stays a deliberate
download-and-run, exactly like the first install.

## For developers

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, style, PR flow.
- [docs/BANK-ADAPTERS.md](docs/BANK-ADAPTERS.md) — add a bank in one
  file (the only pluggable integration; Klaviyo and GA4 are
  fixed).
- [docs/GA4-SETUP.md](docs/GA4-SETUP.md) — the GA4 walkthrough as a
  readable page.
- The web UI is plain server-rendered HTML with one CSS block and a
  few lines of vanilla JS — no build step anywhere.

## License

[MIT](LICENSE) — free for any use, commercial included.
