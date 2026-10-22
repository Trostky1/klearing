# Klearing — full context for any AI tool

Read this file first. Then load files from `skills/` as needed for the
specific task at hand.

---

## What Klearing is

Local-first bookkeeping approval tool, packaged as a native desktop app
(PyInstaller `.exe` / `.app`). A FastAPI + server-rendered HTML web app
(no build step, one `STYLE` block in `ui.py`) that runs on the owner's
machine at `localhost:8642`.

**What it does:** Bank transactions (via swappable bank adapters — Mercury
first), lead follow-ups (Klaviyo + Gmail), and overdue invoice reminders
(Wave) land in review queues. The user approves, edits, or skips each
item; approvals post to Wave or send email. Nothing leaves the machine
without an explicit click.

**Users:** Small-business owners doing their own bookkeeping.
Beginner-friendly — one person, one machine, browser UI, zero terminal.

**Register:** product — design serves the task (review queues, tables,
forms). Restrained color strategy: neutral surfaces, one accent for
primary actions and state, semantic colors for money in/out/transfer.

---

## Tech stack

- FastAPI + server-rendered HTML from Python f-strings
- One shared `STYLE` block in `ui.py` (no CSS framework, no JS framework,
  no build step)
- Hand-rolled SVG chart library in `charts.py` (5 chart types — never add
  a 6th without strong reason)
- SQLite via `db.py`
- PyInstaller packaged — stay lean, no CDN dependencies
- Native window via `pywebview`

---

## Surfaces

Setup (7-step first-run wizard, plus GA4 sub-wizard), then these tabs:

| Sidebar tab | Render function(s) |
|---|---|
| Home | `render_home_page`, `render_uncategorized_page` |
| Pending | `render_pending_page` |
| Reconcile | `render_reconcile_page` |
| Summary | `render_summary_page` |
| Vendors | `render_vendors_page`, `render_vendor_detail_page` |
| Follow-ups | `render_followups_page` |
| Invoices | `render_invoices_page` |
| Insights | `render_insights_page` |
| Growth | `render_growth_page` |
| Logs | `render_logs_page` |
| Settings | `render_settings_page`, `render_connections_page`, `render_automation_page` |
| Tools | `render_tools_hub`, `render_unit_economics`, `render_nexus`, `render_contractors`, `render_audit` |

Setup wizard screens live in `setup_wizard.py`, not `app.py`.

---

## Key files

```
ui.py              Shared CSS/HTML shell (STYLE block, render_shell, render_sidebar)
app.py             All render_*_page functions + API routes
charts.py          5 SVG chart types (sparkline, horizontal_bar, funnel, mini_bars, gauge)
db.py              SQLite schema + all accessors
ollama_client.py   Local Ollama AI client — TOOLS_SPEC, TOOL_FUNCS, build_context
accountant_export.py  PDF close package + QuickBooks CSV + all-transactions CSV
ai_providers.py    (feat/ai-providers, not yet merged) cloud AI provider abstraction
secrets_store.py   Fernet + OS keychain — all secrets go through this
health.py          Deterministic health-score composite (no AI)
cash_alert.py      Cash runway calculations
scheduler.py       OS-scheduler integration
paths.py           All file paths (LOG_NAMES, data-dir, etc.)
requirements.txt   Runtime deps — keep lean, never npm audit fix --force equivalent
CLAUDE.md          Instructions for Claude Code
PRODUCT.md         Product description
SECTION_PLAYBOOK.md 7-phase section improvement cycle checklist
```

---

## Design system (load `skills/` for details)

Dark ledger theme. Design tokens in `ui.py`'s `:root` block.

**Never introduce a new color, spacing, or font-size value** without
checking `skills/design-tokens.md` first. If the value isn't there, add
it there before using it.

**Never add a 6th chart type or a charting library** — extend one of the
5 in `charts.py`.

Quick reference (full detail in `skills/design-tokens.md`):
- Background stack: `--bg` (#0A0A0C) → `--surface` (#141417) → `--raised` (#1B1B20)
- Accent / income: `--accent` / `--income` = `#4ADE80` (controlled by `ACCENT_HEX` Python constant)
- Expense: `--expense` (#F87171), transfer: `--transfer` (#2DD4BF), warn: `--warn` (#FBBF24)
- Body font: system sans stack; numbers: `--font-mono` (ui-monospace stack)
- Monospace is for pure numbers/money only — hybrid phrases use `.mixed`

---

## Critical rules

1. **Port 8642 is the live running Klearing instance.** Always use an
   isolated port and separate data-dir for testing. Never probe 8642 from
   automated scripts.

2. **No hardcoded hex anywhere except `ui.py`'s `:root` block and `CHART_DEFS`
   gradient stops.** Every color in a render function is a `var(--token)`.

3. **Monospace is for pure numbers/money.** A hybrid phrase like "3 waiting"
   uses `.dash-value.mixed`, not bare `.dash-value`.

4. **AI assistant context must never contain raw transaction data** —
   aggregates only (`ollama_client.build_context` builds sums/counts/top-N).

5. **Never break offline operation.** `is_available()` never raises; every
   page renders without the AI model.

6. **Schema migrations are idempotent.** Always use `ALTER TABLE ... ADD
   COLUMN` in `_migrate_columns()`, never raw `CREATE TABLE`.

---

## Skills to load for specific tasks

- **`skills/klearing-ui.md`** — load before touching `ui.py`, `charts.py`,
  or any render function. Full design constitution.
- **`skills/design-tokens.md`** — exact token values from `ui.py`'s `:root`.
- **`skills/chart-patterns.md`** — the 5 chart types, their signatures,
  when to use each.
- **`skills/anti-patterns.md`** — 22 real findings from this codebase. Check
  every diff against this before calling work done.
- **`skills/klearing-section.md`** — load when running a full section
  improvement cycle (visual audit → feature audit → competitor benchmark →
  plan → implement → verify → commit).
- **`skills/section-inventory.md`** — every screen name to render function
  mapping, extracted from `app.py`.
- **`skills/competitors.md`** — benchmarks for Monarch, YNAB, Copilot,
  Tiller Money, Lunch Money (gathered July 2026).

---

## Open PRs as of July 2026

- **PR #38** `feat/ai-providers` — OpenAI + Anthropic cloud providers via
  consent dialog + persistent cloud warning pill. Open, not merged.
- **PR #39** `feat/accountant-export` — PDF close package (reportlab),
  QuickBooks CSV, all-transactions CSV, evidence tags on transactions,
  notes/evidence editing on Vendor Detail. Open, not merged.

---

## Keep this file current

Run `python .context/sync.py` whenever any of the source files change
to overwrite this folder from the originals.
