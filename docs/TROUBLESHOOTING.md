# Troubleshooting

Every item here is a real way setup went wrong for us. If you hit something
new, [open an issue](https://github.com/Trostky1/klearing/issues/new) and we
will add it.

Find your problem:

- A token or key was rejected: see the first three items.
- A service test fails but the key is right: see the fourth item.
- The app will not open, or a job did not run: see the last items.
- First-launch security warnings: see [FIRST-LAUNCH.md](FIRST-LAUNCH.md).

Built directly from the ways real setup actually went wrong.

**"…rejected the token" right after pasting.**
Almost always a copy problem, not a service problem. Tokens are shown
once and must be pasted whole: Mercury tokens start with
`secret-token:mercury_` (Klearing repairs a missing prefix if the
rest is intact), and Klaviyo private keys start with `pk_`. If the format
check passes but the live test still fails, the token was probably
revoked — create a fresh one and paste again.

**Plaid: "rejected these keys for the chosen environment."** The
Secret must match the environment — a Sandbox Secret won't work in
Production, and vice versa. Copy the Secret for the environment you
picked, from Plaid's **Developers → Keys**. The Client ID is the same
across environments.

**Test Connection fails but the credential is definitely right.**
If the error says the service couldn't be reached at all: a browser
extension, ad-blocker, or DNS privacy tool (AdGuard, Pi-hole, NextDNS,
strict tracking protection) may be silently blocking the request.
Try disabling ad-blockers or testing from a different network. This
one cost hours in the original setup — it looks exactly like a wrong
key.

**GA4 hurts.** Known. It's optional — skip it and connect later from
Settings → Connections. When you do: use the guided walkthrough (it
has a checkpoint after every stage), remember the **Property ID is a
plain number** (the G- Measurement ID will not work), enable the
**Data API** (not the Admin API), and if a **Google Workspace**
account won't let you create a Cloud project, use a personal Gmail —
it only needs Viewer access on the Analytics property. If numbers
stay at zero, confirm you granted access on the property that's
actually receiving data: open GA4's Realtime report while visiting
your site.

**No emails arrive.** The Gmail credential must be an **app
password** — exactly 16 letters from
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
which requires 2-Step Verification — not your normal password. Note
Google revokes all app passwords whenever you change your Google
password; generate a new one and update it in Settings → Connections.

**The dashboard doesn't open.** The app may already be running —
just go to <http://localhost:8642>. Check the **Logs** page (app.log)
for "port already in use" or errors. Double-clicking the app again is
safe: it opens the dashboard instead of starting a second copy.

**The morning check didn't run.** Settings → Automation shows each
job's status and the "last succeeded" heartbeat; daily.log and
error.log on the Logs page have the details. On Windows the machine
must be on (or asleep — it catches up on wake); runs missed while
powered off are skipped.

**macOS asks about keychain access again after updating the app.**
A limitation of unsigned builds: macOS ties "Always Allow" to the
binary's signature, and each rebuild looks like a new app. Click
Allow once after an update.

## Review is empty after "Check for new transactions"

1. Open **Settings, Connections** and click **Re-test** on your bank. Fix any
   red message first.
2. Check the **Logs** page (`error.log` and `daily.log`) for the reason.
3. Remember that Klearing imports only settled transactions. A card charge the
   bank still shows as pending will not appear until it settles.

## Still stuck

Open the **Logs** page, copy the last few lines, and put them in an issue.
Remove any account numbers first. Klearing never writes your keys to the logs.

## Start over on a test copy

To try a fix without touching your real books, point Klearing at an empty
folder and a different port. See [CUSTOMIZE.md](CUSTOMIZE.md#use-a-test-copy-first).
