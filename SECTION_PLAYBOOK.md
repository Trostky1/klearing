# Klearing Section Improvement Playbook

> Load this file at the start of every section improvement session.
> Follow phases in order. Do not skip phases.

This playbook is the phase checklist. The actual design rules and
research live in two skills — load them, don't re-derive them each time:

- **`skills/klearing-ui/SKILL.md`** — the design constitution (tokens,
  chart patterns, and a running list of real anti-patterns already found
  and fixed). Load this for Phase 1.
- **`skills/klearing-section/SKILL.md`** — this same workflow, plus a
  section inventory (screen name → render function) and the competitor
  benchmark research. Load this for Phases 2-3.

Last full cycle run: the July 2026 full-app sweep — EVERY section in
the inventory has now had a complete cycle, ending with
Home/Uncategorized, Pending, Unlock, Audit log, 1099 contractors,
Sales tax nexus, Unit economics, Tools hub, Settings,
Automation, Connections, Logs, Growth,
Insights, Invoices, Follow-ups, Vendors, Summary, Reconcile, and
Home/Dashboard — see
`skills/klearing-section/references/section-inventory.md` for what
shipped on each, and `skills/klearing-ui/references/anti-patterns.md`
for what was found along the way.

---

## How to invoke

```
Read SECTION_PLAYBOOK.md and run a full improvement cycle on the [SECTION NAME] section.
```

---

## Phase 1 — Visual Audit

**Goal:** Catch every visual inconsistency before touching features.

1. Load `skills/klearing-ui/SKILL.md` and its three references
   (design-tokens.md, chart-patterns.md, anti-patterns.md)
2. Open the target section's render function(s) — check
   `skills/klearing-section/references/section-inventory.md` for the
   exact function name(s) if unsure
3. Screenshot or describe the current rendered state, including at a
   narrow (~375px) window width, not just desktop
4. Check every value (color, spacing, font, radius) against
   `design-tokens.md`; check every chart against `chart-patterns.md`;
   check the whole section against `anti-patterns.md` — if a finding
   matches a known pattern, say so and apply the known fix rather than
   re-diagnosing from scratch
5. Flag every deviation — hardcoded colors, wrong font, broken spacing,
   misaligned elements, illegible text at narrow width
6. Output: **Visual Issues List** (numbered, ranked by severity)

---

## Phase 2 — Feature Audit

**Goal:** Document exactly what the section currently does.

1. List every user action available in this section (clicks, inputs, filters, exports)
2. List every data point currently displayed
3. List every chart or visualization
4. Note what's missing, broken, or placeholder
5. Output: **Current Feature Inventory** (what exists vs what's stubbed/missing)

---

## Phase 3 — Competitor Benchmark

**Goal:** Know what best-in-class looks like for this specific section.

Start from `skills/klearing-section/references/competitors.md` — the
research is already done for Monarch Money, YNAB, Copilot, Tiller Money,
and Lunch Money. Re-search only if the section doesn't map to anything
already covered there, or if the reference is stale.

For each relevant competitor:
- What data does their equivalent section show?
- What interactions does it support?
- What visualizations do they use?
- What does Klearing have that they don't?
- What do they have that Klearing doesn't?

Also check `competitors.md`'s "what to explicitly NOT chase" list before
proposing anything — some competitor features have already been
evaluated and declined for Klearing's specific register.

Output: **Gap Analysis Table**
| Feature/Visual | Klearing | Monarch | YNAB | Copilot | Priority |
|---|---|---|---|---|---|

---

## Phase 4 — Enhancement Plan

**Goal:** Decide what to build before touching any code.

Using Phase 1-3 outputs, produce a prioritized list:

**Tier 1 — Quick wins (< 1 hour each)**
- Visual fixes from Phase 1
- Simple data additions (new metric, new label)

**Tier 2 — Feature additions (1-4 hours each)**
- New interactions
- New filters/toggles
- New data visualizations

**Tier 3 — Bigger lifts (> 4 hours)**
- New data sources
- New chart types
- Structural layout changes

Get explicit approval on Tier 2 and Tier 3 items before proceeding. If
later asked to "do all fixes," that means every item explicitly
presented and tiered — it does not implicitly include anything flagged
with a recommendation to skip; call those out by name so a one-word
correction is all it takes if they were actually wanted.

---

## Phase 5 — Implement

**Order of operations:**
1. Visual fixes first (low risk, immediate improvement)
2. Tier 1 features
3. Tier 2 features (one at a time, test between each)
4. Tier 3 features (branch first)

**Rules:**
- Never break existing functionality
- All numbers must use monospace font — hybrid word+number phrases use
  `.dash-value.mixed` instead (see anti-patterns.md #6)
- All new colors must pull from `skills/klearing-ui/references/design-tokens.md`
- New charts must use `skills/klearing-ui/references/chart-patterns.md`'s
  five existing types — do not introduce new chart libraries
- A conditionally-rendered card should show a calm "all clear" state
  when checked-and-fine, not vanish entirely (see anti-patterns.md #8)
- Run regression suite after every Tier 2+ addition

---

## Phase 6 — Verify

1. Run full regression suite
2. Check section at narrow window width (≤375px, not just the 640px
   breakpoint edge) — `.dash-grid`-style layouts can overflow well
   before the breakpoint itself looks broken (see anti-patterns.md #11)
3. Confirm new text is readable at that narrow width by measuring actual
   rendered pixel height, not just the declared font-size (see
   anti-patterns.md #1 — declared size alone doesn't prove legibility
   once an SVG viewBox is scaling it down)
4. Confirm no hardcoded colors slipped in — grep the diff for literal
   hex values outside `ui.py`'s `:root` block and `CHART_DEFS`
5. Output: **Change Summary** (what was added, what was fixed, test results)

---

## Phase 7 — Commit

```
git checkout -b improve/[section-name]
git add -A
git commit -m "improve([section]): [summary of changes]"
git push origin improve/[section-name]
# Open PR — do not merge without review
```

---

## Section Order (recommended)

Work through sections in this order based on user impact:

1. **Home / Dashboard** — most seen, highest impact — ✅ cycle complete
2. **Transactions / Reconcile** — core workflow — ✅ cycle complete
3. **Summary** — second most viewed — ✅ cycle complete
4. **Vendors** — data density opportunity — ✅ cycle complete
5. **Insights** — visualization opportunity — ✅ cycle complete
6. **Growth** — aspirational, save for last — ✅ cycle complete
7. All remaining screens
   - **Follow-ups** — ✅ cycle complete (run out of recommended order,
     by request)
   - **Invoices** — ✅ cycle complete (run out of recommended order, by
     request — follow-on from Follow-ups' flagged confirm_onsubmit
     duplication)
   - **Logs** — ✅ cycle complete
   - **Connections** — ✅ cycle complete
   - **Automation** — ✅ cycle complete
   - **Settings** — ✅ cycle complete
   - **Tools hub** — ✅ cycle complete
   - **Tools / Unit economics** — ✅ cycle complete
   - **Tools / Sales tax nexus** — ✅ cycle complete
   - **Tools / 1099 contractors** — ✅ cycle complete
   - **Tools / Audit log** — ✅ cycle complete
   - **Unlock** — ✅ cycle complete (audited clean, zero changes)
   - **Pending** — ✅ cycle complete
   - **Home / Uncategorized** — ✅ cycle complete (audited clean,
     zero changes)
