# Anti-patterns — real findings from this codebase, not hypotheticals

Every entry below actually shipped in Klearing at some point and was
caught by a design/code audit, then fixed. Each one is a category of
mistake, not a one-off — if you're writing new UI code, check the "watch
for" line before you ship, because these are exactly the kind of thing
that's easy to reintroduce somewhere else in the app.

## 1. Chart text becomes illegible on narrow viewports

**What happened:** every chart's SVG uses a fixed 640-unit `viewBox` that
scales responsively to its container (`width: 100%`) — correct for the
shapes, but CSS `font-size` scales down with it too, uncorrected. Measured
directly: a chart value declared at 12.8px rendered at an **effective
6.7px** on a 375px-wide window (0.38× scale), while the exact same chart
at desktop width rendered at the full 12.8px.

**Why it's easy to miss:** it looks completely fine at desktop width,
which is what gets eyeballed by default. It only shows up when someone
actually measures rendered text height at a narrow viewport — a
screenshot alone doesn't catch it either, since 6.7px text still
*renders*, it's just too small to read.

**The fix that shipped:** at the `≤640px` breakpoint, `.chart-svg` holds
its native 640px width (`max-width: none`) instead of shrinking, and the
wrapping `figure.chart` gets `overflow-x: auto` so the chart scrolls
horizontally instead of compressing. Same pattern the app already used
for wide tables on the same breakpoint (`.section table { overflow-x:
auto }`) — reused, not invented.

**Watch for:** any new chart type, or any existing chart moved into a
narrower container (e.g. a chart added to a `.dash-card`, which is
narrower than a `.section`). Verify rendered text height at a 375px
viewport before shipping, not just at desktop width.

## 2. A chart fill's fallback color doesn't match its own gradient

**What happened:** `.chart-bar.neutral { fill: url(#kl-bar-h-neutral)
#A1A1AA; }` — every other chart fill's solid CSS fallback exactly equals
its own gradient's light/end stop (e.g. `.chart-bar { fill:
url(#kl-bar-h) var(--accent) }`, and `--accent` IS `kl-bar-h`'s end
stop). The neutral variant alone used a hand-picked midtone instead of
its gradient's actual end stop (`#D4D4D8`).

**Why it's easy to miss:** invisible under normal rendering — the
gradient always wins over the fallback when the SVG defs load correctly.
It only shows if the defs ever fail to render, which is precisely the
scenario the fallback exists to cover, so a wrong fallback fails exactly
when it's needed.

**The fix that shipped:** changed to `#D4D4D8`, matching
`kl-bar-h-neutral`'s actual end stop.

**Watch for:** any new chart fill variant — the solid fallback must equal
that variant's gradient's own end stop, verified by reading the gradient
definition in `CHART_DEFS`, not guessed.

## 3. The same "eyebrow label" CSS declared 8 times, already drifted in 3

**What happened:** the small-uppercase-caption treatment (faint color,
600 weight, uppercase, 0.04em tracking) was independently declared in
`.totals .label`, `th`, `.fu-field label`, `.dash-kicker`, `.chart-title`,
`h3`, `.dash-mini-label`, and `.dev-tools .dev-label` — 8 separate
copy-pasted rules, no shared class. Three had already silently drifted:
`h3` was 0.75rem instead of 0.72rem, `.dev-label`'s tracking was 0.05em
instead of 0.04em.

**Why it's easy to miss:** each individual drift is imperceptible alone
(0.03rem ≈ 0.5px). It only becomes visible when two instances render
side-by-side and you diff their computed styles.

**The fix that shipped:** consolidated into one shared rule (`.totals
.label, .dash-kicker, .chart-title, .fu-field label, h3, th,
.dash-mini-label, .dev-tools .dev-label { ... }`), with each site's
individual rule trimmed down to only its layout-specific extras
(margins, display). `.dash-mini-label` keeps a smaller override
font-size as a documented, deliberate exception (denser 3-up context),
not silent drift.

**Watch for:** copy-pasting a typography treatment to a new selector
instead of adding that selector to the existing shared rule. If two
things are supposed to look identical, they should share one CSS rule,
not two rules with identical-looking values.

## 4. A card-like surface missing the shared "card" treatment

**What happened:** `.accounts li` (used on 3 screens — Settings'
account picker, Connections' business list, Automation's job list) had
`background`, `border`, and `border-radius` like every other card
(`.card`, `.section`, `.fu-card`, `a.dash-card`, `.health-card`), but was
missing the `box-shadow: inset 0 1px 0 rgba(255,255,255,.04)` top
highlight all of those carry — so it sat visibly flatter next to the
`.section` that usually wraps it.

**The fix that shipped:** added the same inset box-shadow.

**Watch for:** any new "card"-shaped element (background + border +
radius) — it should get the same inset highlight, or there should be a
deliberate reason it doesn't (e.g. `.log-box` is intentionally recessed,
not raised, so it correctly has no highlight).

## 5. Repeated literal hex instead of a token

**What happened:** `#55555F` (hover border) appeared 3 times, `#FCA5A5`
appeared twice as two different components' text color, and the inline
brand-mark SVG hardcoded 4 hex values that exactly duplicated existing
`--raised`/`--line-strong`/`--ink`/`--accent` tokens — even though it's
inline HTML where `fill="var(--accent)"` works fine.

**The fix that shipped:** added `--expense-ink` and
`--hover-border-strong` tokens, replaced all usages; switched the brand
mark to `var()` references.

**Watch for:** the same literal color value appearing twice. Two is
already the signal — promote to a token on the second use, don't wait
for a third.

## 6. Monospace applied to a full phrase, not just the number

**What happened:** `.dash-value` is monospace everywhere — correct for
`+$8,309.01`, wrong for `"3 waiting"` and `"1,240 sessions"`, where the
whole phrase (including the words) rendered in a fixed-width font meant
for tabular digit alignment.

**The fix that shipped:** added `.dash-value.mixed { font-family:
inherit; letter-spacing: normal; }`, applied only at the 2-3 call sites
that combine a number with words. Pure numeric/money `.dash-value`
instances were left untouched — monospace is still correct there.

**Watch for:** any new `.dash-value`, `.amount`, or `.totals .value`
content that isn't a bare number/money/percent figure. If it has words
mixed in, it needs `.mixed`.

## 7. A label-truncation character limit with no relationship to its actual pixel budget

**What happened:** `_truncate(text, limit=26)` in `charts.py` was a
character count with no connection to `horizontal_bar_chart`'s 168px
label column. Measured against the real font stack: a realistic 25-char
all-caps bank-transaction descriptor (`"AMAZON MKTPLACE PMTS AMZN"` —
not an edge case, a completely normal Plaid merchant name) rendered at
196px, 20px into the bar track.

**Why it's easy to miss:** Python can't measure real glyph widths, so
this kind of bug is invisible from the code alone — it only shows up
when you actually render the string in a browser and measure it.

**The fix that shipped:** widened the label column (168px → 190px) and
tightened the default limit (26 → 22 chars), both calibrated against the
actual failing case, verified in-browser afterward. See
`chart-patterns.md` — these two numbers are paired and must be changed
together.

**Watch for:** any new fixed-character-count truncation paired with a
fixed-pixel column. If you change one, re-verify the other against
worst-case realistic content (all-caps, space-heavy, the kind of text a
bank or CRM actually produces) — don't just eyeball typical-length
sample data.

## 8. A card vanishing instead of showing a calm "all clear" state

**What happened:** `dash_vendor_flag()` returned `""` (no card at all)
whenever nothing was flagged — contrast with Reconcile, which shows a
positive "in sync" message when everything's fine. An absent card and a
"checked, nothing wrong" card look identical to a user glancing at the
dashboard, but only one of them actually communicates that the check
ran.

**The fix that shipped:** added two calm states — "not enough expense
history yet" (no data to check) and "no vendor spend jumps this month"
(checked, clean) — matching Reconcile's existing pattern instead of
introducing a third, inconsistent one.

**Watch for:** any conditionally-rendered dash-card that returns `""`
on the "nothing wrong" path. Ask whether that's truly "nothing to
compute yet" (fine to omit) or "checked and it's fine" (should say so).

## 9. A card promising an affordance it doesn't have

**What happened:** Home's sub-heading says "Every card opens its full
tab" — true for all 7 `<a class="dash-card">` tiles, false for the
health card, which had zero links anywhere, not even on its own
"Weights adjustable in Settings" text.

**The fix that shipped:** made "Settings" an actual `<a href="/settings">`
inside the health-note text.

**Watch for:** any page-level copy that makes a blanket promise ("every
X does Y") — grep for the actual exceptions before shipping that
sentence, not after.

## 10. The same sentence duplicated verbatim within one component

**What happened:** the health card's "Biggest risk right now" summary
restated the exact same detail sentence (e.g. "One vendor is over your
spend-jump threshold this month.") that also appeared, word for word, in
that factor's own row directly below it.

**The fix that shipped:** the summary now just names the factor
("Biggest risk right now: vendor stability.") and lets the factor row
below carry the actual detail sentence — a real summary-then-detail
pattern instead of copy-pasted duplication.

**Watch for:** reusing the exact same pre-built string in two places in
the same render function. If a "headline" and a "detail" both need the
same underlying fact, paraphrase the headline shorter — don't call
`str(x)` twice on the same variable in two different spots.

## 11. A responsive grid with no mobile-specific column floor

**What happened:** `.dash-grid`'s `grid-template-columns: repeat(auto-fit,
minmax(300px, 1fr))` never relaxed at the `≤640px` breakpoint. On a true
small-phone viewport (~320-350px), the 300px card floor exceeded the
available container width, and since nothing caught the overflow, it
propagated all the way to `<html>` — the **entire page** scrolled
sideways, not just the grid.

**The fix that shipped:** `.dash-grid { grid-template-columns: 1fr; }`
inside the existing `≤640px` media query — forces a single column, which
always fits regardless of exact container width.

**Watch for:** any `grid-template-columns: repeat(auto-fit, minmax(Npx,
1fr))` pattern — verify it at a true small-phone width (≤360px), not
just the 375px preset, since `minmax`'s floor can exceed the container
before the grid's own overflow becomes obvious at a slightly wider test
width.

## 12. Documentation that quietly drifted from the code it describes

**What happened:** `SECTION_PLAYBOOK.md`'s own "Design System Reference"
table listed `#0d0d0d`/`#00ff88`/`#666666` as the real tokens — none of
which matched `ui.py`'s actual values (`#0A0A0C`/`#4ADE80`/`#A3A3AF`).
It also claimed spacing followed "a consistent 8px grid," which doesn't
match the real, intentional 14/16/18/20/22px scale.

**The fix that shipped:** corrected the playbook's token table and
spacing claim to match `ui.py` exactly, with an explicit note that
`ui.py` wins if they ever disagree again.

**Watch for:** this is the meta-lesson — any doc (a playbook, a skill
reference, a comment) that states a concrete value (a hex code, a
spacing number) needs to be checked against the actual source
(`ui.py`'s `:root` block) periodically, not trusted as permanently
accurate just because it was correct once. This very file is exactly
that kind of doc — if `ui.py` changes, update `design-tokens.md` in the
same change.

## 13. A "shared" class that only works inside one host container

**What happened:** `.hf-detail` looked like a reusable detail-text
class, but its only rule was scoped `.health-factor .hf-detail`. When
Growth's revenue rows reused the class inside `.growth-row-head`, it
silently matched nothing — the detail line rendered as 15px full-ink
body text competing with the row's title, instead of the intended
faint 0.82rem (measured live: `rgb(244,244,246)`/15px vs the health
card's correct rendering).

**Why it's easy to miss:** the class name reads generic, autocomplete
offers it, and the page still renders — just wrong. Nothing errors;
only a computed-style comparison against the other usage site shows it.

**The fix that shipped:** descoped the rule to plain `.hf-detail`; the
original host renders pixel-identically.

**Watch for:** reusing any class whose defining rule you haven't read.
If the rule is parent-scoped, either descope it (when the treatment is
genuinely one thing) or don't reuse the name. Related to #3, but the
failure is a scoping miss on one rule, not drift between duplicates.

**Found again, much bigger (2026-08-01).** `.hint` had exactly the same
shape: declared only as `.field .hint` and `.cred-field .hint`, reused
as a bare class 73 times across `app.py`, `setup_wizard.py` and `ui.py`.
An ancestry audit over rendered pages — walking each element's real
parent chain, not source proximity, since a hint's enclosing markup
spans several f-strings — found **24 of them outside any form field**,
rendering as full-size body ink: every hint on Contacts, Inventory,
Recurring invoices and Tools. The two originals were identical apart
from `62ch` vs `60ch`, which carried no comment marking it deliberate
and so was drift (cf. #22) rather than an exception to preserve.

Fixed by descoping to one `.hint` rule beside `.sub`, and now guarded by
`check_hint_is_not_parent_scoped()` in
`scripts/ui_consolidation_regression.py`, which fails if a parent-scoped
copy reappears.

The lesson is the audit method as much as the fix: a class used across
dozens of call sites cannot be checked by reading the CSS: only rendering
the pages and resolving real ancestry tells you which usages the rule
actually reaches.

## 14. A click that silently does nothing

**What happened:** twice in one sweep. Unit economics: submitting
Calculate with exactly one of the two fields filled re-rendered the
page with no result and no explanation. Pending: the header Lock
button rendered even with no app password configured, where clicking
it bounced /lock → /unlock → straight back to the dashboard — a
complete no-op.

**Why it's easy to miss:** both paths are "working as coded" — no
error, no traceback. The user's click just evaporates, which reads as
"the app is broken" or trains them that the control is decorative.

**The fix that shipped:** Unit economics now names the missing field
("Enter a target retail price as well to see a margin."); the Lock
button only renders when `app_lock.is_configured()`.

**Watch for:** the interactive cousin of #8 (which covers vanishing
cards). Every button/submit needs a visible consequence on every
reachable state — or to not be offered in states where it can't do
anything. Test the half-filled and unconfigured paths, not just the
happy one.

## 15. An unbounded query feeding an ever-growing table

**What happened:** the Audit log rendered `db.list_items()` in full —
every transaction Klearing had ever seen, forever. Fine in month one;
a years-old ledger would render thousands of rows on every visit.

**The fix that shipped:** `AUDIT_LIMIT = 200` newest, with a
"Showing the most recent 200 of N decisions." note when truncated —
the same cap-plus-honest-note shape as `VENDOR_DETAIL_LIMIT` on the
vendor detail page. Two instances make it the house pattern.

**Watch for:** any render loop over a query with no LIMIT. Time-based
data always grows; cap it at render time and say so honestly (silent
truncation is its own bug — see chart `max_bars`, which also caps but
is documented). If the user needs the tail, that's a filter/search
feature, not a reason to render everything.

## 16. One word carrying two meanings on the same surface

**What happened:** twice. Insights: the funnel's "% of prior" meant
*previous stage* while three table headers' "Prior" meant *previous
period* — same word, two comparison axes, one page. Settings: a hint
said "the composite score on the Dashboard" when the tab is named
"Home" everywhere else in the app.

**The fix that shipped:** the funnel now says "% of prior stage"; the
Settings hint says "on the Home tab" (linked).

**Watch for:** comparison words ("prior", "last", "previous") reused
across different axes on one page, and any copy that names a screen —
grep the sidebar's link list for the canonical name before writing it.

## 17. AI assistant boundaries (rules, not a retrospective)

Unlike the entries above, this one was written WITH the feature, as
its standing constraints:

- AI assistant context must never contain raw transaction data —
  aggregates only. `ollama_client.build_context` builds sums, counts,
  and top-N rollups; if a change would put an individual transaction
  row (vendor + date + amount as a record) into the prompt, that's
  the line being crossed.
- If Ollama is offline the app must work identically — never a hard
  dependency. `is_available()` never raises, the button degrades to
  "AI offline", and no page render blocks on the model.
- Ollama is not in system PATH — all calls go to
  http://localhost:11434 directly. Never shell out to ollama.exe.
- AMENDED (feat/ai-providers): "never send this data to any
  non-localhost address" is no longer absolute. OpenAI and Anthropic
  are now selectable providers (ai_providers.py) — but ONLY through
  the full consent path: an explicit confirmation dialog at selection
  time naming the provider and stating that transaction summaries and
  balances leave the machine, plus a persistent "Cloud AI active"
  warning pill in the chat header the entire time a cloud provider is
  active. Ollama remains the default, never warns, and the
  aggregates-only rule above applies unchanged to every provider —
  cloud or local, the context never contains raw transaction rows.
  A silent cloud fallback when Ollama is down is still forbidden;
  the provider only changes when the user changes it in Settings.

**Watch for:** any "improvement" that enriches the context with
transaction lists ("just include the last 20 transactions"), makes a
page wait on the model, adds an UNWARNED remote fallback when the
local model is down, or routes to a cloud provider without the
consent dialog + persistent pill. All of these read as features and
are regressions.

## 18. A capability flag that doesn't mean the model actually does it

**What happened:** Ollama's `/api/show` reports `deepseek-r1:8b` with
`"tools"` in its capability list — but live-probed against
`/api/chat` with the real `TOOLS_SPEC`, it never once emits a
`tool_calls` entry, with or without `think: false`. Trusting the
capability flag would have silently downgraded every deepseek-r1
answer to guesses instead of real Klearing numbers, with no error
anywhere to notice it by.

**The fix that shipped:** `ollama_client._supports_tools()` runs one
cheap live probe per model (a direct "call this tool now" instruction)
and caches the verdict for the process's lifetime. Only a model that
actually returns a tool call gets the native tool-calling path;
everything else — deepseek-r1 included — gets `build_full_context()`,
which dumps every tool's data into the system prompt up front so a
tool-less model still answers from real numbers instead of inventing
them.

**Watch for:** any future default-model change (or a new model added
to `PREFERRED_MODELS`) — re-run the same probe against it before
assuming the capability flag is honest. Never route a model to the
tool-calling path on the strength of `/api/show` alone.

## 19. A dismissed banner that dismisses itself forever

**What happened, avoided proactively:** the daily-insight banner
writes new content to `agent_memory["daily_insight"]` every day. A
plain boolean "dismissed" flag would suppress not just today's
insight but every future one too, since nothing would ever flip it
back.

**The fix that shipped:** the dismiss setting stores the dismissed
insight's own `updated_at` stamp, not a bool. The banner compares the
CURRENT insight's stamp against the stored one — a newer insight
(different stamp) always reappears, even though a strictly-older one
stays hidden.

**Watch for:** any other "one-time" dismiss button backed by
regenerating content (a new daily/weekly value, a refreshed flag). If
the underlying value can change, the dismiss key must identify WHICH
value was dismissed, not just whether the dismiss button was ever
clicked.

## 20. A consistent recipe still copy-pasted instead of shared

**What happened:** `.card`, `.section`, `.fu-card`, `.accounts li`,
`a.dash-card`, and `.health-card` all correctly carried the same
"panel" look (`background: var(--surface); border: 1px solid
var(--line); box-shadow: inset 0 1px 0 rgba(255,255,255,.04);`) — no
selector was missing it (contrast with #4 above, where one selector
genuinely lacked the highlight). The problem wasn't drift yet; it was
six independent copies of the same three declarations, which is
exactly the setup that produces drift the next time only one of the
six gets edited.

**The fix that shipped:** grouped all six selectors into one shared
rule for the three common properties (`.card, .section, .fu-card,
.accounts li, a.dash-card, .health-card { background: ...; border:
...; box-shadow: ...; }`), leaving each selector's own rule with only
its genuinely-different properties (radius, padding, layout). Same
technique already used for the eyebrow label treatment (#3) and now
applied to buttons (see #21) — this is the house pattern for "N
selectors, same recipe."

**Watch for:** a new card-shaped element that copies an existing
selector's three-property recipe instead of joining the shared `.card,
.section, .fu-card, .accounts li, a.dash-card, .health-card` rule. If
you add a 7th panel-shaped selector, add it to that group rather than
retyping the three properties.

## 21. Two parallel "looks like a button" systems drifting independently

**What happened:** a real `<button>` and `.btn-link` (an `<a>` styled
to look like one, used where the control needs to navigate rather than
submit) each independently declared the same pill geometry (`font-size:
0.875rem; font-weight: 600; border-radius: 999px; padding: 8px 18px;`)
in two separate rules. Both currently agreed, but nothing enforced
that — a future edit to one could silently produce a `<button>` and a
`.btn-link` that no longer match, which the product register's
component-consistency rule (same button shape everywhere) explicitly
forbids.

**The fix that shipped:** the shared geometry moved to one `button,
.btn-link { font-size: ...; border-radius: ...; padding: ...; }` rule.
`button`'s own rule (which still needs `font: inherit` to reset
inherited font properties before the shared rule re-sets size/weight)
had to stay ABOVE the shared rule in source order — `font: inherit` is
a shorthand that resets font-size/weight too, so the shared rule's
same-specificity declarations must come later to win the cascade. If
you ever reorder these two rules, re-verify button text doesn't
silently revert to the inherited body size.

**Watch for:** any new element meant to "look like a button" (a link,
a label, a summary/details toggle) that restates pill geometry instead
of joining the `button, .btn-link` group.

## 22. Consolidating into a shared scale can silently erase a documented exception

**What happened, caught before shipping:** collapsing 21 near-duplicate
font-size literals into 6 named `--fs-*` steps, `.dash-mini-label`'s
0.68rem and the shared eyebrow rule's 0.72rem both rounded to the same
nearest step (`--fs-xs`, 0.7rem) by simple nearest-neighbor distance.
But `.dash-mini-label`'s smaller size was never drift — it's a
documented, deliberate exception (see #3: "`.dash-mini-label` keeps a
smaller override font-size as a documented, deliberate exception... not
silent drift"). A pure nearest-token mapping would have quietly merged
two values that were *intentionally* different, undoing that
documentation instead of respecting it.

**The fix that shipped:** `.dash-mini-label` was left as its own
literal (0.68rem, not tokenized), with a comment explaining why it's
deliberately below `--fs-xs`, rather than being folded into the scale
like the other 20 values.

**Watch for:** any future consolidation pass (colors, spacing, radii —
not just type) that maps "close" values onto a shared token by distance
alone. Before merging two values, check whether either one has a
comment marking it as an intentional exception rather than drift — if
so, it stays a literal, even if it would technically fit the nearest
step.

## Intentional export and asset color literals

`accountant_export.py` and `assets/klearing.svg` contain intentional color
literals. These are export/asset contexts that cannot reference CSS
variables. Do not tokenize them and do not flag them as UI color drift.

---

## Redesign additions (2026-07-26)

Patterns found and fixed during the Desk redesign. Each is now guarded by
`scripts/ui_consolidation_regression.py`.

### 23. A second implementation of a shared component

Three stat systems, six sub-nav idioms, four emptiness registers, nine
card paddings. None arrived deliberately; each was one screen solving a
solved problem locally. Before writing a container, a tile, a tab bar or
an empty state, check `section-inventory.md`'s component table.

**Exception worth naming:** sub-navigation collapsed to *two*, not one.
Switching between sibling views (`.subnav`, underline tabs) and filtering
within one view (`.segmented`, pills) mean different things, and making
them identical would teach the wrong model. Consolidation is about
removing accidents, not flattening real distinctions.

### 24. Spacing decided outside the stylesheet

91 inline `style=` attributes in `app.py` — hardcoded `width:280px`,
`margin-top:32px`, a seven-property flex recipe pasted seven times. The
colour rule had been enforced for a year; the spacing equivalent had
never been written down. It is now: **no hardcoded spacing or width
outside `ui.py`**, and the regression suite fails on any inline `style=`.

### 25. A table with no way to order it

26 tables, zero sortable, in a pywebview window with no browser chrome
and therefore no Ctrl-F. Past about twenty rows the user had no tool at
all. Any table that can exceed ~10 rows needs `data-sort` on the columns
that carry magnitude.

Money must be *parsed*, not string-sorted, or `$9` lands after `$100`.
Nulls sort last in both directions — an empty cell is not smaller, it is
absent.

### 26. A queue with no bulk action and no cap

Pending rendered every item as a full-height card, unbounded. Fine for
the five a daily user sees; two hundred scrolls and two hundred
confirmations after a fortnight away. A review surface needs selection,
one bulk action, and a row cap with an honest overflow note.

Bulk must not weaken a per-item guard: the duplicate-post check still
refuses in bulk and names what it refused. **A bulk action that reports
success for work it did not do is worse than one that does nothing.**

### 27. Explanation that is always on

Reconcile carried eleven always-rendered sections — a verdict followed by
several hundred words of prose and four tables, on screen whether or not
anything was wrong. Answer first, evidence behind `ui.disclosure`.

Same shape on Connections: thirteen cards and thirty forms meant the two
services you had configured were buried under eleven you had not.
Collapsing the unconfigured ones cut the page 66%.

### 28. A chart that plots the table beneath it

Shopify drew the top ten products as a bar chart and then listed the same
ten rows immediately below. Once tables sort, a ranked bar chart of
identical data is strictly less informative than the table. Keep charts
that show what a table cannot — time series, comparisons, funnels.

### 29. Showing the user the shape of the database

`/shopify/order/{id}` printed `line_items_json` raw inside a `<pre>`. It
is linked from a table, so people land there. If a detail page has
nothing to say, it still owes a layout.

### 30. A breakpoint band nothing targets

One breakpoint at 640px against fixed 900/1160px containers meant every
window between ~640 and 1100px scrolled the whole page sideways — and a
resizable desktop window lives in exactly that band. Containers are
`min(Npx, 100%)` now, with a second breakpoint at 1100px.

### 31. Losing signal while consolidating

Retiring `.log-tabs` would have turned the Logs error count from a red
badge into a muted number. Consolidation must carry the meaning across,
not just the markup — `sub_nav` gained a tone argument. A count meaning
"something is wrong" should not look like a count meaning "four files".
