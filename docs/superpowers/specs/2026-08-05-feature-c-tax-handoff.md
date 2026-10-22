# Feature C tax handoff contract

This prerequisite branch owns the canonical contractor candidate tracker. The
Feature C accountant checklist must consume that tracker after this branch is
merged; it must not create a second contractor query or checklist-specific
classification path.

## Required checklist behavior

- Link the 1099 / contractor checklist item to `/tools/contractors`.
- Use the shared `backend.services.contractor_handoff` contract and the
  existing `contractor_candidates()` data. The canonical positive status is
  **“Candidates identified; eligibility not verified.”** When there are no
  candidates, say **“No candidates identified; eligibility not verified.”**
- Keep the item informational. Candidate detection is based on local category
  and payment history; it does not establish 1099-NEC eligibility.
- Do not infer eligibility from business type, vendor name, or payment amount.
  Entity type and W-9 details remain outside Klearing's knowledge.
- Do not use a “not tracked” stub: the tracker already exists and must be the
  linked source of truth.

The durable regression contract is
`scripts/feature_c_handoff_regression.py`. It must remain green when Feature C
adds its checklist surface.

## Projection limitations carried forward

- State tax is profile-driven. Tier 1 no-income-tax states return an explicit
  zero; verified Tier 2 flat-rate states return a state estimate; all other
  states (including NH and WA) return `null` with an honest **“Not available”**
  warning. When state tax is unavailable, the displayed total is labeled
  **“Federal + self-employment only”** so it cannot be mistaken for a complete
  multi-state total. NH interest/dividend and WA capital-gains treatment should
  be revisited only after estimated-other-income is split into typed inputs.
- The current tax year is hardcoded to 2026 in the projection service. There is
  no automatic 2027 rollover. Before January 2027, update the tax-year data
  (brackets, deductions, rates, due dates), migrate the settings value, and add
  a year-boundary regression so the app cannot continue presenting stale 2026
  planning data as current.
