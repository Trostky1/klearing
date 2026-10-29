# Security

Klearing keeps your data on your own machine. It has no hosted service.

## Report a problem

Open a private security advisory on GitHub:
**Security tab > Report a vulnerability**.

Please do not post secrets, bank tokens, or real financial data in a public issue.

## How your data is protected

| Protection | What it does |
|---|---|
| Loopback only | The server listens on `127.0.0.1`. Other computers cannot reach it. |
| Exact Host check | A request must name Klearing's own address and port. This blocks DNS rebinding. |
| Cross-site guard | A form on another website cannot make your browser change Klearing data. A POST, PUT, PATCH, or DELETE with a foreign `Origin`, or marked `cross-site` by the browser, gets a 403. |
| Security headers | Every page sends `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a `Permissions-Policy`, and a content security policy that forbids framing. |
| App password | Locks every page. Failed attempts trigger a timed lockout. |
| Encrypted credentials | Keys sit encrypted in the local database. The encryption key lives in your operating system credential store. |
| Read-only bank access | Klearing cannot move money. |
| Local first | Transactions, reports, and exports stay on your machine. Integrations are opt in. |
| Approval gates | Nothing is posted or sent until you approve it. |

## What you should do

1. **Set an app password** in Setup. Without one, anyone at this computer can open Klearing.
2. **Use read-only keys.** When a service offers a read-only token, use it.
3. **Try sandbox keys first.** Plaid and Shopify both offer test modes.
4. **Keep your operating system updated.**
5. **Never paste keys into an issue or a chat.** If you did, rotate the key now.
6. **Download only from this repository's releases.** Builds are unsigned, so the source of the download is your only proof.
7. **Make a backup** from Settings and keep it somewhere safe.

## How we check

We run a review modeled on the security checklist from the
[ECC project](https://github.com/affaan-m/ECC), applied by hand and in CI:

- Secrets: none in tracked files or git history. `.env` and `*.db` stay ignored.
- Input: values go to SQLite through placeholders. Column names in dynamic
  `UPDATE` statements come from fixed allow lists in code.
- Output: user text is escaped before it goes into a page.
- Commands: no shell string is built from user input.
- Access: every page except the lock page and a local health check requires the app password when one is set.
- Dependencies: `pip-audit` runs weekly and on every pull request, and fails the build on a known vulnerability.
- Code: `bandit` runs on every pull request and fails the build on any high-severity finding.

The workflow is in [.github/workflows/security.yml](.github/workflows/security.yml).

## Known limits

- **Content security policy.** The policy forbids framing, plugins, and
  `<base>` tricks. It does not yet restrict scripts, because pages still use
  inline scripts and load Plaid Link from its CDN.
- **Lockout is in memory.** Restarting Klearing resets the failed-attempt count.
- **Unsigned builds.** Your operating system warns on first launch. See
  [docs/FIRST-LAUNCH.md](docs/FIRST-LAUNCH.md).
- **Someone with your logged-in account** on your computer can read your data.
  Klearing cannot protect against that.
