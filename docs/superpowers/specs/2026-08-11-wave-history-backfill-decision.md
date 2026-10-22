# Open decision: the Wave-only transaction history

**Status:** decided and executed for the unambiguous subset on 2026-08-19.
**Raised:** 2026-08-11, during Phase 2.
**Cost of delay:** grows with every approved transaction.

## The situation

Klearing's approved bookkeeping history exists **only in Wave**. Verified read-only against
`%LOCALAPPDATA%\Klearing` on 2026-08-11:

| | |
|---|---|
| Approved `pending_items` | 23, all carrying a `wave_transaction_id` |
| Date range | 2026-06-26 to 2026-07-27 |
| Total | $1,304.89 |
| Denied | 3 |
| `journal_entries` / `journal_lines` | **0 / 0** |
| Bank adapter | Mercury (`plaid_transactions` is 0) |

Phases 3 to 5 remove Wave. Unless something is done, the native Ledger — which is the sole basis
for the P&L, the balance sheet, and the tax projection — starts empty at the cutover and this
history is represented nowhere in the books.

## This is a backfill, not a reclassification

Worth stating plainly, because the two get conflated and they are different jobs:

- **Reclassification** would mean the native Ledger holds entries with *wrong* categories that
  need correcting. That is **not** the situation. The Ledger is empty, and it was never wrong —
  the buggy `_sync_plaid` path could not touch this install because the bank is Mercury and it
  only ever read `plaid_transactions`.
- **Backfill** means replaying history the Ledger simply does not have.

Backfill is the lower-risk of the two: nothing existing is rewritten, entries are only added.
There is no "correct the past" step and no possibility of clobbering a good entry.

## Why it should not drift

Every approved transaction from here on adds to the set. Phase 1 changed this going forward —
approvals now post natively — so the gap is bounded to transactions approved **before** Phase 1
shipped. But it does not shrink on its own, and the longer the decision sits the more likely the
mapping questions below get answered from memory rather than from records.

## The options

1. **Backfill from `pending_items`.** Everything needed is already local: vendor, amount, date,
   direction, and the chosen category. Replay the 23 approved rows through the same path
   approval uses (`app._post_one_to_ledger`), under their existing
   `plaid:transaction:<ext>` references so the dedup guard applies for free.
2. **Accept the seam.** Native books start at cutover; Wave history is exported once for the
   record. Simplest, but the P&L and tax projection cover only part of FY2026.
3. **Re-review.** Surface the 23 as review items and confirm each. Most faithful to the approval
   model, slowest, and re-asks questions already answered once.

## What blocks option 1

The stored categories are **Wave account names** and must be mapped to native Ledger accounts.
This is a per-category money decision and must not be guessed:

| Wave category (count) | Native account | Notes |
|---|---|---|
| Uncategorized Income (7) | `4190` Uncategorized Income | maps cleanly |
| Purchases – Parts & Materials (7) | **undecided** | `1200` Inventory or `5000` COGS — materially different: one capitalizes, the other expenses immediately |
| Owner Investment / Drawings (5) | `3000` Owner's Equity | direction decides contribution vs draw |
| Uncategorized Expense (3) | `6080` Uncategorized Expense | maps cleanly |
| Owner's Equity (1) | `3000` Owner's Equity | maps cleanly |

**Parts & Materials is the one that needs a real answer** — it is 7 of 23 transactions, and
inventory versus COGS changes both the balance sheet and the tax position.

## Recommendation

Option 1, decided before Phase 5 rather than during it, with the category mapping written down
here once it is settled. A backup of the pre-Phase-2 database is at
`Klearing 2/backups/klearing-pre-phase2-*.db` if any of this needs to be reconstructed.

## Decision addendum — 2026-08-19

Ben explicitly chose to exclude the seven **Purchases – Parts & Materials**
transactions ($175.93 total) from the native-ledger backfill as an immaterial
historical balance on a card no longer in active use. They remain intact and
queryable in the Wave-only `pending_items` history; no Inventory, COGS, or
other native category was guessed for them.

The other 16 transactions were carried forward using the mappings above. The
previously investigated roughly 2x discrepancy against the bank statement was
confirmed to be a Wave double-counting artifact: actual bank spend was about
$671, not the duplicated Wave-reported figure. This context explains the
historical source discrepancy; it does not rewrite any individual source row.
