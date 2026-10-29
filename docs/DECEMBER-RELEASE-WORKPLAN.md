# Klearing December release workplan

Started 2026-10-21. This is a working audit, not a claim that every feature is release-ready.
On 2026-10-22 the first public release was scoped to small-business bookkeeping;
personal finance is a later mode. Public release is requested now, subject to
passing the release checks below.
The portfolio release should demonstrate one complete, reliable workflow:
connect or import transactions, review them, inspect the ledger and summary,
reconcile, then export or back up. Use fictional data for every public image.

## Scope decision sheet

| Surface | Current state | December decision to validate |
| --- | --- | --- |
| Setup, Connections, Review | Implemented; multiple bank paths and review queues | Keep; test a fresh install and one supported demo path end to end. |
| Ledger, Summary, Reconcile | Native double-entry ledger and reports | Keep; verify definitions, source coverage, sign conventions, and date windows. |
| Backup and accountant export | Implemented | Keep after restore and export checks with fictional data. |
| Invoices, Contacts, Vendors | Implemented | Keep only if the flows are complete and fit the core story; otherwise remove from primary navigation. |
| Shopify, Inventory, Tax | Implemented and coupled to financial records | Audit dependencies before simplifying navigation; do not delete historical data or migrations. |
| Growth, marketing insights, follow-ups, AI, optional integrations | Broad optional surface | Defer from the primary demo unless each has a clear user task, current data, and a working recovery path. |

Decide hide versus remove per route after checking dependencies and existing users' data.
The section inventory in `skills/klearing-section/references/section-inventory.md`
was generated in July and must be checked against current routes before use.

## Evidence gathered in the first pass

- `ledger.py` uses integer cents and balanced journal entries; source sync and
  tax projections have dedicated regression fixtures. This is an existing
  foundation, not proof that every displayed metric is correct.
- The July Impeccable critique's mobile icon-only navigation, crowded Home,
  and Review-row findings are partly obsolete. Current `ui.py` has a labelled
  mobile bottom navigation and More sheet; `app.py` has a Today region; the
  UI consolidation regression covers the focused Review drawer.
- `scripts/money_cents_regression.py` had a time-sensitive fixture: it inserted
  an August 2026 reconciliation snapshot, then queried the last day. The
  fixture now uses today's date for that one snapshot and passes 9/9 checks.
- Baseline passes in isolated data: ledger regression; bank sign convention
  8/8; tax projection legacy/native comparison with zero differences; money
  cents 9/9 after the fixture fix; UI consolidation regression.
- The localhost security test confirmed loopback binding and exact Host rules,
  but its real server could not start in this sandbox. Rerun on a normal local
  environment before release. The app render regression stalled in this
  environment and needs a bounded rerun. Neither is recorded as a pass.
- Tracked file names did not show `.env`, databases, logs, or build output.
  This is not a full secret or packaged-artifact audit.

## Next work batches

The release blocker at the top of this list is a usable path without a paid
provider. Today setup step 3 requires a bank, and the OFX/QFX/CSV importer in
Data Health requires an account returned by that bank. A fresh user cannot
complete the advertised free import workflow. Build manual account creation,
statement import with an explicit sign/date/duplicate preview, Review posting,
and a statement balance close in one tested flow. Do not silently bypass the
wizard while leaving the importer unreachable.

1. **Metric contract.** List every money value on Home, Summary, Reconcile,
   Ledger, Tax, and exports with source query, date basis, sign convention,
   exclusions, and freshness. Identify duplicate implementations and
   ambiguous values. Add fixtures for observed discrepancies before edits.
2. **Current UI walkthrough.** Capture Setup, Home, Review, Summary, Ledger,
   Reconcile, and Settings at desktop and narrow widths with fictional data.
   Run Impeccable on rendered states and prioritize actual current defects.
3. **Scope cut.** Trace optional routes, sidebar links, stored data, and
   exports. Hide unfinished features before deleting code; preserve migration
   and read compatibility. Update navigation and documentation together.
4. **End-to-end release check.** Fresh install, sample import, approval,
   ledger reports, reconcile, backup/restore, packaged launch, and upgrade
   from a copy of older fictional data. Test with an isolated port and data
   directory; never use the live 8642 instance.
5. **Public handoff.** Review tracked files and release archive for secrets
   and personal data. Reconcile README claims with the build and supported
   integrations. Add a reproducible sample dataset, screenshots, short demo,
   known limitations, and a tagged release.

## Release gate status (2026-10-22)

| Gate | Status | Evidence / next action |
| --- | --- | --- |
| Product scope | Chosen | Small-business first; personal mode deferred. |
| Provider-free first run | Blocked | Bank required by setup; statement import relies on bank account list. |
| Local regression checks | Passing | Ledger, bank sign 8/8, money cents 9/9, UI consolidation. New PR/push workflow added; hosted run pending. |
| Tracked file-name exposure | Preliminary pass | No tracked `.env`, database, key, or log file found. Contents, history, packages still need review. |
| Packaged launch and restore | Unverified | Run on clean Windows/macOS and older fictional backup. |
| GitHub publication | Done | Published at `Trostky1/klearing`. |

## Portfolio evidence to capture during work

Save dated before/after screenshots, the reason for each feature cut, a
financial discrepancy and its fixture, a usability finding and iteration,
and proof of the final first-run workflow. The case study should clearly
separate personal design and engineering contributions from external tools.
