# Chart patterns — the five types in charts.py

Klearing's charts are hand-rolled SVG strings built from Python — no
charting library, no CDN, no runtime network dependency, so the packaged
app stays small and works fully offline in the native window. **Never add
a charting library or a sixth chart type without a strong reason** — extend
one of these five first.

Every function lives in `charts.py`, returns a safe-to-drop-in HTML string,
and shares three rules:

1. Colors come only from CSS classes tied to `ui.py`'s tokens — chart
   functions never hardcode a color.
2. Every chart degrades to `NO_DATA` (`'<div class="none-note">Not enough
   data yet.</div>'`) on thin/empty input — never a broken or misleading
   picture. Exception: `mini_bars` returns `""` instead, because on a
   dashboard tile the right degradation is no chart at all, not an
   apologetic note.
3. Every mark carries a native `<title>` tooltip — color never carries
   meaning alone.

## sparkline(values, labels=None, last_label=None, zero_baseline=False, aria="Trend over time", width=640, height=120, grid=False)

Line-and-area chart, oldest → newest. Use for: any trend over time
(net position, revenue by category, sessions/users, cumulative cash flow).

- Needs 2+ non-None points or returns `NO_DATA`.
- `zero_baseline=True` draws a dashed reference line at y=0 **only when
  the data actually crosses zero** — pass this for anything that can go
  negative (net position), skip it for anything that can't (sessions,
  revenue).
- `grid=True` draws two faint horizontal reference lines. Currently only
  used on Summary's net chart — if you add it elsewhere, do it because
  the data specifically benefits from a visual reference, not by default.
- `last_label` renders the final point's value as text next to the line's
  end — use it to give the reader the current number without a full axis.
- The line animates in via `pathLength="1"` + CSS (`kl-draw`); inert
  under `prefers-reduced-motion`.

## horizontal_bar_chart(rows, aria="Bar chart", width=640, bar_h=26, gap=10, max_bars=12)

Horizontal bars sharing one scale. Use for: rankings (vendor spend, top
pages, top categories, channel conversion rate).

- `rows`: list of dicts — `label` (str), `value` (number), `value_label`
  (str, optional), `warn` (bool, optional → warning color), `neutral`
  (bool, optional → gray, for a bar that shouldn't imply a judgment, e.g.
  the Wave side of a Wave-vs-bank comparison).
- Caller sorts — the function doesn't.
- A negative `value` draws as a zero-length bar with its signed
  `value_label` still shown — this surfaces an imbalance instead of
  hiding it behind `abs()`.
- A `warn` row also colors its value text (`.chart-value.warn`), not
  just the bar — because a warn row can have a zero-length bar (see
  the negative-value rule above), and a warning that only colors the
  bar would be invisible exactly on the overdrawn row it exists to
  mark (Reconcile's per-account breakdown is the real case).
- Label column is 190px wide, paired with `_truncate`'s 22-char default
  limit. **These two numbers are calibrated together** — if you change
  one, re-verify the other against real worst-case content (see
  anti-patterns.md for how this was actually measured, not guessed).
- `bar_h=30` (vs the 26px default) is used on Reconcile's 2-row
  comparison, for more visual breathing room on a short list — a
  deliberate per-call override, not drift.

## funnel_chart(stages, aria="Conversion funnel", width=640, bar_h=44, gap=12)

Shrinking-width funnel, one bar per stage. Currently used only for the
Insights quiz→waitlist funnel (3 stages).

- `stages`: list of `(label, count)` tuples. Needs 2+ stages with a
  positive top count.
- Width scales against the **largest** stage, not the first — real funnel
  data isn't always monotonic (a later stage can exceed an earlier one),
  and scaling to the max keeps every bar on-canvas instead of drawing an
  inverted, clipped bar wider than its container.
- **Hardcoded to a 3-step color ramp** (`chart-funnel-1/2/3`, capped via
  `min(i + 1, 3)`). A 4th+ stage would silently reuse step 3's color and
  its on-dark label logic. If you ever add a 4th stage, extend the ramp
  in `ui.py` and re-verify contrast on the new step before shipping —
  don't just let it inherit step 3.

## mini_bars(values, labels=None, aria="Recent trend")

Tiny column chart for dashboard stat tiles — one rounded pill bar per
value, most recent full-strength, the rest dimmed.

- Needs 2+ points or returns `""` (not `NO_DATA` — see rule 2 above).
- `labels`, when given, are used **verbatim** as each bar's tooltip —
  callers pre-format them (e.g. `"Week of May 04: +$620"`); there's no
  automatic number formatting layered on top.
- Negative values draw as a 2px stub, true value still in the tooltip —
  at 44px tall this is a shape to glance at, not a chart to read closely.
- No visible `<text>` — this is the one chart type unaffected by the
  mobile text-scaling issue in anti-patterns.md, because it has no text
  to scale.

## gauge(value, threshold, unit="days", aria="Runway gauge", width=640, height=54)

Horizontal progress bar, value filled against a threshold. Currently used
only for cash runway vs. the runway alert level.

- Returns `NO_DATA` if `threshold` is None/≤0 or `value` is None/<0.
- Fill color: warning if `value < threshold`, positive otherwise. A tick
  marks the threshold position.
- Scale is `max(value, threshold) * 1.15` — the threshold sits at ~70% of
  the track, and a value well above threshold still stays on-canvas
  (capped, not clipped).

## Shared geometry rule: pill radius scales with height

All five chart types that draw a rounded rect/bar compute
`rx = bar_h / 2` (or `height / 2` for the gauge) — never a fixed radius.
This is what makes bars, funnel segments, and the gauge read as the same
control vocabulary as the app's pill buttons (`border-radius: 999px`)
instead of assorted differently-rounded boxes. If you add a sixth chart
type, follow the same rule.

## Rendering context: gradients and mobile

- Every bar/gauge fill references a shared SVG `<linearGradient>` (ids
  `kl-bar-h`, `kl-bar-h-warn`, `kl-bar-h-neutral`, `kl-bar-v`,
  `kl-area-green`), rendered once per page by `ui.py`'s `CHART_DEFS` and
  injected in the shell — **not** per-chart. Every fill also declares a
  solid CSS fallback color that matches the gradient's light/end stop —
  if you add a new gradient variant, match this pattern exactly (a
  fallback that's a hand-picked color instead of the gradient's own end
  stop is a real bug that shipped once — see anti-patterns.md).
- All five chart types use a fixed-width (640-unit) SVG `viewBox` that
  scales responsively via `.chart-svg { width: 100%; height: auto }`.
  On viewports ≤640px, this is overridden to hold native size and scroll
  horizontally instead of shrinking — see the anti-patterns entry on
  mobile chart-text legibility for why, and never remove that override
  without re-verifying text stays legible at 375px width.
