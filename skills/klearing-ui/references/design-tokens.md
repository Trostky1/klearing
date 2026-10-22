# Design tokens — verbatim from ui.py

> **Updated by the 2026-07-26 redesign (Phases 1–3).** The surface ramp,
> type scale, spacing scale, font stacks and brand/action split all
> changed; see `docs/superpowers/specs/2026-07-26-visual-redesign-design.md`
> for why. This file mirrors the new values.

Source of truth: `ui.py`'s `STYLE` string, `:root` block. This file is a
mirror for quick reference; if it and `ui.py` ever disagree, `ui.py` wins
— update this file in the same change that changes a token, don't let it
drift again (see anti-patterns.md — this has already happened once).

## Surfaces

Four steps, widened from three so cards stop reading flat against the
page. Every card-like keeps a hairline top highlight so the stack has a
light source.

```
--bg                  #08080A
--surface             #121216
--raised              #1A1A20
--overlay             #202027   (command palette, dialogs — above everything)
--ink                 #F4F4F6
--muted               #A3A3AF
--faint               #8B8B98   (5.9:1 on --surface — AA with headroom)
--line                #232329
--line-strong         #3A3A44
```

## Brand vs action vs income

These used to be one value, so a primary button and an income figure
carried identical colour for different meanings. Green now means exactly
one thing — positive, income, healthy — plus brand identity. Primary
actions are ink-filled.

```
--brand               #4ADE80   (ACCENT_HEX — mark, active nav, focus ring)
--brand-strong        #86EFAC
--brand-soft/-line/-ink
--action              var(--ink)    (primary filled buttons)
--action-ink          var(--bg)
--income              #4ADE80
--expense             #F87171
--transfer            #2DD4BF
--warn                #FBBF24
--accent*             aliases of --brand*, retained for existing call sites
```

## Type

Three bundled OFL families (`frontend/fonts/`, served by route, no CDN):

```
--font-ui             "Instrument Sans"   — all interface text
--font-doc            "Instrument Serif"  — document register ONLY:
                                            statement titles, Home briefing,
                                            export cover. Never body or tables.
--font-mono           "Commit Mono"       — numeric columns, codes, timestamps
```

Nine steps spanning 11–56px, replacing six spanning 10–20px plus four
literal exceptions. The old scale was consistent but had no range, which
is why dense screens read as an undifferentiated grey wall.

```
--fs-2xs  0.6875rem  11px   micro labels, eyebrow
--fs-xs   0.75rem    12px   metadata, table headers
--fs-sm   0.8125rem  13px   secondary text
--fs-base 0.875rem   14px   body, table cells
--fs-md   1rem       16px   card titles
--fs-lg   1.25rem    20px   section headings
--fs-xl   1.75rem    28px   page titles, stat values
--fs-2xl  2.5rem     40px   statement titles (serif)
--fs-3xl  3.5rem     56px   briefing headline (serif)
```

One documented literal exception remains: `.health-grade .g` at 2.1rem,
constrained by its 88px circle and slated for rebuild.

Hero figures use `--font-ui` with `font-variant-numeric: tabular-nums
lining-nums` (set on `body`), not the mono stack — a 28px money figure in
a monospace reads as terminal output.

## Space

One 4px scale replaces ~20 ad-hoc values between 2 and 44px.

```
--sp-1 4    --sp-2 8    --sp-3 12   --sp-4 16
--sp-5 24   --sp-6 32   --sp-7 48   --sp-8 64
--pad-compact  var(--sp-3) var(--sp-4)
--pad          var(--sp-4) var(--sp-5)
--gap-section  var(--sp-5)   between sections
--gap-block    var(--sp-4)   within a section
--gap-tight    var(--sp-2)   label to value
```

Container padding is one of exactly two: `--pad-compact` or `--pad`.
**No hardcoded spacing or width outside the stylesheet** — this is now a
rule enforced the same way the colour rule is, and
`scripts/ui_consolidation_regression.py` fails if an inline `style=`
appears in `app.py`.

## Motion

```
--t-state    120ms   state changes
--t-overlay  200ms   overlays, dialogs, the bulk bar
--ease       cubic-bezier(0.32, 0.72, 0, 1)
```

## Radius

```
--radius     16px
--radius-sm  12px
```

## Component dimensions

```
--bottom-nav-h   calc(44px + 2 * var(--sp-2))   mobile bottom tab bar height —
                                                  44px touch target + sp-2 padding,
                                                  reused by main-inner's scroll
                                                  padding and the ai-fab/panel and
                                                  bulk-bar mobile offsets so all four
                                                  move together
```

---

## Spacing/sizing scale (values that repeat 2+ times in ui.py)

Not a strict 8px grid — this is the real, intentional scale, carried
over from the source design. When adding new spacing, match the nearest
value below rather than inventing one:

```
2px   4px   6px   7px   8px   9px   10px  11px  12px  13px
14px  16px  18px  20px  22px  24px  26px  32px  40px  44px
88px  190px 320px
62ch  60ch  66ch  (prose max-width)
100%  100vh (layout)
```

`10px` and `8px` are the two most common (30 and 18 occurrences
respectively) — default to one of those two when there's no strong
reason to pick something else.

## Border-radius values

```
var(--radius-sm)   = 12px   small cards, inputs, pills-that-aren't-fully-round
var(--radius)      = 16px   cards, sections, the health card
999px                       fully-round pills (buttons, tags, sidebar nav)
50%                         perfect circles (health grade badge)
rx = height / 2              chart bars/funnel/gauge — SEE chart-patterns.md,
                             this is computed per-chart, not a fixed value
```
