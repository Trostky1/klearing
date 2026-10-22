# Klearing: December release research and decision dossier

Research pass: 2026-10-21. This document expands `DECEMBER-RELEASE-WORKPLAN.md`.
It is a design and engineering brief, not an instruction to implement every
competitor feature. Current source code outranks older project documents.

## 0. Product thesis and release boundary

**Candidate promise:** A local desktop bookkeeping workspace for a solo
business owner: bring in bank activity, review each decision, see where the
money went, reconcile the books, and take a portable copy of the records.

The December portfolio release succeeds if a new person can use fictional
data to complete that loop without an account at every optional provider.
The product should communicate three states distinctly: imported bank facts,
review decisions, and posted ledger entries. A displayed number needs a
source, period, basis, exclusions, and timestamp. A feature outside this loop
must earn its navigation and testing cost.

**Candidate showcase path:** first launch -> demo/statement import -> Review
-> categorized and matched entries -> Summary -> Ledger -> Bank vs books ->
export and backup. This is a target to verify against real screens, not a
claim that the current application already supports every step smoothly.

### Research method and REA boundary

The user suggested [REA](https://github.com/morluto/rea). Its documented
tools can inspect local desktop artifacts, website/CDP targets, JavaScript,
network captures, and SQLite snapshots. For this pass, REA inspected a fresh,
synthetic database created with `db.init_db()` in an isolated test directory;
it reported 45 tables, including `pending_items`, `bank_transactions`,
`journal_entries`, `journal_lines`, `ledger_source_imports`, and
`ledger_source_import_skips`. This confirms a broad persisted surface in a
new database, not the behavior or data quality of a real installation.

The signed-in Wave tab is available in Edge, so its visible workflows were
inspected read-only through the accessibility tree. There is still **no REA
finding about Wave's internals**: no Wave binary or REA CDP target was
available, and this account is in an initial Starter/setup state with no
transaction records shown. We did not create, upload, edit, or export account
data. This makes screen structure observable, but leaves actual transaction
matching and closed reconciliation behavior to public documentation or a
populated test business. Do not transplant proprietary source, assets,
branding, or private API calls. Implement the user need using Klearing's own
architecture and tests.

## 1. Scope and feature audit

### What is already here

The repository has server-rendered FastAPI UI, a native double-entry ledger,
bank adapters, manual file import, review queues, reconciliation, Shopify,
invoices, contacts, inventory, tax, marketing integrations, AI, exports,
backup, Windows and macOS build jobs, and dozens of regression scripts.
`skills/klearing-section/references/section-inventory.md` describes 15
sidebar items and many secondary routes, but was generated in July. Its route
inventory must be regenerated against current `app.py` and `ui.py`.

REA's synthetic schema inventory is a warning against deleting feature code
merely because a navigation item disappears. Historical records and schema
migrations can outlive their UI. The Wave-only history decision in
`docs/superpowers/specs/2026-08-11-wave-history-backfill-decision.md` explicitly
retained seven ambiguous Parts & Materials transactions rather than assigning
them an invented inventory or COGS meaning.

### Decision rubric for every feature

Score each surface 0-2 on: (a) supports the core loop, (b) reliable with
fictional and existing data, (c) understandable without training, (d) worth
its setup cost, (e) demonstrable without paid or private credentials. Record
its dependencies, tables, routes, jobs, exports, migrations, and regression
coverage. Then choose one of four treatments:

| Treatment | Condition | Release behavior |
| --- | --- | --- |
| Showcase | Essential and trustworthy | Primary navigation, onboarding, screenshots, full end-to-end test. |
| Available | Useful and verified | Secondary navigation or contextual entry, concise documentation. |
| Shelve | Incomplete or distracts | Remove from primary navigation and promises; retain safe read paths and data. |
| Retire | No user need and no data dependency | Remove through a planned migration and compatibility review. |

**Initial hypothesis, pending usage and dependency audit:** showcase Review,
Summary, Ledger, Bank vs books, import, backup/export. Keep Settings and
Connections accessible. Evaluate Shopify, invoices, contacts, vendors, tax,
inventory, Growth, follow-ups, AI, and marketing integrations individually.
Do not remove an existing user's accounting history to make a cleaner demo.

**Deliverable:** a route-by-route matrix with owner action, source, value,
failure mode, dependency, treatment, and reason. **Gate:** every visible
primary route has a working task; every shelved route has an explicit data
retention/upgrade decision. **Main risk:** cutting navigation without a way
for a current user to inspect historical data.

## 2. Financial correctness and metric contract

### Source layers observed in code

`app.py:summarize()` totals approved and pending `pending_items`, separates
transfers from income and expense, and feeds `/summary`. The Summary chart's
8-week/12-week/12-month trailing window is independent of its date filter,
which the page currently discloses. The CSV export uses approved items in the
chosen range. `ledger.py` provides journal-based statements and trial balance;
`ledger_sync.py` imports source transactions into that ledger. The older
`backend/services/financial_statements.py` identifies itself as a frozen
Wave-era regression oracle rather than current production output.

**Inference to test:** approved Review totals and formal P&L may differ by
design because their source sets, posting status, historical backfill, and
account mapping differ. The answer cannot be “the math looks close.” Create
a bridge report explaining each difference from source record to journal
entry, with explicit skip reasons and no speculative reclassification.

### Metric contract template

For each displayed amount, write: label; formula in integer cents; source
tables/functions; account/business scope; inclusive date boundaries and
timezone; cash/accrual or imported/approved/posted basis; sign convention;
pending/removed/refunded treatment; exclusions; freshness; empty and error
states; export equivalent; golden fixture. The first inventory should cover:

| Area | Values requiring proof | Likely failure class |
| --- | --- | --- |
| Home | Cash, work waiting, monthly income/outflow, health score | Live bank balance vs ledger timing; stale async value. |
| Summary | Approved/pending totals, transfer, net, categories, trend | Review items vs posted entries; independent chart range. |
| Ledger | Trial balance, P&L, balance sheet, source sync skips | Missing opening balances, skipped source, refund and transfer treatment. |
| Bank vs books | Bank balance, book cash, pending effect, residual | Different dates/accounts, in-flight entries, bank sign. |
| Shopify and inventory | Gross/net sales, fees, payouts, COGS, margin | Refund timing, payout reserves, historical cost snapshot. |
| Tax and exports | Taxable income, projected tax, month close and CSV | Basis, category mapping, prior-period changes, rounding. |

### Fixtures that matter

1. Exact cents and round trips, including zero, negatives, large amounts,
   and amounts that binary floats cannot represent exactly. Python's Decimal
   documentation explains why decimal representation matters; Klearing's
   ledger already uses integer cents.
2. Bank withdrawal/deposit conventions across Mercury, Plaid, SimpleFIN,
   and statement files. The same economic event must show the same direction.
3. Pending -> posted replacement, modified posted item, and removed item.
   Plaid documents these as distinct updates; a pending authorization can
   disappear or settle with a different amount and date.
4. Duplicate import of the same source; manually created invoice payment
   later appearing in a bank feed; two same-amount real transactions.
5. Transfer between two owned accounts, owner contribution, loan proceeds,
   card payment, refund, chargeback, fee, and Shopify payout net of fees.
6. Opening balances and mid-life adoption. Preserve the August 2026 Wave
   backfill exception; do not let a demo imply full historical coverage.
7. Inventory acquisition before/after sale, missing cost snapshot, partial
   refund, and sold item with no verified cost. Never price an old sale with
   today's cost.
8. Date boundary at month/year end and a reconciliation period crossing a
   posting-date change. Reports and export for the same basis must agree.

Wave explicitly models transfers as movements between two accounts so they
do not inflate income or expense. Wave's reconciliation flow starts with a
statement end date and closing balance, then identifies unmatched items and
the difference. QuickBooks separates **match** to an existing record from
**categorize** into a new one before the bank item affects books. These are
better benchmarks than copying a dashboard's visual totals.

**Decision:** Klearing's current live “Bank vs books” residual is useful for
continuous monitoring, but is not equivalent to a closed, statement-period
reconciliation. Evaluate adding statement-period matching only after the
core math contract is proven; label the current check accordingly.

**Deliverable:** metric dictionary, source-to-journal bridge, frozen fixture
set, discrepancy register. **Gate:** balance sheet equation and trial balance
pass; same-basis UI/export values agree to the cent; every discrepancy has a
named source or a visible unresolved status. **Main risk:** changing posting
logic while trying to fix a display mismatch. Isolate such changes and test
old databases as well as clean ones.

## 3. Import, Review, and reconciliation experience

Wave's transaction screen lets users categorize to accounts, merge duplicates,
and reconcile statement periods. QuickBooks places bank imports in a “For
review” stage and distinguishes matching existing records from creating new
ones. Actual Budget documents file import, reconciliation, transfers, and
rules; it is a useful open-source example for understandable local workflows.

Map Klearing's current user actions in this order:

1. **Connect/import:** show source, selected business/account, last successful
   sync, date coverage, new/modified/removed count, and import errors. Detect
   cross-source overlap without declaring two same-amount items duplicates.
2. **Review:** show what the bank supplied and what Klearing proposes. Make
   match-existing, categorize-new, transfer, defer, and exclude distinct
   decisions if supported; never silently post. Current focused Review drawer
   and separate Deny action should be assessed in rendered UI, not against
   the obsolete July critique.
3. **Post:** show the affected account and a concise explanation; preserve
   source ID, actor, timestamp, and reason. Bulk and single approval must call
   one posting implementation.
4. **Recover:** explain errors with a next action. A rejected source should
   remain reviewable. Correction must preserve an audit trail; “undo” needs
   defined accounting semantics before it is promised.
5. **Reconcile:** compare account and period on the same basis. Show exact
   included items, pending differences, opening balance, and remaining gap.
   Distinguish continuous live check from a completed statement period.

**Deliverable:** task map and a fictional 20-transaction dataset that exercises
all five steps. **Gate:** a new tester can finish the path and explain why
two numbers differ. **Main risk:** trying to automate category guesses when
the correct choice is a transfer, loan, equity, or existing invoice match.

## 4. UI and Impeccable redesign

The July Impeccable report scored the old app 27/40, but current code already
contains a labelled mobile bottom bar and More sheet, a Today region, a
focused Review drawer, and async retry/freshness patterns. Re-running an old
list would waste effort. Capture current pages with isolated fictional data
at approximately 1440px, 900px, 640px, and 375px widths. Include setup,
empty, loading, error, populated, and completed states.

### Reference study protocol

**Observed in the signed-in Wave interface:** the dashboard leads with an
expandable “Set up your business” checklist grouped into bookkeeping and
invoice outcomes, then separates operational overview cards (overdue,
cash flow, P&L, expense breakdown, net income, receivables/payables). The
dashboard's sample figures are explicitly marked “Sample”; do not mistake
them for this account's actual ledger. The Transactions route offers three
clear entry points: automatic import (marked Pro), CSV statement upload, and
manual income/expense entry. This supports a useful Klearing design choice:
make a free, fictional-data or file-import demo the first action and defer
credential setup until it provides value.

The Reports index groups reports by task: financial statements, taxes,
payroll, customers, vendors, and detailed reporting. P&L and Balance Sheet
screens expose date/as-of controls, report type (including Accrual / Paid &
Unpaid), export, Summary/Details, and a clear account-level statement table.
P&L also has compare-to-prior-period. This is a strong information pattern
for Klearing: let the reader scan a summary, inspect account detail, then
export the same selected period. Klearing should label its own basis directly
beside the report heading and make sure those controls and export stay in
agreement. Since the inspected account has no posted records, no numerical
or behavior conclusion about Wave matching has been inferred from it.

Choose one reference for structure and one for interaction. Record a small
table: task, reference behavior, why it helps, Klearing adaptation, what to
avoid, screenshot/source. Wave is useful for account/reconciliation language;
QuickBooks for match-versus-add decisions; Actual for local import and rules.
Do not copy a competitor's logo, exact screen, artwork, proprietary code,
or wording. Create an original Klearing system from observed principles.

Use Impeccable on **rendered screenshots and task states**: critique -> rank
actual defects -> sketch a changed hierarchy -> implement one shared pattern
-> compare before/after -> check keyboard and narrow widths. Preserve the
repo's design tokens, five chart types, native dialogs, and server-rendered
stack unless a separately justified architectural decision changes them.

Prioritize: (1) first-run and demo data choice; (2) one next action on Home;
(3) bank source and posting status in Review; (4) Summary basis and date
language; (5) Bank vs books explanation; (6) navigation reduction; (7) polish.
Use transaction detail and report explanations as the visual design's center,
not decorative dashboard cards.

**Deliverable:** current-state screenshots, annotated task flow, reference
matrix, revised components, and before/after captures. **Gate:** every
financial number has a nearby basis and drilldown; primary tasks remain
discoverable by mouse and keyboard; no horizontal page overflow at narrow
width. **Main risk:** an attractive borrowed composition hiding incomplete
accounting states.

## 5. Release hardening, privacy, and packaging

The current workflow builds Windows and macOS on tag pushes and attaches
artifacts to a release. PyInstaller output is platform-specific, so each
platform needs its own real smoke test. The existing build workflow is a
packaging pipeline, not a pre-release test gate; it has no visible regression
job before release attachment. `README.md` also makes broad integration and
pricing claims that need current verification against the shipped build.

### Release test matrix

| Scenario | Required evidence |
| --- | --- |
| Fresh install | Launch without Python/terminal, setup, create business, use sample import. |
| Upgrade | Restore a copy of older fictional DB; migrations idempotent; old records viewable. |
| Data integrity | Backup then restore; compare counts, amounts, source IDs, and statements. |
| Security | Bind only 127.0.0.1, reject foreign Host, protect mutation routes and secrets. |
| Offline | Core records/reports work without network; optional integrations fail clearly. |
| Provider failure | Timeout, revoked token, partial sync, repeated sync, removed transaction. |
| Public archive | No `.env`, tokens, real account IDs, personal data, logs, raw backups, or private screenshots. |
| Reproducibility | Pin supported runtime/dependencies, run regressions before packaging, record build OS and artifact hash. |

`scripts/localhost_security_regression.py` could not start a real server in
the current sandbox; its static checks passed, but its end-to-end result is
open. The app-render suite also stalled here. Rerun both in a bounded,
ordinary local environment before a tag. The money, ledger, bank-sign, tax
comparison, and UI consolidation tests passed in the earlier baseline.

**Deliverable:** release checklist with actual artifacts and results, CI
test gate, upgrade/restore fixtures, security and content audit, and known
limitations. **Gate:** no tag solely because the build job passed. **Main
risk:** the public archive or example data containing real credentials or
financial history.

## 6. Open-source handoff

An outsider should be able to answer within five minutes: What is Klearing?
Who is it for? What can I try with no bank account? How do I install it?
Which financial results are authoritative? Which features are experimental?
Where is data stored, and how do I leave with it?

Ship a short README with one product image, a 60-90 second demo, install and
sample-data path, supported-platform matrix, local-data explanation, feature
status table, limitations, development setup, test commands, contribution
scope, and license. Keep deeper integration guides in `docs/`. The existing
MIT license and CONTRIBUTING file are starting points, not substitutes for a
fresh first-run test. Plaid's current documentation says its Trial permits
10 Production Items in the US/Canada; after upgrading, Transactions is a
monthly subscription product per active Item. That makes the README's broad
“no cost to anyone” promise too absolute. Verify the exact supported plan,
geography, and SimpleFIN cost before release. A manual import path makes the
public demo independent of provider approval and pricing.

**Deliverable:** release candidate and evidence-backed README. **Gate:** a
person unfamiliar with the project can complete the fictional workflow from
the release package and understand limitations. **Main risk:** marketing copy
claiming accounting completeness or provider support beyond tested behavior.

## 7. Portfolio case study

Tell the product-design story through decisions and evidence, not a feature
count. Suggested sequence: problem and target owner; why local and review
first; messy original scope; research and task model; metric/source mismatch;
an iteration on Review or reconciliation; before/after screens; validation;
shipped result and honest limitations. Show your role in design, code,
research, test creation, and AI-assisted work precisely.

Capture now: dated baseline screens, navigation inventory, one real bug and
fixture, a feature cut with rationale, a competitor principle adapted into
Klearing, a usability test, a release smoke test, and an end-to-end video.
The strongest claim is measurable: e.g., number of primary destinations
reduced, task steps removed, discrepancies explained, or first-run completion
time. Record the baseline before claiming improvement.

**Deliverable:** public project page, short demo, repository/release link,
and a concise case study. **Gate:** someone scanning for two minutes can
understand the problem, your contribution, a hard tradeoff, and the result.

## Recommended order and decision gates

1. Finish the route, dependency, and metric inventories. Decide the release
   promise and source-of-truth labels before any broad redesign.
2. Fix proven money discrepancies with golden fixtures, one defect at a time.
   Keep ledger changes isolated and verify migration behavior.
3. Test the entire fictional workflow, then use current-state Impeccable
   critiques to redesign the bottlenecks.
4. Shelve peripheral features through navigation and claims first. Retire
   persisted data only when a migration and compatibility plan exists.
5. Harden the packaged release and public content. Tag only after real
   platform smoke tests. Produce the case study from evidence collected above.

### Open questions to answer with evidence

- Does Summary intentionally represent approved review decisions while P&L
  represents posted journal entries, and how is the difference explained?
- Which bank import path is reliable without a paid account for a stranger?
- Can a bank item be matched to an existing invoice/payment rather than
  double-posted? What is the recovery path after a wrong approval?
- Is “Bank vs books” an on-demand balance health check, or should Klearing
  support closed statement periods as a distinct feature?
- Which optional features have actual users or valuable historical records?
- Which OS releases have been smoke-tested on a clean machine, beyond CI?

## Primary research sources

- [REA repository and capability/limitations](https://github.com/morluto/rea)
- [Wave: reconciliation and statement-period workflow](https://www.waveapps.com/blog/what-is-bank-reconciliation)
- [Wave: transfer between accounts](https://support.waveapps.com/hc/en-us/articles/208621616-How-to-create-a-transfer-between-two-accounts-e-g-categorize-a-credit-card-payment-)
- [Wave: automated payment clearing](https://support.waveapps.com/hc/en-us/articles/115004964423-Automated-bookkeeping-with-online-payments)
- [Wave: report export](https://support.waveapps.com/hc/en-us/articles/38600080764692-Export-a-report)
- [QuickBooks: match versus categorize](https://quickbooks.intuit.com/learn-support/en-us/help-article/bank-feeds/match-online-bank-transactions-quickbooks-online/L6qyw0PvP_US_en_US)
- [Actual Budget: import](https://actualbudget.org/docs/transactions/importing/), [reconciliation](https://actualbudget.org/docs/accounts/reconciliation/), [rules](https://actualbudget.org/docs/budgeting/rules/)
- [Plaid: sync updates](https://plaid.com/docs/transactions/sync-migration/) and [pending/posted states](https://plaid.com/docs/transactions/transactions-data/)
- [Plaid: current pricing and Trial limits](https://plaid.com/docs/account/billing/)
- [Python: decimal arithmetic](https://docs.python.org/3/library/decimal.html)
- [PyInstaller: platform-specific output](https://pyinstaller.org/en/stable/operating-mode.html)
