# Ledger UI — design

Date: 2026-07-25
Branch: `feat/ledger-ui`
Status: implemented — all five screens shipped 2026-07-25

## Problem

Klearing has a working native double-entry ledger (`ledger.py`,
`ledger_sync.py`, three migrations) with no user interface of any kind.
`app.py` never imports it. Its only consumers are `scripts/ledger_verify.py`
and `scripts/ledger_regression.py`, so every number it produces is visible
only from a terminal.

The most damaging consequence is not the missing reports. It is that
`ledger_source_import_skips` — the durable record of source data that was
deliberately not posted — has no home. Unmatched Shopify payouts and orders
missing cost snapshots are invisible, which means any statement rendered from
this ledger today is silently incomplete.

## Two sources of financial truth

Klearing now has two systems that each produce an income statement.

| | Tax path | Ledger path |
|---|---|---|
| Table | `kl_transactions` | `journal_lines` |
| Source | Wave | Plaid + Shopify |
| Method | Flat, single-entry, cash-basis | Double-entry, accrual-capable |
| Gate | Human approves AI category | Idempotent source import |
| UI | Shipped | None |

They share no data: no join, no shared IDs, no shared category vocabulary
(account codes vs. AI category strings).

**Decision: separate now, converge later.** Ledger screens live under their
own nav tab. Every statement states its basis explicitly, so a user who sees
two different net-income figures can tell why. Convergence is a later phase
and is out of scope here.

## Known data problems, and how the UI treats them

`ledger_sync.py` had four limitations that affected what the reports said,
found while building this UI on 2026-07-25.

1. `_sync_plaid` credited `4000 Sales Revenue` for **every** non-Shopify
   deposit (`ledger_sync.py:116`). Owner contributions, loan proceeds,
   refunds, and interest all landed in revenue. Revenue read high.
2. Transfers were skipped entirely, so inter-account movement was invisible.
3. There are no closing entries, so `balance_sheet` reports
   `current_earnings` separately rather than rolling it into equity.
4. **Inventory drifted negative.** `_sync_shopify_cogs` credited `1200
   Inventory` when it posted COGS, but nothing in `ledger_sync` ever debited
   Inventory — there was no inventory-purchase path. Inventory therefore
   decreased by the full value of every COGS entry and never increased,
   going further negative over time. Confirmed on seeded data: three
   entries produced an Inventory balance of -$40.00, exactly the COGS
   posted.

**Original decision: surface, do not fix.** Correcting these was scoped as
separate backend work on its own branch. The UI's job in this pass was to
make each problem legible rather than bury it — a standing caveat attached
to the number it describes, not a dismissible banner. Point 3 was not a
defect; it was handled honestly by the backend and simply needed an
accurate label. Points 1, 2 and 4 were real defects and their caveats said
so.

**Update, 2026-07-26 — points 1, 2 and 4 are fixed.** A separate backend
pass (on this same branch, prior to the visual redesign) changed
`ledger_sync.py`:

- **Deposit classification (point 1):** Plaid deposits now route to
  `4200 Interest Income`, `6090 Refunds & Reimbursements`, or
  `4190 Uncategorized Income` by category, via `_plaid_deposit_account`
  (`ledger_sync.py:73`). Only Shopify sales post to `4000 Sales Revenue`.
  A one-time reclassification pass (`_reclassify_misclassified_plaid_deposits`,
  `ledger_sync.py:242`) moves any deposit a prior version had already
  posted to Sales Revenue out to the correct account, keyed off which
  account each source reference actually posted to
  (`db.source_import_account_codes`). Regression:
  `scripts/ledger_regression.py`, "prior Plaid deposits are reclassified
  out of Sales Revenue."
- **Transfers (point 2):** now posted rather than skipped, via a new
  `1090 Transfer Clearing` account (`_sync_transfer`, `ledger_sync.py:145`).
  A same-amount, opposite-sign, same-account, ±5-day counterpart posts
  directly between the two real accounts; an unpaired transfer posts
  one-sided through Transfer Clearing so it stays visible rather than
  silent, and a later match clears the suspense side rather than
  rewriting the immutable first entry. Two or more equally-plausible
  candidates are refused and logged as `ambiguous_transfer_match` rather
  than guessed. Regressions: "paired Plaid transfers move between
  connected accounts", "one-sided Plaid transfers remain visible through
  clearing."
- **Inventory (point 4):** `_sync_shopify_cogs` (`ledger_sync.py:458`) no
  longer posts a COGS entry with nothing to debit it. Every order without
  a tracked acquisition cost is now skipped with reason
  `inventory_acquisition_not_tracked`, visible on Ledger → Sync's
  exceptions table exactly like the pre-existing `missing_cost_data` and
  `zero_cogs` skips. Inventory itself no longer misstates — this trades
  the original bug for a correctly surfaced, now-conditional one: sales
  without complete, dated Ledger-backed acquisition coverage still skip COGS,
  while costed sales post normally. Regression coverage includes both paths.

This requires the new `add_ledger_routing.sql` migration (`plaid_accounts`
table plus the `1090`/`4190`/`4200`/`6090` accounts), registered in
`migrations/run_migrations.py` as `LEDGER_ROUTING_TABLES`.

The UI caveats were corrected to match on 2026-07-26, as part of the
visual redesign: the old `REVENUE_CAVEAT` ("Revenue includes every bank
deposit... overstated") was removed from the P&L Revenue group — false
now that classification is accurate, and the account breakdown already
shows Uncategorized Income as its own line for anyone checking what
Plaid could not classify. `INVENTORY_CAVEAT` was removed from the
Balance sheet Assets group for the same reason. The conditional COGS caveat
appears on the P&L's Cost of goods sold group only when unresolved Shopify
COGS skips exist in the selected period, and counts those sales rather than
describing every Shopify sale as uncosted. See `app.py`'s comments at both
call sites for the full reasoning.

The current implementation makes that caveat conditional: it is rendered
only when unresolved Shopify COGS skips exist in the selected period, and it
counts those sales instead of describing all Shopify sales as uncosted.

Where each caveat appears, current state:

| Issue | Screen | Treatment |
|---|---|---|
| 1. Deposits booked as revenue | — | **Fixed.** No caveat; classification is now accurate. |
| 2. Transfers not posted | — | **Fixed.** No caveat; transfers post via Transfer Clearing. |
| 3. No closing entries | Balance sheet | Current earnings labelled as its own line (not a defect) |
| 4. Inventory drifts negative | — | **Fixed.** Inventory no longer misstates. |
| 5. COGS unresolved for some sales | Profit & Loss | Conditional count and sync-log link under Cost of goods sold |

`is_balanced` is close to a tautology: `_validate_journal_entry` rejects
unbalanced entries at write time and a SQL CHECK constraint enforces
one-sided lines, so assets = liabilities + equity essentially cannot fail.
It is still worth displaying as a trust signal, framed as a confirmation
rather than a live audit.

## Structure

One new top-level tab, `Ledger`, placed between Invoices and Tax so the two
accounting surfaces sit together. Requires a `NAV_TABS` tuple and a
`NAV_ICONS` entry in `ui.py`.

Sub-views dispatch on `?tab=`, mirroring `_tax_nav` (`app.py:5209`) and using
the generic `nav` / `nav a.active` styling already in `ui.py:114-127`:

`Sync` · `Trial balance` · `Profit & Loss` · `Balance sheet` · `Accounts`

All five are server-rendered f-strings into `render_shell`, with mutations as
POST → 303 → banner. No JSON endpoint and no JavaScript file: nothing here
needs interaction without a reload, and expandable statement groups use
native `<details>`. This keeps the no-build-step property intact.

## Build order

Sync → Trial balance → Profit & Loss → Balance sheet → Accounts.

Sync comes first because no report is trustworthy until the user can see what
did not post. Trial balance comes before the statements because it is the
cheapest screen that proves the ledger is sound.

## Screen 1 — Sync

Default tab. Route `GET /ledger`, action `POST /ledger/sync`.

**Header action.** A single `Sync now` button. Manual, because
`sync_ledger_from_sources()` is manual — no implied automation.

**Freshness.** Two genuinely different timestamps, both shown:
when source data was last pulled from Plaid/Shopify (`synced_at` on
`plaid_transactions` and `shopify_payouts`) and when entries were last posted
to the ledger (`MAX(ledger_source_imports.imported_at)`). Reuses the existing
`.conn-tested` treatment, including its stale state. Timestamps written by
the ledger are UTC-aware; the existing `_conn_tested_line` compares naive
datetimes, so ledger freshness needs its own UTC-correct comparison rather
than reusing that helper directly.

**Source tiles.** A `.dash-grid` of four `.section` tiles — Plaid
transactions, Shopify orders, Shopify payouts, Shopify COGS — each showing
entries posted, and needs-attention where that source records skips. Tiles
are `.section`, not `a.dash-card`, because there is no per-source drill-down
target; making them anchors would promise a click that does not exist.

Counts are **standing state**, not last-run deltas, because the page can be
reloaded independently of a sync. Per-run created counts go in the banner.

Only posted counts and recorded skips are shown. An "awaiting import" count
is deliberately omitted: Plaid transfers and zero-amount rows are skipped
silently without a skip record, so any such number would overcount. Better
absent than wrong.

**Exceptions table.** Reads `ledger_source_import_skips`, newest first.

| Source | Reference | Reason | Detail | Last seen |

`reason` is rendered with underscores stripped, matching
`ledger_verify.label_reason`. `detail` is already written as a human sentence
by `_log_unmatched_payout` and goes on screen verbatim. Empty state is a
`.none-note`: "Nothing skipped. Every source record posted."

Conditionally-rendered content shows a calm all-clear state rather than
vanishing (anti-patterns #8).

## Screen 2 — Trial balance

A plain table from `ledger.trial_balance()`: Code, Account, Type, Debit,
Credit, Balance. Numbers use `.num` (right-aligned, tabular, mono).
Sub-accounts indent by `parent_account_id`. A totals row proves debits equal
credits.

No date filter, because `trial_balance()` does not take one. Deliberately the
least decorated screen in the app — it is a verification tool, not a
dashboard.

## Screen 3 — Profit & Loss

`.range-form` From/To, defaulting to the current month, feeding
`ledger.profit_and_loss(start, end)`.

Collapsed to five rows — Revenue, COGS, Gross Profit, Operating Expenses, Net
Income — each expanding via native `<details>` to its constituent accounts.
Subtotals are separated by a hairline rule rather than heavier type.

A standing warn-toned note sits under Sales Revenue:

> Includes every bank deposit, not only sales. Owner contributions, refunds,
> and loan proceeds are not yet separated, and transfers are not posted at
> all. Revenue is overstated.

The page states its basis: double-entry ledger, Plaid and Shopify sources.

## Screen 4 — Balance sheet

An as-of date input feeding `ledger.balance_sheet(as_of)`. Assets,
Liabilities, and Equity, with `current_earnings` on its own labelled line:
"Current earnings (not yet closed to equity)."

The balance check renders as confirmation — "Assets = Liabilities + Equity"
with the difference shown — not as an alarm.

## Screen 5 — Accounts

The chart of accounts from `ledger.list_accounts()`, grouped by type with
counts, parent/child indented.

**Read-only, and not by choice:** `ledger.py` exposes `list_accounts` and
`account_by_code` and no create or edit function. The screen says so plainly
rather than showing controls that cannot work.

Per-account "last activity" is omitted from v1; it needs a query that does
not exist yet. Shipped instead with an in-use / not-yet-used status per
account, read from `trial_balance`'s lifetime totals, which answers the
useful part of the same question without a new query: on the seeded chart,
13 of 22 accounts have never been posted to. Balances are deliberately not
repeated here — that is the trial balance's job.

## As-built notes

Deviations from the design above, all made during implementation and
recorded in their commits:

- **Zero-activity rows are filtered** on the trial balance and inside the
  P&L's expandable groups, with a count where they are omitted. The spec did
  not mention filtering. A zero row changes no total, and on a young ledger
  listing all 22 accounts buries the handful that moved.
- **Shared statement CSS is `.stmt-*` / `.statement`**, not `.pnl-*`. The
  Profit & Loss and Balance sheet are the same visual grammar, so the class
  names say so.
- **Negative figures carry colour as well as sign**, reusing
  `.amount.expense`. The minus sign still carries the meaning alone, so
  colour is never the only cue.
- **Known narrow-width nit:** at five tabs the sub-nav scrolls horizontally
  and the active tab can start out of view. Pre-existing and shared with the
  Tax workspace's four-tab nav. Deliberately left consistent with Tax rather
  than diverged from; to be fixed for both navs together, not piecemeal.

## Explicitly out of scope for v1

- **No raw journal-entry browser.** Wrong audience — Klearing targets owners
  doing their own books, and the exceptions table answers "what happened"
  better than a debit/credit stream.
- **No manual journal-entry form.** `create_journal_entry` would support it,
  but hand-keyed entries invite unbalanced-books support burden before there
  is demand for them.
- **No new chart types.** `charts.py`'s five types stand; these screens are
  tables and need none of them.
- **No backend changes to `ledger.py` or `ledger_sync.py`.** The revenue
  classification fix is a separate branch.

## Process

Tier 3 under `SECTION_PLAYBOOK.md`: new routes, new nav entry, structural.
Branch `feat/ledger-ui`, PR, no merge without review.

- Load `skills/klearing-ui/SKILL.md` before touching render code.
- Every colour from a token; no literal hex outside `:root` and `CHART_DEFS`.
- Numbers in mono; `.mixed` for any word-bearing value.
- Verify at ≤375px, not just at the 640px breakpoint edge.
- Test on an isolated port and data directory. Never probe 8642.
