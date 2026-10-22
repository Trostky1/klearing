# Klearing visual redesign — design spec

Date: 2026-07-26
Branch: `feat/redesign-desk`
Status: implemented — all eight phases shipped 2026-07-26

## Why this pass exists

Klearing has real functionality across Review, Money, Books, and Insights,
and a design system that has held up well under incremental work: one token
block, a documented type scale, an anti-patterns register, genuine
accessibility care. What it does not have is a *product* shape.

The measurable problems, from a full survey of ~48 rendered views:

- **15 top-level nav items.** A file menu, not navigation. Below 640px it
  becomes 15 unlabelled hand-drawn glyphs with no grouping.
- **Three unrelated stat-tile systems** (`.dash-value`, `.totals .value`,
  `.planning-result .value`), **six idioms for "switch between sibling
  views"**, and **four registers of emptiness**.
- **26 tables, none sortable.** The app ships as a pywebview window with no
  browser chrome, so there is no Ctrl-F either. The Logs page already
  discovered this and hand-rolled a refresh link because F5 does not exist.
- **Pending — the core daily workflow — has no row cap and no bulk action.**
  Two weeks away produces 200 full-height cards and 200 native confirms.
- **Ten type sizes, nine between 10px and 18px.** One display size exists
  and belongs to one component on one screen. Dense screens read as an
  undifferentiated grey wall.
- **The body font is the system stack.** On Windows that resolves to Segoe
  UI. The single strongest "this is a template" signal in the product.
- **`--accent` and `--income` are the same value.** A green Approve button
  and a green income figure carry identical colour for different meanings.
- **The 641–1100px band is dead.** One breakpoint at 640px against content
  boxes at 900/1160px, so any window under ~1000px scrolls sideways — which
  is exactly where a resizable desktop window lives.
- **Tax is a different app sharing a stylesheet**: 437 lines of client JS,
  live filtering, bulk select, a real `<dialog>`. Nothing else works that
  way.

## Direction

**"The Desk"** — dark, dense, keyboard-first; a professional instrument —
with **an editorial briefing on Home** borrowed from the narrative direction.

References: Mercury's grouped sidebar and table density, Linear's keyboard
model, Clay and Midday for selection and bulk actions, Stripe's Today /
Your overview split for the briefing.

The governing idea: **Klearing is a tool you open every other day, forever,
to do a repetitive job.** Speed compounds; charm does not. Density is a
feature, but density without hierarchy is a wall — so the briefing and a
real display tier supply the hierarchy that density needs.

---

# 1. Foundations

## 1.1 Typography — the largest single change

Three roles, all bundled as subset woff2 under `frontend/fonts/` and served
by route, the same way `frontend/tax/*.js` already is. `build/klearing.spec`
gains one `datas` entry. No CDN, no build step, offline intact.

| Role | Family | Used for |
|---|---|---|
| UI | **Instrument Sans** (OFL) | All interface text. Slightly narrow set width, so dense tables fit more per line. Real tabular figures. |
| Document | **Instrument Serif** (OFL) | The Home briefing headline, financial statement titles, accountant-export cover. Nothing else. |
| Data | **Commit Mono** (OFL) | Numeric table columns, statement amounts, account codes, timestamps, log output. |

**The serif is the memorable decision and it is semantically motivated, not
decorative.** Things you would hand to an accountant — Profit & Loss,
Balance sheet, Trial balance, the export package — are set in a document
register. Things you work in are set in a tool register. A reader can tell
which is which before reading a word. Used at display sizes in roughly six
places; never in body copy, never in tables.

**Display numbers move off mono.** Today `.dash-value` renders at 1.5rem in
Consolas, which is one of the weakest details in the product. Hero figures
become Instrument Sans with `font-variant-numeric: tabular-nums lining` —
alignment preserved, typewriter texture gone. Mono keeps the job it is
actually good at: dense columns of figures.

**Type scale** gains a real display tier and keeps the healthy small end:

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

Nine steps replacing ten sizes, but now spanning 11–56px instead of
10–34px, with the top three actually load-bearing. The four documented
literal exceptions are retired and folded in.

Licence files for all three families ship in `frontend/fonts/` — required
for commercial distribution.

## 1.2 Colour

The dark palette is working and mostly survives. Two changes.

**Split action from income.** `--accent` and `--income` are currently the
same `#4ADE80`. Primary buttons become **ink-filled** (near-white on dark),
the pattern Vercel and Linear both use, and green retires from actions
entirely. Green then means exactly one thing everywhere: positive, income,
healthy. This removes a real semantic collision and reads more premium.

- `--brand` — the green. Logo, active nav indicator, focus ring.
- `--action` — `--ink`. Primary filled buttons.
- `--income` — the green. Money in, positive deltas, all-clear states.
- `--expense`, `--warn`, `--transfer` — unchanged.

**Deepen elevation.** Three surfaces today (`--bg`, `--surface`, `--raised`)
are close enough to read flat. Widen the steps and keep the existing hairline
top highlight so cards have a light source. Add `--overlay` for dialogs and
the command palette, which need to sit clearly above everything.

A very low-opacity grain overlay on `--bg` is proposed as an optional
refinement — inline SVG data-URI, no asset, no build step. Flagged for
review at implementation; it is cheap to try and cheap to drop, and it is
the difference between "dark grey" and "a considered surface."

## 1.3 Space

The current scale is 20 ad-hoc values between 2 and 44px with no
relationships. Replace with an 8-step 4px scale:

```
--sp-1 4   --sp-2 8   --sp-3 12  --sp-4 16
--sp-5 24  --sp-6 32  --sp-7 48  --sp-8 64
```

Every existing value maps to the nearest step. Container padding collapses
to two options: **compact** (`--sp-3` / `--sp-4`) and **standard**
(`--sp-4` / `--sp-5`). The nine different card paddings become two.

Vertical rhythm gets three named gaps: between sections (`--sp-5`), within a
section (`--sp-4`), tight (`--sp-2`). Stacking a `.sub`, a filter form and
ten sections currently produces gaps of 24, 24, 16, 16 with no relationship;
after this it produces one rhythm.

**All 91 inline `style=` attributes are removed**, replaced by real classes.
The rule "no hardcoded colour outside `:root`" gains a sibling: no hardcoded
spacing or width outside the stylesheet.

## 1.4 Motion

Restrained and mostly absent. One orchestrated moment — the Home briefing
reveals on load with a short staggered fade, nothing else on the page moves.
Transitions standardise at 120ms (state) and 200ms (overlay). The existing
`prefers-reduced-motion` block covers all of it.

---

# 2. Information architecture

Fifteen flat tabs become **13 grouped items plus a command palette**, and
three review queues merge into one job.

```
Home
Review          ← badge: total items waiting
——— MONEY ———
Summary
Vendors
Reconcile
Shopify
——— BOOKS ———
Ledger
Tax
——— UNDERSTAND ———
Insights
Growth
——— SYSTEM ———
Connections
Settings
```

- **Review** absorbs Pending, Follow-ups and Invoices as sub-tabs. These are
  three queues serving one daily job; splitting them across the sidebar
  hides how much is actually waiting. The badge is the total.
- **Tools, Logs, Automation, Audit log, Uncategorized** leave the sidebar.
  Tools sub-pages and Logs and Automation become Settings sub-tabs;
  Uncategorized stays reachable from Home. The Tools hub — six static cards
  with no live data — is deleted rather than restyled.
- **Group labels are not collapsible.** Collapsing hides things; the point
  is scannability, not compression.

**Command palette (⌘K / Ctrl+K)** carries everything else: all ~48 views,
plus verbs — "sync ledger", "export accountant package", "test connections",
"run daily check". Grouped results (Actions / Recent / Pages), fuzzy match,
full keyboard control. This is the signature interaction of the direction
and the thing that makes 48 views navigable without 48 nav items.

---

# 3. Component consolidation

Direction-independent hygiene, done as part of this pass.

| Today | After |
|---|---|
| 3 stat-tile systems | **1 `Stat`** — label, value, optional delta, optional sparkline. Replaces `.dash-value`, `.totals .value`, `.planning-result .value`. |
| 6 sub-nav idioms | **1 `SubNav`** — used by Review, Ledger, Tax, Insights, Settings, Logs. Retires `.log-tabs`, `_tools_sibling_nav`'s prose sentence, and `.range-toggle`'s separate pill row. |
| 4 empty registers | **2** — `.empty` (page-level, designed, with an action) and `.none-note` (inline, calm). `_ledger_empty_state` and chart `NO_DATA` fold into these. |
| 26 ad-hoc tables | **1 `DataTable`** — see §4.2. |
| 22 `window.confirm()` | **1 `Dialog`** — see §4.4. |
| 9 card paddings | **2** (compact / standard). |

## 3.1 New and rebuilt components

**`CommandPalette`** — overlay, grouped and keyboard-driven. ~180 lines of
vanilla JS, no library.

**`DataTable`** — the workhorse. Sortable headers (click to sort, arrow
indicator, three-state), optional selection column, sticky header on scroll,
right-aligned tabular numerics, row hover, and a consistent empty state.
Sorting and selection are client-side over already-rendered rows; the server
still renders the HTML. Roughly 120 lines of JS shared by all 26 tables.

**`BulkBar`** — floating action bar that appears on selection, following
Midday's pattern: count, deselect, primary actions. Used by Review
(approve/skip), Tax (already has one, migrates to the shared component), and
anywhere else selection lands.

**`Dialog`** — native `<dialog>`, replacing every `window.confirm()` for
destructive and bulk actions. Specific copy, not "Are you sure?". The Tax
bulk dialog is the existing prototype.

**`Briefing`** — Home's editorial header. Two or three sentences with the
numbers set inline, in Instrument Serif at `--fs-3xl`, followed by the
evidence. Sourced from data that already exists: `compute_insight_flags`,
the health score, the daily insight, reconciliation state, runway.

**`Stat`** — one tile to rule the three.

---

# 4. Interaction model

The Tax workspace's model becomes the app-wide standard rather than the
outlier.

- **Sorting** — every table with more than ~10 rows. Click a header, sort
  ascending / descending / off. Numeric, date and text comparators.
  Client-side, no reload.
- **Selection and bulk actions** — Review first (the core workflow), then
  anywhere a list supports a repeated verb. Shift-click ranges,
  select-all-visible.
- **Dialogs** — replace all destructive confirms.
- **Filtering** — stays server-side via GET where it changes the query
  (dates, periods); becomes client-side only where it filters already-loaded
  rows (status pills), matching what Tax does today.
- **Row caps** — Pending gains one, with an honest overflow note, matching
  the pattern already applied to Audit and Vendor detail.

Keyboard: `⌘K` palette, `/` focus search, `j`/`k` row movement in Review,
`a` approve, `s` skip, `?` shortcut sheet. Every shortcut is discoverable
from the palette; none is required.

---

# 5. Screen treatment

Not every screen changes equally. Three tiers.

**Rebuilt** — Home (briefing + Stat grid), Review (three queues into one
table-driven workspace with selection), Connections (13 cards, 30 forms,
2500px of wall → grouped, filterable, collapsed-by-default for
unconfigured services), Settings (one monolithic form → sub-tabs).

**Restructured** — Reconcile (11 always-on sections → a verdict plus
progressive disclosure), Growth and Marketing Insights (10 sections each →
merged where they overlap, chart/table duplication resolved), Shopify (the
order detail page currently prints raw JSON in a `<pre>`; it gets a real
layout or it gets deleted).

**Retokenised only** — Ledger's five tabs, Tax's four, Summary, Vendors,
Audit, Nexus, Accountant export, Unit economics, Logs, the wizard. These
inherit the new type, colour, spacing and DataTable without structural
change. The Ledger `.stmt-*` grammar survives as-is; it gains the serif
titles.

---

# 6. Responsive

Two breakpoints instead of one.

- **≥1100px** — full layout, sidebar with labels.
- **640–1100px** — the currently dead band. Content max-widths become fluid
  (`min(1160px, 100% - 2rem)`), the Stat grid drops a column, sidebar keeps
  labels. This is where a resizable desktop window actually lives.
- **<640px** — existing behaviour, improved: the icon rail gains grouping
  separators, sub-navs scroll rather than overflow, tables keep horizontal
  scroll but gain a sticky first column.

---

# 7. Constraints

**Relaxed, approved:**
1. Client-side sorting and selection — vanilla JS, no library.
2. A second breakpoint at ~1024px.
3. Native `<dialog>` replacing `window.confirm()` for destructive and bulk
   actions.

**Defended, upheld:**
1. **No chart library.** `charts.py`'s five SVG types stay. A library means a
   build step, which forfeits the single-unsigned-binary property. The real
   chart problem is duplication — Shopify draws the same ten rows as a bar
   chart and a table immediately below it — which is an editing problem.
2. **Server-rendered HTML.** Client-side *state* (sort, selection, filter,
   palette) yes. Client-side *routing* no. Every mutation stays
   POST → 303 → banner.

**New constraint introduced:** no hardcoded spacing or width outside the
stylesheet, enforced the same way the colour rule is.

---

# 8. Accessibility

The existing care is preserved and extended: skip-nav, `aria-current`, 44px
coarse-pointer targets, `prefers-reduced-motion`, per-mark chart tooltips.
Additions required by the new components:

- Palette: `role="dialog"`, focus trap, `aria-activedescendant`, Escape to
  close, focus returned on close.
- Sortable headers: `<button>` inside `<th>`, `aria-sort`.
- Selection: real checkboxes, `aria-label` per row, live region announcing
  the selected count.
- Dialogs: native `<dialog>` gives focus trapping and Escape for free.
- Contrast: every new pairing verified ≥4.5:1 for body, ≥3:1 for large.

---

# 9. Out of scope

- **Correction, 2026-07-26:** the two ledger backend defects this section
  originally deferred (revenue miscoding, Inventory drifting negative)
  were fixed by a separate backend pass on this branch before this section
  was written — `ledger_sync.py` now routes Plaid deposits correctly and
  posts transfers via a Transfer Clearing account, and no longer credits
  Inventory without a debit to match. Confirmed against
  `ledger_sync.py` and `scripts/ledger_regression.py`, and the Ledger UI's
  own caveats were corrected in this same redesign pass to stop describing
  a fixed bug — see
  `docs/superpowers/specs/2026-07-25-ledger-ui-design.md`'s known-issues
  section for the full history and the caveat replacing them. What
  remains genuinely out of scope for this redesign: the new gap that
  fix traded in — Cost of goods sold does not post at all for Shopify
  sales until inventory-acquisition tracking exists — which is a backend
  feature, not a visual one.
- A light theme. The direction is dark; a light mode is a separate project.
- Any new feature. This pass moves nothing that is not already on screen,
  except deleting the Tools hub.
- Mobile as a first-class target. The app is a desktop binary; the
  responsive work targets the resizable window, not phones.

---

# 10. Success criteria

1. Sidebar is 13 grouped items; every remaining view is reachable in two
   keystrokes via the palette.
2. Every table over ten rows sorts. No user is left with Ctrl-F in a window
   that has no Ctrl-F.
3. Review handles a 200-item backlog in one selection and one dialog.
4. One stat component, one sub-nav, two empty registers, zero inline
   `style=` attributes.
5. Nothing scrolls sideways between 640 and 1100px.
6. `scripts/app_render_regression.py` passes throughout; no screen loses
   information it has today.
7. `section-inventory.md` regenerated and accurate.

---

# 11. Documentation debt cleared as part of this work

`skills/klearing-section/references/section-inventory.md` documents 21
render functions and a 12-item sidebar. Shopify, Ledger and Tax all landed
afterwards; roughly a third of the surface is missing. It is regenerated
against the rebuilt app at the end of the pass, not the start.

`skills/klearing-ui/references/design-tokens.md` and `anti-patterns.md` are
updated in step with each phase — tokens when they change, anti-patterns
when a phase retires one.
