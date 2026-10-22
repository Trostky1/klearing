# State tax profile boundary (2026)

The state-tax profile is intentionally data-driven: it records the tax base,
federal conformity, deductions/exemptions, self-employment add-backs, local-tax
status, and the last tax year verified. `local_tax.mode` distinguishes an
explicitly tax-free/no-local-tax state (`none`) from a state whose local rules
are not yet populated (`not_populated`). A profile may produce a number only
when it is populated, current, and has no unpopulated local layer.

## Populated tiers

- Tier 1 zero-tax states: AK, FL, NV, SD, TN, TX, WY.
- Tier 2 flat-rate states: AZ, CO, GA, ID, IL, IA, MS, NC, UT.
- Tier 3: every other state, including NH and WA. NH's interest/dividend tax
  and WA's capital-gains tax cannot be separated from the single
  `estimated_other_income` field. Revisit them when income-type inputs are
  separated.

Each populated profile includes the state revenue department reference URL in
`backend/services/state_tax.py`. Graduated-bracket states and states with
municipal layers (including OH, PA, and NY/NYC/Yonkers) remain unavailable until
their rules are sourced and verified.

The 2026 flat-rate constants currently encoded are AZ 2.5%, CO 4.4%, GA 4.99%,
ID 5.3%, IL 4.95%, IA 3.8%, MS 4% after its $10,000 exemption, NC 3.99%, and
UT 4.45%. Utah's 2026 rate revision is recorded by the Utah State Tax
Commission's forms/publications revision log; the other links identify the
corresponding state revenue guidance.
