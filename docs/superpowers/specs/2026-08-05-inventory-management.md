# Inventory Management — canonical acquisition and FIFO COGS

Status: implemented on the inventory workstream branch.

## Accounting contract

Klearing owns inventory accounting quantity and cost. Shopify remains the
operational quantity reference; its cost snapshots do not prove that units
were purchased. Plaid data is evidence only and never supplies a variant,
quantity, unit cost, or funding account by inference.

Every acquisition uses one path and requires:

- Shopify variant identity
- positive quantity
- unit cost in cents
- the date the units were received
- an explicit funding account (bank asset or payable)

The path atomically posts `Dr 1200 Inventory / Cr funding account` and writes
the FIFO acquisition layer. Manual entry and later reconciliation of legacy
quantity-only rows both call this same posting path. Source references make
the journal and layer idempotent.

FIFO is retained. A sale can use only Ledger-backed acquisition layers dated
on or before the sale. If the layers do not cover every unit, the whole COGS
entry is refused and logged; no partial cost, backfill, or today's cost is
guessed.

## Legacy reconciliation

Existing quantity-only acquisition rows are marked `legacy_unposted` by the
table-gated migration. They appear in the existing Review drawer under
`/review?tab=inventory` and do not provide COGS coverage until a user chooses
the historical funding account. If the funding cannot be established, the
user can enter a note and choose the terminal
`acknowledged_unresolvable` state. That state remains visible in history but
is excluded from COGS coverage, so the queue can clear without inventing a
cash movement.

No historical transaction is edited or repriced. Invoice inventory drawdown
is intentionally deferred until invoice lines carry variant identity; it
will reuse the same FIFO path. Contacts/contact_aliases are not changed.

## UI disclosure and downstream dependencies

The P&L caveat is conditional: it names the count of Shopify sales whose COGS
was held back and links to the sync log. Costed sales are no longer described
as missing. Feature B's margin metrics can read posted COGS and report the
remaining unresolved coverage; the metric registry must not fill missing COGS
with zero. The future invoice workstream should consume the same variant-keyed
FIFO layers rather than introduce a second inventory quantity or cost system.

## Verification contract

After migrations and syncs, `scripts/ledger_verify.py` must report
`Assets = Liabilities + Equity: PASS`. Regression coverage includes FIFO
ordering, dated-sale refusal, atomic acquisition journals, legacy queue
reconciliation, terminal acknowledgement, and the existing ledger/UI suites.
