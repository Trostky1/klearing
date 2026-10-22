"""
Hand-rolled SVG charts for Klearing.

Deliberately dependency-free and offline: no charting library, no CDN,
no runtime network — just small SVG strings built from Python, so the
packaged app stays tiny and works fully inside the native window.

Design rules (kept consistent so every tab reads as one system):
- Colors come from the app's existing palette via CSS classes only
  (see the "Charts" block in ui.py). This module never hardcodes a
  hex value, so no new colors are ever introduced and everything
  tracks the theme.
- Every chart degrades gracefully. Thin or empty data returns the same
  "Not enough data yet." note the rest of the app uses, never a broken
  or misleading picture.
- Marks are thin; lines 2px with rounded ends; bars have rounded
  data-ends and a small gap between them; axes/baselines are recessive.
- Text is always real text (labels/values), never color-only, and each
  mark carries a native <title> tooltip.

Every function returns an HTML string safe to drop into a page.
"""

from html import escape

# The same empty state the tables use, so a data-less chart looks like
# every other "nothing here yet" spot in the app.
NO_DATA = '<div class="none-note">Not enough data yet.</div>'


def _figure(svg, caption=None, aria=""):
    """Wrap an SVG in a responsive figure. `aria` names the chart for
    screen readers; `caption` is optional visible text under it."""
    cap = f'<figcaption class="chart-cap">{caption}</figcaption>' if caption else ""
    return f'<figure class="chart" role="group" aria-label="{escape(aria)}">{svg}{cap}</figure>'


def _fmt_num(value):
    """A compact default number label (callers usually pass their own)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _truncate(text, limit=22):
    """Shorten a long label with an ellipsis; the full text still rides
    in the mark's <title> tooltip, so nothing is lost.

    limit is a character count, not a pixel budget — Python can't measure
    real glyph widths, so this is calibrated against horizontal_bar_chart's
    label column (190px) using worst-case realistic content: measured
    against the live font stack, a 25-char all-caps bank descriptor like
    "AMAZON MKTPLACE PMTS AMZN" (an entirely normal Plaid transaction
    name) rendered at 196px and bled into the bar track. 22 chars of
    similarly wide (all-caps, space-heavy) text stays clear with margin.
    """
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------- sparkline


def sparkline(values, labels=None, last_label=None, zero_baseline=False,
              aria="Trend over time", width=640, height=120, grid=False):
    """A line-and-area chart of `values` over time (oldest → newest).

    values      list of numbers (y). Fewer than 2 points → "Not enough
                data yet.", because a single point isn't a trend.
    labels      optional x labels (same length). Used for hover tooltips
                and drawn as x-axis ticks (all of them when there are 8
                or fewer points, else just the first and last).
    last_label  optional pre-formatted string for the final point (e.g.
                a money value); shown as a direct label so the reader
                gets the current number without a full axis.
    zero_baseline draw a dashed reference line at y=0 when the data
                actually crosses zero (for net).
    grid        draw two faint horizontal grid lines across the plot.

    The line animates in (CSS kl-draw via pathLength) and a soft
    gradient area sits under it; both are inert under reduced-motion.
    """
    points = [v for v in values if v is not None]
    if len(points) < 2:
        return NO_DATA

    pad_l, pad_r, pad_t = 8, 64, 12
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - 8
    axis_y = pad_t + plot_h
    show_ticks = bool(labels)
    tick_h = 22 if show_ticks else 0
    total_h = axis_y + 8 + tick_h

    lo, hi = min(points), max(points)
    if zero_baseline:
        lo, hi = min(lo, 0), max(hi, 0)
    span = (hi - lo) or 1.0

    def x(i):
        return pad_l + (plot_w * i / (len(points) - 1))

    def y(v):
        return pad_t + plot_h * (1 - (v - lo) / span)

    coords = [(x(i), y(v)) for i, v in enumerate(points)]
    path = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)

    # Chrome: bottom axis, optional grid, optional dashed zero line
    # (only when zero is inside the range, i.e. the data crosses it).
    chrome = ""
    if grid:
        mid_y = pad_t + plot_h / 2
        chrome += (f'<line class="chart-grid" x1="{pad_l}" y1="{pad_t}" '
                   f'x2="{pad_l + plot_w}" y2="{pad_t}"/>'
                   f'<line class="chart-grid" x1="{pad_l}" y1="{mid_y:.1f}" '
                   f'x2="{pad_l + plot_w}" y2="{mid_y:.1f}"/>')
    chrome += (f'<line class="chart-axis" x1="{pad_l}" y1="{axis_y}" '
               f'x2="{pad_l + plot_w}" y2="{axis_y}"/>')
    if zero_baseline and lo < 0 < hi:
        zy = y(0)
        chrome += (f'<line class="chart-axis" x1="{pad_l}" y1="{zy:.1f}" '
                   f'x2="{pad_l + plot_w}" y2="{zy:.1f}" '
                   f'stroke-dasharray="3 3"/>')

    # Gradient area under the line, down to zero when the data crosses
    # it (so negative stretches aren't shaded as if positive), else to
    # the bottom axis.
    base_y = y(0) if (zero_baseline and lo < 0 < hi) else axis_y
    area = (f'M{coords[0][0]:.1f},{base_y:.1f} '
            + " ".join(f"L{px:.1f},{py:.1f}" for px, py in coords)
            + f' L{coords[-1][0]:.1f},{base_y:.1f} Z')
    area_svg = f'<path class="chart-area" d="{area}"/>'

    # Native hover tooltips: one invisible dot per point carrying a title.
    dots = ""
    for i, (px, py) in enumerate(coords):
        label = ""
        if labels and i < len(labels):
            label = f"{labels[i]}: "
        dots += (f'<circle cx="{px:.1f}" cy="{py:.1f}" r="8" '
                 f'fill="transparent"><title>{escape(str(label))}'
                 f'{escape(_fmt_num(points[i]))}</title></circle>')

    # X ticks: every label when few points, else just the ends.
    ticks = ""
    if show_ticks:
        tick_y = axis_y + tick_h
        n = len(points)
        idxs = range(n) if n <= 8 else (0, n - 1)
        for i in idxs:
            if i >= len(labels):
                continue
            anchor = ("" if i == 0 else
                      ' text-anchor="end"' if i == n - 1 else
                      ' text-anchor="middle"')
            ticks += (f'<text class="chart-tick" x="{coords[i][0]:.1f}" '
                      f'y="{tick_y}"{anchor}>{escape(str(labels[i]))}</text>')

    lx, ly = coords[-1]
    end_marker = f'<circle class="chart-dot" cx="{lx:.1f}" cy="{ly:.1f}" r="4.5"/>'
    end_label = ""
    if last_label is not None:
        # Keep the label inside the canvas.
        ty = min(max(ly, pad_t + 6), axis_y + 6)
        end_label = (f'<text class="chart-value" x="{lx + 6:.1f}" '
                     f'y="{ty:.1f}">{escape(str(last_label))}</text>')

    svg = (
        f'<svg viewBox="0 0 {width} {total_h:.0f}" class="chart-svg" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(aria)}">'
        f'{chrome}{area_svg}'
        f'<polyline class="chart-line draw" pathLength="1" points="{path}"/>'
        f'{dots}{end_marker}{end_label}{ticks}'
        f'</svg>'
    )
    return _figure(svg, aria=aria)


# ------------------------------------------------------- horizontal bars


def horizontal_bar_chart(rows, aria="Bar chart", width=640, bar_h=26,
                         gap=10, max_bars=12):
    """Horizontal bars sharing one scale.

    rows: list of dicts, each:
      label        left-hand name (required)
      value        numeric magnitude for bar length (required). A bar
                   shows "how much", so a negative value draws as an
                   empty bar (length 0) with its signed value_label —
                   which surfaces an imbalance (e.g. an overdrawn
                   balance) rather than hiding it behind abs().
      value_label  right-hand text (optional; defaults to the number)
      warn         True → warning-colored bar (optional)
      neutral      True → neutral gray bar (optional; e.g. the Wave side
                   of the reconcile comparison, where green would imply
                   a judgement)

    Caller sorts. No positive values → "Not enough data yet."
    """
    rows = [r for r in rows if r is not None][:max_bars]
    if not rows:
        return NO_DATA
    magnitudes = [max(r["value"], 0) for r in rows]
    scale_max = max(magnitudes) if magnitudes else 0
    if scale_max <= 0:
        return NO_DATA

    label_w = 190  # paired with _truncate's default limit — see its docstring
    value_w = 96
    track_x = label_w + 8
    track_w = width - track_x - value_w
    height = len(rows) * (bar_h + gap) + gap
    rx = bar_h / 2  # pill-shaped bars, matching the control vocabulary

    body = ""
    for i, row in enumerate(rows):
        y = gap + i * (bar_h + gap)
        mag = max(row["value"], 0)
        bar_w = max(2.0, track_w * mag / scale_max) if mag > 0 else 0
        bar_cls = ("chart-bar warn" if row.get("warn")
                   else "chart-bar neutral" if row.get("neutral")
                   else "chart-bar")
        value_label = row.get("value_label", _fmt_num(row["value"]))
        full_label = str(row["label"])
        label = escape(_truncate(full_label))
        title_label = escape(full_label)
        text_y = y + bar_h / 2

        bar_svg = ""
        if bar_w > 0:
            bar_svg = (f'<rect class="{bar_cls}" x="{track_x}" y="{y}" '
                       f'width="{bar_w:.1f}" height="{bar_h}" rx="{rx:.0f}">'
                       f'<title>{title_label}: {escape(str(value_label))}</title>'
                       f'</rect>')
        # A warn row's bar can be zero-length (negative value draws no
        # bar), so the warning also colors the value text — otherwise
        # the flag would be invisible exactly on the overdrawn row it
        # exists to mark.
        value_cls = "chart-value warn" if row.get("warn") else "chart-value"
        body += (
            f'<text class="chart-label" x="0" y="{text_y:.1f}" '
            f'dominant-baseline="middle">{label}</text>'
            f'<rect class="chart-track" x="{track_x}" y="{y}" '
            f'width="{max(track_w, 0):.1f}" height="{bar_h}" rx="{rx:.0f}"/>'
            f'{bar_svg}'
            f'<text class="{value_cls}" x="{width}" y="{text_y:.1f}" '
            f'text-anchor="end" dominant-baseline="middle">'
            f'{escape(str(value_label))}</text>'
        )

    svg = (f'<svg viewBox="0 0 {width} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMinYMin meet" role="img" '
           f'aria-label="{escape(aria)}">{body}</svg>')
    return _figure(svg, aria=aria)


# --------------------------------------------------------------- funnel


def funnel_chart(stages, aria="Conversion funnel", width=640, bar_h=44,
                 gap=12):
    """A shrinking-width funnel: one centered bar per stage, width
    proportional to its count, with the count and the conversion rate
    from the previous stage labeled on each bar.

    stages: list of (label, count). Needs at least two stages with a
    positive top count; otherwise "Not enough data yet."
    """
    stages = [(str(name), int(count or 0)) for name, count in (stages or [])]
    counts = [c for _, c in stages]
    denom = max(counts) if counts else 0
    if len(stages) < 2 or denom <= 0:
        return NO_DATA

    # Width is proportional to count against the LARGEST stage, not the
    # first. Real funnel events aren't always monotonic (e.g.
    # waitlist_joined fires from entry points other than the quiz, so it
    # can exceed quiz_started); scaling to the max keeps every bar on the
    # canvas instead of drawing an inverted, clipped bar wider than the
    # top. The per-stage "% of prior stage" label still tells the true
    # story.
    height = len(stages) * (bar_h + gap) + gap
    rx = bar_h / 2
    body = ""
    for i, (name, count) in enumerate(stages):
        y = gap + i * (bar_h + gap)
        frac = count / denom  # in [0, 1] — never exceeds the canvas
        bar_w = max(6.0, width * frac)
        x = (width - bar_w) / 2
        # Conversion from the previous stage.
        if i == 0:
            conv = "100%"
        else:
            prev = stages[i - 1][1]
            conv = (f"{(count / prev * 100):.0f}% of prior stage"
                    if prev else "—")
        label = escape(name)
        # An ordinal green ramp, one class per step (capped at 3); the
        # deepest step's fill is dark, so its label ink flips to white
        # to keep contrast (both combinations verified AA in ui.py).
        step = min(i + 1, 3)
        on_dark = " on-dark" if step == 3 else ""
        body += (
            f'<rect class="chart-funnel-{step}" x="{x:.1f}" y="{y}" '
            f'width="{bar_w:.1f}" height="{bar_h}" rx="{rx:.0f}">'
            f'<title>{label}: {count:,} ({conv})</title></rect>'
            f'<text class="chart-funnel-label{on_dark}" x="{width / 2:.1f}" '
            f'y="{y + bar_h / 2 - 2:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle">{label} · {count:,}</text>'
            f'<text class="chart-funnel-sub{on_dark}" x="{width / 2:.1f}" '
            f'y="{y + bar_h / 2 + 13:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle">{conv}</text>'
        )

    svg = (f'<svg viewBox="0 0 {width} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMidYMin meet" role="img" '
           f'aria-label="{escape(aria)}">{body}</svg>')
    return _figure(svg, aria=aria)


# ------------------------------------------------------------- mini bars


def mini_bars(values, labels=None, aria="Recent trend"):
    """A tiny column chart for dashboard stat tiles: one rounded pill
    bar per value, the most recent full-strength and the rest dimmed.
    Returns "" (not the no-data note) on thin data — on a dash card the
    right degradation is simply no chart, the headline number stands.

    Negative values draw as a minimal stub (the true signed value still
    rides in the tooltip) — at 44px tall these are a shape to glance at,
    not a chart to read.

    labels, when given, are used VERBATIM as each bar's tooltip (callers
    pre-format them, e.g. "Week of May 04: +$620"); otherwise the bare
    number is used.
    """
    points = [v for v in (values or []) if v is not None]
    if len(points) < 2:
        return ""

    bar_w, gap, vb_h = 8, 5.5, 44
    n = len(points)
    vb_w = n * (bar_w + gap) - gap
    top = max(max(points), 0)
    if top <= 0:
        return ""

    bars = ""
    for i, v in enumerate(points):
        h = max(4.0, vb_h * max(v, 0) / top) if v > 0 else 2.0
        y = vb_h - h
        x = i * (bar_w + gap)
        cls = "chart-minibar" if i == n - 1 else "chart-minibar dim"
        title = (str(labels[i]) if labels and i < len(labels)
                 else _fmt_num(v))
        bars += (f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" '
                 f'width="{bar_w}" height="{h:.1f}" rx="{bar_w / 2:.0f}">'
                 f'<title>{escape(title)}</title>'
                 f'</rect>')

    return (f'<svg class="mini-chart" viewBox="0 0 {vb_w:.0f} {vb_h}" '
            f'preserveAspectRatio="xMinYMax meet" role="img" '
            f'aria-label="{escape(aria)}">{bars}</svg>')


# ---------------------------------------------------------------- gauge


def gauge(value, threshold, unit="days", aria="Runway gauge", width=640,
          height=54):
    """A horizontal progress bar: `value` filled against `threshold`.

    Below the threshold fills with the warning color; at or above, the
    positive color. A tick marks the threshold. Returns "Not enough data
    yet." when there's no usable threshold or value to plot.
    """
    if threshold is None or threshold <= 0 or value is None or value < 0:
        return NO_DATA

    pad = 4
    track_x = pad
    track_w = width - 2 * pad
    bar_y = 12
    bar_h = 20

    # Scale so the threshold sits at ~70% of the track; a value well
    # above the threshold still stays on-canvas (capped at the track).
    scale_max = max(value, threshold) * 1.15
    frac = min(value / scale_max, 1.0)
    fill_w = max(2.0, track_w * frac)
    thr_x = track_x + track_w * (threshold / scale_max)

    below = value < threshold
    fill_cls = "chart-gauge-fill warn" if below else "chart-gauge-fill ok"
    value_txt = f"{value:.0f} {unit}"
    thr_txt = f"alert at {threshold:.0f}"

    svg = (
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(aria)}: {escape(value_txt)}, {escape(thr_txt)}">'
        f'<rect class="chart-track" x="{track_x}" y="{bar_y}" '
        f'width="{track_w:.1f}" height="{bar_h}" rx="{bar_h / 2:.0f}"/>'
        f'<rect class="{fill_cls}" x="{track_x}" y="{bar_y}" '
        f'width="{fill_w:.1f}" height="{bar_h}" rx="{bar_h / 2:.0f}">'
        f'<title>{escape(value_txt)} · {escape(thr_txt)} {unit}</title></rect>'
        f'<line class="chart-gauge-tick" x1="{thr_x:.1f}" y1="{bar_y - 3}" '
        f'x2="{thr_x:.1f}" y2="{bar_y + bar_h + 3}"/>'
        f'<text class="chart-value" x="{track_x}" y="{height - 2}">'
        f'{escape(value_txt)}</text>'
        f'<text class="chart-label" x="{width}" y="{height - 2}" '
        f'text-anchor="end">{escape(thr_txt)} {escape(unit)}</text>'
        f'</svg>'
    )
    return _figure(svg, aria=aria)
