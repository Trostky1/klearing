# Klearing — project context for Claude

Full product context lives in `PRODUCT.md` at the repo root. Read that
first for what Klearing is, who it's for, and its register.

## Skills

Design constitution: skills/klearing-ui/SKILL.md
Section workflow: skills/klearing-section/SKILL.md

Load the design constitution before touching `ui.py`, `charts.py`, or any
render function in `app.py`. Load the section workflow when asked to run
a full improvement cycle on a specific screen — it wraps
`SECTION_PLAYBOOK.md`'s phase checklist with the design constitution and
the competitor-benchmark research already gathered.

## Context folder

`.context/` is a portable snapshot of all project context files (skills,
playbook, product doc, CLAUDE.md) for dropping into any AI tool. Run
`python .context/sync.py` whenever skill files or context files are
updated to keep `.context/` current. `.context/CONTEXT.md` is
hand-authored — edit it directly if the high-level overview changes.

## Testing

Port 8642 is the live running Klearing instance — always use an isolated
port and separate data-dir for testing, never probe 8642 from automated
scripts.

Klearing — Project Router & Rules

Read this file first, every session, before exploring the codebase or writing code. It routes you to the right files and encodes rules learned the hard way across this project's history. When a task conflicts with a rule below, stop and ask rather than proceeding.

What Klearing is

Local-first Windows desktop business management app. Python/FastAPI backend, server-rendered HTML (no build step, no JS framework), packaged via PyInstaller, native window via pythonnet + WebView2. Runs at localhost:8642.

Where to look (router)
main.py — entry point, shell/backend supervision, CLI flags (development-only --serve, scheduled --daily-check/--weekly-digest), host binding. Packaged normal use is Klearing.exe with no flags; the private backend child is never a user launch target. Must be 127.0.0.1 for any real release — confirm this before packaging, don't assume it.
app.py — FastAPI routes, Home page rendering (render_home_page, _briefing_points, _briefing, _today_region, _today_exceptions)
ui.py — all HTML/CSS generation, design tokens, shared components table
ledger.py — double-entry ledger core (Account, JournalEntry, JournalLine). Money is integer cents. Every entry must balance before it touches the DB.
ledger_sync.py — sync_ledger_from_sources(), pulls Shopify + Plaid into journal entries, idempotent
check_bank.py — run_daily(), the --daily-check orchestration
briefing_signals.py / briefing_notifications.py — Feature A (daily briefing + Windows toast)
business_registry.py — local business identity and active-business selection
scripts/ledger_verify.py — manual CLI: sync + trial balance + P&L + balance sheet, PASS/FAIL on Assets = Liabilities + Equity
scripts/*_regression.py — regression suites. Run the relevant ones before declaring anything done.
docs/superpowers/specs/ — dated design-decision specs. Read relevant ones before touching UI or ledger-adjacent features. Write a new one when you make a non-obvious architectural call.
.env (repo root, dev) / %LOCALAPPDATA%\Klearing\.env (packaged) — secrets. UTF-8, no BOM. Never log or commit these.
Non-negotiable rules

Never guess on money. If data is ambiguous (owner contribution vs. loan proceeds, whether a payout has reserves, whether cost data predates a sale) — skip and log, never auto-categorize speculatively.

Never retroactively reprice historical sales. COGS must never apply today's cost to a historical order.

Single/bulk code paths must be identical, never parallel implementations, on anything touching the ledger or money. Two implementations of the same money-moving logic diverging is the specific failure mode this project is designed against.

No chart library, ever. 5 chart types max, hand-rolled in charts.py. Defended explicitly even during full creative redesigns — not an oversight to fix.

No literal hex outside design tokens. No inline style= attributes (regression-guarded). Money renders in the existing monospace convention.

Any new mutation follows the existing POST → 303 → banner pattern. Any new confirmation uses the existing native <dialog> pattern — never window.confirm().

Local-first is load-bearing, not a slogan. No new cloud dependency, no new inbound listener, no relay server — for any feature, not just the ones where this was explicitly discussed already. Default answer to "does this need a public server" is no.

Wave is retired. Do not add new Wave functionality or restore its credentials, setup flow, client, or live reads. Historical Wave identifiers and the read-only Tax archive remain audit data; the native ledger is authoritative.

Approval gates — stop and ask, don't decide silently
Any new third-party dependency. This has already gone wrong once — an agent shipped a full macOS UserNotifications implementation, an entire unplanned platform target, without asking. Don't repeat that pattern.
Any change to ledger.py, ledger_sync.py, or anything that posts journal entries
Any change to the "Needs attention" card's scope — it's deliberately bookkeeping-blocking-only, that's a decision, not an omission
Any new persisted schema/table
Any expansion of provider/integration scope beyond what a spec explicitly names
Any fix that starts expanding into a rework (e.g. "fixing a DLL issue" turning into "redesigning how WebView2 is hosted") — stop and report before going bigger than the original ask
Test plan / verification convention

Every phase ends with a verification summary table (what was built, what was tested, pass/fail) before moving to the next phase. Don't chain multiple phases into one uninterrupted build — stop for sign-off at each boundary. Run the relevant regression suite(s) before declaring anything done. ledger_verify.py must show PASS on Assets = Liabilities + Equity after any ledger-adjacent change.

When you find outdated docs

If something in docs/superpowers/specs/ or elsewhere no longer matches shipped code, report the discrepancy with a citation (file:line, commit hash) — don't trust the doc, and don't silently rewrite it either. Docs get corrected by the human
