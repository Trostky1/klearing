---
name: klearing-ui
description: Design constitution for Klearing's UI — the dark-ledger design system, chart vocabulary, and the anti-patterns already found and fixed. Load this before touching ui.py, charts.py, or any render_*_page function in app.py.
---

# Klearing UI — design constitution

Klearing is server-rendered HTML from Python (FastAPI + f-strings, one shared
`STYLE` block in `ui.py`, no build step, no CSS framework, no JS framework).
Every screen shares the same shell (`ui.py:render_shell`), the same sidebar
(`ui.py:render_sidebar`), and the same hand-rolled SVG chart library
(`charts.py`). This skill is the contract that keeps all of that one system
instead of twelve slightly-different ones.

Register: **product** — design serves the task (a solo bookkeeper's approval
queue), not a marketing surface. Familiarity beats novelty. See
`references/anti-patterns.md` for what "familiar" has already gone wrong
here once — don't repeat it.

## When to use this skill

- Before writing or editing anything in `ui.py`, `charts.py`, or a
  `render_*_page` / `dash_*` function in `app.py`
- Before adding a new CSS class, color, spacing value, or chart type
- When reviewing a diff that touches HTML/CSS output
- As the visual-audit step inside the `klearing-section` skill's Phase 1

## The three references

- `references/design-tokens.md` — every real color, font, spacing, and
  radius value in `ui.py`, verbatim. Never introduce a value not on this
  list without adding it here first.
- `references/chart-patterns.md` — the five chart types in `charts.py`,
  their signatures, and when to use which one. Never build a new chart
  type or pull in a charting library — extend one of these five.
- `references/anti-patterns.md` — real, already-found problems (not
  hypothetical ones) with the fix that shipped for each, so the same
  mistake doesn't get reintroduced somewhere else in the app.

## Non-negotiables (the short version — see anti-patterns.md for why)

1. **No hardcoded hex.** Every color is a `var(--token)` from `ui.py`'s
   `:root` block. If a value repeats twice anywhere, it's a missing token.
2. **Monospace is for pure numbers/money only.** A hybrid phrase like
   "3 waiting" or "1,240 sessions" gets `.dash-value.mixed` (or the
   equivalent), never bare `.dash-value`/`.amount`/`.totals .value`.
3. **Pill radius scales with height**, never a fixed `rx`. Bars, funnel
   segments, and the gauge all use `rx = height / 2` — that's what makes
   them read as one control vocabulary instead of assorted rounded boxes.
4. **Every chart with an SVG viewBox wider than its container must stay
   legible on a narrow window.** Don't let a chart's text shrink below
   what desktop shows — scroll the chart horizontally instead of
   compressing it (see `figure.chart { overflow-x: auto }` in the mobile
   media query).
5. **Every card that says or implies "click through" must actually be a
   link.** If a card's copy references another page or promises "every
   card opens its full tab," it needs a real `<a href>`, not prose.
6. **New spacing/type values get checked against `design-tokens.md`'s
   existing scale first.** The scale is real (14/16/18/20/22px, not a
   strict 8px grid) — match the nearest existing value, don't invent a
   new one two pixels off.
7. **A card's "nothing to report" state is a calm positive message, not
   an absent card.** An absent card reads as "not checked yet"; a calm
   message reads as "checked, all clear." Only omit a card entirely when
   there's truly nothing to compute yet (see anti-patterns.md).

## How to apply this skill

1. Read `references/design-tokens.md` and `references/chart-patterns.md`
   before writing any new markup.
2. After writing/editing, scan the diff against
   `references/anti-patterns.md` — if a listed pattern appears, fix it
   before calling the work done, don't leave it for a later audit pass.
3. If you're about to introduce a genuinely new token, spacing value, or
   chart type that isn't in the references, add it there in the same
   change — the references are the source of truth precisely because
   they're kept current, not aspirational.
