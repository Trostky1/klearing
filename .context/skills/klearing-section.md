---
name: klearing-section
description: Section-by-section improvement workflow for Klearing — visual audit, feature inventory, competitor benchmark, tiered plan, implement, verify, ship. Load this when asked to improve, audit, or level-up a specific Klearing screen.
---

# Klearing section improvement workflow

A 7-phase cycle for taking one Klearing screen from "shipped" to
"deliberately better," in order, without skipping straight to code. This
is the same cycle documented in `SECTION_PLAYBOOK.md` at the repo root —
that file is the phase checklist; this skill is the entry point plus the
two references a section audit actually needs.

## When to use this skill

- "Run a full improvement cycle on [section]"
- "Audit [section] and fix what you find"
- Any request to systematically improve one screen rather than make one
  targeted fix

## The phases (detail lives in SECTION_PLAYBOOK.md — this is the map)

1. **Visual audit** — load the `klearing-ui` skill first. Every visual
   finding gets checked against `klearing-ui/references/anti-patterns.md`
   before being reported as new — if it's a known pattern, say so and
   apply the known fix rather than re-diagnosing from scratch.
2. **Feature audit** — what the section actually does today: every
   action, every data point, every chart. Cross-reference
   `references/section-inventory.md` for the render function that owns
   the section, so the audit reads the real current code, not a guess.
3. **Competitor benchmark** — `references/competitors.md` has the
   research already done for Monarch Money, YNAB, Copilot, Tiller Money,
   and Lunch Money. Re-search only if the section under audit doesn't map
   to anything already covered there (e.g. a section with no personal-
   finance-app equivalent, like Reconcile or the Setup wizard).
4. **Enhancement plan** — tier the findings (visual fixes → Tier 1 quick
   wins → Tier 2 feature additions → Tier 3 bigger lifts). **Stop and get
   explicit approval on Tier 2 and Tier 3 before writing any code** —
   this is a hard gate, not a suggestion.
5. **Implement** — visual fixes first, then Tier 1, then Tier 2 one at a
   time (test between each), then Tier 3 (branch first). Every new color/
   spacing/chart must come from `klearing-ui`'s references, not invented
   fresh.
6. **Verify** — full regression suite, narrow-window check, readable-text
   check (verify actual rendered pixel height at ~375px width, not just
   the declared font-size — see anti-patterns.md #1 for why declared
   size alone doesn't prove legibility), no hardcoded colors.
7. **Commit** — branch, commit, push, open a PR. Do not merge without
   review.

## Reference files

- `references/section-inventory.md` — every screen name and its render
  function, extracted directly from `app.py`. Use this to find the exact
  function(s) that own a section before Phase 2, instead of grepping
  cold each time.
- `references/competitors.md` — what Monarch Money, YNAB, Copilot,
  Tiller Money, and Lunch Money actually do, organized so it can be
  scanned for the section under audit without re-running web searches
  every cycle.

## Working agreement carried over from the last cycle

- Klearing's niche (solo bookkeeping approval + Wave reconciliation) has
  no 1:1 competitor among the 5 benchmarked apps — they're consumer
  budgeting tools, Klearing is a small-business books tool. Don't chase
  a competitor feature just because it exists; map it to what Klearing's
  actual user (one person, one machine, approval-queue workflow) needs.
- Customization (draggable/reorderable widgets, à la Monarch) has been
  explicitly evaluated and declined once already — it fights the app's
  deterministic, no-clutter, beginner-friendly register. Don't
  re-propose it without a specific reason the previous call was wrong.
- "All fixes" from an approved plan means every item that was explicitly
  presented and tiered — it does not implicitly include anything that
  was presented with an explicit recommendation to skip. Flag what you
  skipped and why in the final summary so it's a one-line correction if
  the user actually wanted it.
