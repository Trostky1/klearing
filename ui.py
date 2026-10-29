"""
Shared page rendering for Klearing's web app.

One CSS block, one HTML shell, used by both the main tabs (app.py)
and the Setup wizard (setup_wizard.py). Plain CSS, no build step.
"""

import json
import re
from html import escape

import paths
from config import APP_NAME

# Single source of truth for the accent green. The :root token, the
# funnel chart's first-step fill, and CHART_DEFS' SVG gradient stops
# (kl-bar-h, kl-bar-v, kl-area-green) used to hardcode this same hex
# independently — three copies that had to be kept in sync by hand.
# SVG <stop> color can't reliably take var() the way fill="var()" does
# on plain shapes (see BRAND_MARK below), so the SVG copies stay
# literal, but now they're generated from this one Python value instead
# of being typed three more times.
ACCENT_HEX = "#4ADE80"

_ROOT_VARS = f"""
  :root {{
    /* Surfaces. Four steps, widened from three: the old --bg/--surface
       gap was small enough that cards read flat against the page. Every
       card-like keeps the hairline top highlight so the stack has a
       light source. --overlay is for things that must sit clearly above
       everything (command palette, dialogs). */
    --bg: #08080A;
    --surface: #121216;
    --raised: #1A1A20;
    --overlay: #202027;
    --ink: #F4F4F6;
    --muted: #A3A3AF;
    --faint: #8B8B98;            /* 5.9:1 on --surface — AA with headroom */
    --line: #232329;
    --line-strong: #3A3A44;
    --hover-fill: rgba(255, 255, 255, 0.05);

    /* Brand vs action vs income. These used to be one value, so a
       primary button and an income figure carried identical colour for
       different meanings. Green now means exactly one thing — positive,
       income, healthy — plus brand identity (mark, active nav, focus
       ring). Primary actions are ink-filled instead, which is what
       Vercel and Linear both do and what stops the eye reading a green
       Approve button as a money figure.
       --accent* remain as aliases so ~100 existing call sites keep
       working; they migrate to --brand* in Phase 2. */
    --brand: {ACCENT_HEX};
    --brand-strong: #86EFAC;
    --brand-soft: rgba(74, 222, 128, 0.10);
    --brand-line: rgba(74, 222, 128, 0.28);
    --brand-ink: #86EFAC;
    --action: var(--ink);
    --action-ink: var(--bg);
    --accent: var(--brand);
    --accent-strong: var(--brand-strong);
    --accent-soft: var(--brand-soft);
    --accent-line: var(--brand-line);
    --accent-ink: var(--brand-ink);
    --income: {ACCENT_HEX};
    --income-soft: rgba(74, 222, 128, 0.10);
    --income-line: rgba(74, 222, 128, 0.30);
    --expense: #F87171;
    --transfer: #2DD4BF;
    --warn: #FBBF24;
    --warn-soft: rgba(251, 191, 36, 0.10);
    --warn-line: rgba(251, 191, 36, 0.30);
    --error-bg: rgba(248, 113, 113, 0.10);
    --error-line: rgba(248, 113, 113, 0.30);
    --expense-ink: #FCA5A5;      /* lighter expense tint, for text on a soft/tinted fill */
    --hover-border-strong: #55555F;  /* outline pill hover border, one step past --line-strong */
    --log-bg: #0E0E11;           /* log box: recessed, between --bg and --surface */
    --log-ink: #C9C9D2;          /* log text: softer than --ink, brighter than --muted */
    --white: #FFFFFF;            /* true white — hover-brightened filled-button
                                     backgrounds, on-dark funnel-step label text */
    --delta-watch: #FCD34D;      /* .delta.watch text — deliberately distinct
                                     from --warn, not a duplicate needing merge */
    --dash-card-hover: #17171B;  /* a.dash-card hover bg, one step lighter
                                     than --surface */
    --radius: 16px;
    --radius-sm: 12px;
    /* Height of the mobile bottom tab bar (44px WCAG 2.5.5 touch target
       plus symmetric --sp-2 padding). Named rather than repeated as a
       calc() at each of its three call sites — the bar's own CSS, the
       main-inner scroll padding that clears it, and the AI fab/panel and
       bulk-bar offsets that sit above it — so all four move together. */
    --bottom-nav-h: calc(44px + 2 * var(--sp-2));

    /* Space. Replaces ~20 ad-hoc values between 2 and 44px that had no
       relationship to each other. One 4px scale; every old value maps
       to its nearest step. Container padding collapses to two options,
       --pad-compact and --pad, and vertical rhythm to three gaps. */
    --sp-1: 4px;
    --sp-2: 8px;
    --sp-3: 12px;
    --sp-4: 16px;
    --sp-5: 24px;
    --sp-6: 32px;
    --sp-7: 48px;
    --sp-8: 64px;
    --pad-compact: var(--sp-3) var(--sp-4);
    --pad: var(--sp-4) var(--sp-5);
    --gap-section: var(--sp-5);   /* between sections */
    --gap-block: var(--sp-4);     /* within a section */
    --gap-tight: var(--sp-2);     /* label to value */

    /* Motion. Two durations, one easing. */
    --t-state: 120ms;
    --t-overlay: 200ms;
    --ease: cubic-bezier(0.32, 0.72, 0, 1);

    /* Type. Three families, all bundled — see @font-face below.
       Instrument Sans carries the interface. Instrument Serif is
       reserved for documents (statement titles, the Home briefing, the
       accountant-export cover) so the things you would hand an
       accountant read in a different register from the things you work
       in; it never appears in body copy or tables. Commit Mono keeps
       the job mono is actually good at — dense columns of figures —
       and gives up the job it was bad at: hero numbers, which now use
       Instrument Sans with tabular figures. */
    --font-ui: "Instrument Sans", -apple-system, "Segoe UI", Roboto,
               Helvetica, Arial, sans-serif;
    --font-doc: "Instrument Serif", Georgia, "Times New Roman", serif;
    --font-mono: "Commit Mono", ui-monospace, "Cascadia Mono", Consolas,
                 "SF Mono", monospace;
    /* Type scale. Nine steps spanning 11-56px, replacing six steps
       spanning 10-20px plus four literal exceptions.

       The old scale's problem was not inconsistency — it was healthy —
       but range: nine of its ten sizes sat between 10 and 18px, so
       every dense screen read as an undifferentiated grey wall with no
       hierarchy to break it. The top three steps here are the fix, and
       they are load-bearing: --fs-xl for page titles and stat values,
       --fs-2xl and --fs-3xl for the serif document register.

       All four old literal exceptions are folded in: the app h1
       (1.4rem -> --fs-xl), .dash-value (1.5rem -> --fs-xl),
       .dash-mini-label (0.68rem -> --fs-2xs). .health-grade .g keeps a
       literal for now because its 88px circle constrains it and the
       component is slated for rebuild. */
    --fs-2xs: 0.6875rem;   /* 11px  micro labels, eyebrow */
    --fs-xs: 0.75rem;      /* 12px  metadata, table headers */
    --fs-sm: 0.8125rem;    /* 13px  secondary text */
    --fs-base: 0.875rem;   /* 14px  body, table cells */
    --fs-md: 1rem;         /* 16px  card titles */
    --fs-lg: 1.25rem;      /* 20px  section headings */
    --fs-xl: 1.75rem;      /* 28px  page titles, stat values */
    --fs-2xl: 2.5rem;      /* 40px  statement titles (serif) */
    --fs-3xl: 3.5rem;      /* 56px  briefing headline (serif) */
  }}
"""

# Bundled so the app stays offline-safe and looks identical on every
# machine — no CDN, no build step, served by route exactly like
# frontend/tax/*.js already is. Licences ship alongside the files.
# font-display: swap keeps text visible if a file is ever missing; the
# fallback stacks in --font-* mean a missing file degrades to the old
# system stack rather than breaking the page.
_FONT_FACES = """
  @font-face {
    font-family: "Instrument Sans"; font-style: normal;
    font-weight: 400 700; font-display: swap;
    src: url("/static/fonts/instrument-sans.woff2") format("woff2");
  }
  @font-face {
    font-family: "Instrument Serif"; font-style: normal;
    font-weight: 400; font-display: swap;
    src: url("/static/fonts/instrument-serif.woff2") format("woff2");
  }
  /* Commit Mono ships as static weights, not a variable font, so — unlike
     the two Google Fonts families above — it needs one @font-face per
     weight rather than one block covering a range. */
  @font-face {
    font-family: "Commit Mono"; font-style: normal;
    font-weight: 400; font-display: swap;
    src: url("/static/fonts/commit-mono.woff2") format("woff2");
  }
  @font-face {
    font-family: "Commit Mono"; font-style: normal;
    font-weight: 700; font-display: swap;
    src: url("/static/fonts/commit-mono-bold.woff2") format("woff2");
  }
"""

STYLE = _ROOT_VARS + _FONT_FACES + """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { color-scheme: dark; }
  body {
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-ui);
    line-height: 1.5;
    font-size: var(--fs-base);
    /* Lining tabular figures everywhere by default. In a bookkeeping app
       almost every number sits in a column that has to align, and
       Instrument Sans' tabular set is what lets hero figures leave the
       monospace stack without losing that alignment. */
    font-variant-numeric: tabular-nums lining-nums;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  ::selection { background: rgba(74, 222, 128, 0.30); }
  .wrap { max-width: 760px; margin: 0 auto; padding: 40px 24px 88px; }
  .wrap.wide { max-width: 1100px; }
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 16px; flex-wrap: wrap; margin-bottom: 16px;
  }
  h1 { font-size: var(--fs-xl); font-weight: 700; letter-spacing: -0.015em; }
  .header-actions { display: flex; gap: 8px; align-items: center; }

  /* Skip past the 12-item sidebar for keyboard users (WCAG 2.4.1). */
  .skip-nav {
    position: fixed; left: 16px; top: -52px; z-index: 1100;
    background: var(--ink); color: var(--bg);
    padding: 10px 18px; border-radius: 999px;
    font-size: var(--fs-base); font-weight: 600; text-decoration: none;
    transition: top 0.15s;
  }
  .skip-nav:focus-visible { top: 16px; outline: 2px solid var(--accent); outline-offset: 2px; }

  nav {
    display: flex; gap: 2px; margin-bottom: 28px;
    border-bottom: 1px solid var(--line);
    overflow-x: auto; overflow-y: hidden; white-space: nowrap;
    scrollbar-width: none;
  }
  nav a {
    padding: 9px 13px; text-decoration: none; color: var(--muted);
    font-size: var(--fs-base); font-weight: 500;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color 0.15s;
  }
  nav a.active { color: var(--ink); font-weight: 600; border-bottom-color: var(--accent); }
  nav a:hover { color: var(--ink); }

  /* App shell: left sidebar + scrolling main. */
  .app-shell { display: flex; align-items: stretch; min-height: 100vh; }
  .sidebar {
    width: 218px; flex-shrink: 0; background: var(--bg);
    border-right: 1px solid var(--line); padding: 22px 14px;
    position: sticky; top: 0; align-self: flex-start; height: 100vh;
    overflow-y: auto; scrollbar-width: thin;
  }
  .sidebar-brand {
    display: flex; align-items: center; gap: 10px;
    font-size: var(--fs-lg); font-weight: 700; letter-spacing: -0.01em;
    padding: 2px 10px 20px;
  }
  .brand-mark { width: 26px; height: 26px; flex-shrink: 0; display: block; }
  .side-nav { display: flex; flex-direction: column; gap: 3px; }
  /* The role="group" wrapper exists for screen readers, not for layout —
     so it inherits the rail's own column-and-gap and stays invisible to
     the eye. Without this it defaults to display:block, the flex gap
     stops applying between links inside a group, and they close up to
     2px and -1px (measured) instead of a uniform 3px. */
  .side-nav [role="group"] { display: flex; flex-direction: column; gap: 3px; }
  .side-nav a {
    display: flex; align-items: center; gap: 11px;
    padding: 9px 13px; border-radius: var(--radius-sm);
    color: var(--muted); text-decoration: none; font-size: var(--fs-base);
    font-weight: 500; border: 1px solid transparent;
    transition: background-color 0.15s, color 0.15s, border-color 0.15s;
  }
  .side-nav a:hover { background: var(--hover-fill); color: var(--ink); }
  .side-nav a.active {
    background: var(--brand-soft); color: var(--brand-ink);
    font-weight: 600; border-color: var(--brand-line);
  }
  .nav-icon {
    width: 20px; height: 20px; flex-shrink: 0;
    fill: none; stroke: currentColor; stroke-width: 1.75;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .main { flex: 1; min-width: 0; }
  /* Fluid rather than fixed. These were hard 900/1160px against a
     single 640px breakpoint, so every window between about 640 and
     1100px rendered content wider than its viewport and scrolled the
     whole page sideways — and a resizable desktop window lives in
     exactly that band. min() keeps the same maximum on a wide screen
     and simply stops overflowing below it. */
  .main-inner {
    max-width: min(900px, 100%); margin: 0 auto; padding: 32px 30px 80px;
  }
  .main-inner.wide { max-width: min(1160px, 100%); }
  .main-inner > header { margin-bottom: 22px; }
  .main-inner > header h1 { font-size: var(--fs-xl); letter-spacing: -0.02em; }

  .sub { color: var(--muted); font-size: var(--fs-base); margin-bottom: var(--gap-section); max-width: 65ch; }
  /* Secondary explanatory text, one step below .sub. Deliberately NOT
     parent-scoped: this rule lived only as `.field .hint` and
     `.cred-field .hint`, so of 73 usages across app.py, setup_wizard.py
     and ui.py, 24 sat outside a form field and silently rendered as
     full-size body ink — every hint on Contacts, Inventory, Recurring
     invoices and Tools among them. The class name reads generic and
     autocomplete offers it, so it was reused wherever a hint was wanted;
     nothing errored, the page just rendered wrong (anti-patterns #13).
     The two originals were identical apart from 62ch vs 60ch, which
     carried no comment marking it deliberate and was drift rather than
     an intentional exception (cf. #22). */
  .hint { color: var(--muted); font-size: var(--fs-sm); margin-bottom: 8px; max-width: 60ch; }
  /* Base link color for all page content (both shells: main = app,
     .wrap = unlock/setup). :where() keeps specificity at zero, so any
     class rule (.btn-link, a.dash-card, .banner.error a, ...) still
     wins. This replaced a per-container list (.sub a, .hint a, ...)
     that had to grow every time a link appeared in a new container —
     and missed .banner a for five straight cycles (the Home update
     banner's link rendered browser-default blue). */
  :where(main, .wrap) a { color: var(--accent-ink); }

  .banner {
    border: 1px solid var(--accent-line); border-radius: var(--radius-sm);
    background: var(--accent-soft); color: var(--accent-ink);
    padding: var(--pad-compact); font-size: var(--fs-base); margin-bottom: var(--gap-block);
  }
  .banner.error {
    background: var(--error-bg); border-color: var(--error-line);
    color: var(--expense);
  }
  .banner.error a { color: var(--expense-ink); }
  .banner.ai-insight {
    display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  }

  /* Shared "eyebrow" micro-label treatment: small uppercase caption
     text for stat labels, table headers, form-field captions, dashboard
     kickers, and chart titles. One declaration so the type scale can't
     silently drift between call sites the way it already had (h3 was
     0.75rem instead of 0.72rem; .dev-label's tracking was 0.05em
     instead of 0.04em) — each site below adds only its own layout. */
  .totals .label, .dash-kicker, .chart-title, .fu-field label, h3, th,
  .dash-mini-label, .dev-tools .dev-label, .ai-insight-kicker {
    font-size: var(--fs-xs); font-weight: 600; color: var(--faint);
  }
  /* Deliberately smaller than --fs-xs (the shared eyebrow rule above) —
     a documented exception for this denser 3-up dash-card context, not
     scale drift. Kept as its own literal rather than folded into the
     6-step scale so the intentional distinction from the eyebrow
     default doesn't silently disappear. */
  .dash-mini-label { font-size: var(--fs-2xs); }

  /* Buttons: primary = filled light pill (dark-fintech CTA),
     secondary = hairline outline pill. Shared pill geometry (size,
     weight, radius, padding) lives on one rule below so a real
     <button> and an anchor styled as one (.btn-link, defined further
     down) can't drift — they used to restate the same four values
     independently. This rule must stay ABOVE that shared one: `font:
     inherit` resets size/weight, and the later rule's same-specificity
     override is what makes them 0.875rem/600 again. */
  button {
    font: inherit; cursor: pointer;
    border: 1px solid transparent;
    transition: background-color 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s;
  }
  button, .btn-link {
    font-size: var(--fs-base); font-weight: 600;
    border-radius: 999px; padding: 8px 18px;
  }
  /* Pressed feedback — shared with .btn-link so the two "looks like a
     button" systems stay one vocabulary (see anti-patterns.md #21). */
  button:active:not(:disabled), .btn-link:active { transform: scale(0.97); }
  button:focus-visible, a:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .approve, .save-btn {
    background: var(--action); color: var(--action-ink);
  }
  .approve:hover:not(:disabled), .save-btn:hover:not(:disabled) {
    background: var(--white); box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.08);
  }
  .deny, .check {
    background: transparent; border-color: var(--line-strong); color: var(--ink);
  }
  .deny:hover, .check:hover {
    border-color: var(--hover-border-strong); background: var(--hover-fill);
  }

  /* Form controls: one vocabulary everywhere */
  input[type="text"], input[type="number"], input[type="date"],
  input[type="password"], input[type="email"], select, textarea {
    font: inherit; font-size: var(--fs-base); color: var(--ink);
    background: var(--raised); border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm); padding: 7px 10px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(74, 222, 128, 0.18);
  }
  input[type="checkbox"] { accent-color: var(--accent); width: 16px; height: 16px; }

  /* Shared "panel" surface treatment (raised card look: surface bg,
     hairline border, inset top highlight) used by every card-shaped
     element below — .card, .section, .fu-card, .accounts li,
     a.dash-card, .health-card. One declaration so the recipe can't
     drift between call sites; these six used to restate the same
     three properties independently. Radius and layout stay
     per-selector since they genuinely differ (radius-sm on the denser
     Settings list, flex vs block per component). */
  .card, .section, .fu-card, .accounts li, a.dash-card, .health-card {
    background: var(--surface);
    border: 1px solid var(--line);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }

  /* Pending cards */
  .card {
    border-radius: var(--radius);
    padding: var(--pad);
    margin-bottom: var(--gap-block);
    display: flex; align-items: center; gap: 20px; flex-wrap: wrap;
  }
  .card-main { flex: 1; min-width: 240px; }
  .vendor { font-weight: 600; font-size: var(--fs-md); }
  .meta { color: var(--faint); font-size: var(--fs-sm); margin-top: 2px; }
  /* Pending-queue age: quiet until an item has sat 7+ days, then warns. */
  .waiting { color: var(--muted); font-size: var(--fs-sm); margin-top: 2px; }
  .waiting.warn { color: var(--warn); }
  .category { font-size: var(--fs-base); margin-top: 10px; color: var(--muted); }
  .category span {
    background: var(--raised);
    border: 1px solid var(--line-strong);
    border-radius: 999px; padding: 2px 10px; color: var(--ink);
  }
  .category-select { max-width: 320px; }
  .amount {
    font-size: var(--fs-lg); font-weight: 600; white-space: nowrap;
    font-family: var(--font-mono); letter-spacing: -0.01em;
  }
  .amount.income { color: var(--income); }
  .amount.expense { color: var(--expense); }
  .amount.transfer { color: var(--transfer); }
  .actions { display: flex; gap: 8px; }

  /* Pills */
  .jump-tag, .tag {
    font-size: var(--fs-sm); font-weight: 600;
    border-radius: 999px; padding: 2px 10px; white-space: nowrap;
  }
  .jump-tag { color: var(--warn); border: 1px solid var(--warn-line); background: var(--warn-soft); margin-left: 8px; }
  .tag { color: var(--muted); border: 1px solid var(--line-strong); }
  .tag.ok { color: var(--income); border-color: var(--income-line); background: var(--income-soft); }
  .tag.off { color: var(--faint); }
  .tag.warn { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
  .tag.bad { color: var(--expense); border-color: var(--error-line); background: var(--error-bg); }

  /* Delta pills (stat-tile change indicators) */
  .delta {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: var(--fs-xs); font-weight: 700; border-radius: 999px;
    padding: 2px 9px; white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .delta.up { color: var(--accent-strong); background: var(--income-soft); }
  .delta.down { color: var(--expense-ink); background: var(--error-bg); }
  .delta.watch { color: var(--delta-watch); background: var(--warn-soft); }
  /* Direction only, no good/bad judgment — e.g. "money out" changed, which
     isn't inherently better or worse for a growing business. */
  .delta.neutral { color: var(--muted); background: var(--hover-fill); }
  .delta-line { margin-top: 5px; }

  /* Empty states */
  .empty {
    border: 1px dashed var(--line-strong); border-radius: var(--radius);
    padding: 48px 24px; text-align: center; color: var(--muted);
    background: var(--surface);
  }
  .empty p { margin-bottom: 4px; }
  .empty strong { color: var(--ink); }

  footer { margin-top: 40px; color: var(--faint); font-size: var(--fs-sm); }

  /* Dev tools */
  .dev-tools { display: flex; align-items: center; gap: 8px; margin-bottom: 20px; }
  .dev-tools form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .dev-insert {
    background: transparent; color: var(--muted);
    border: 1px dashed var(--line-strong); font-size: var(--fs-sm); font-weight: 500;
    padding: 6px 14px;
  }
  .dev-insert:hover { color: var(--ink); border-color: var(--hover-border-strong); }

  /* Summary page */
  .range-form {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    margin-bottom: 24px;
  }
  .range-form label { font-size: var(--fs-base); color: var(--muted); }
  .planning-controls, .planning-source, .planning-ranges, .planning-manual {
    display: flex; gap: 8px; align-items: end; flex-wrap: wrap;
  }
  .planning-controls { justify-content: space-between; margin-bottom: 24px; }
  .planning-source button, .planning-ranges button {
    background: transparent; border-color: var(--line-strong); color: var(--muted);
  }
  .planning-source button.active, .planning-ranges button.active {
    color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft);
  }
  .planning-manual label, .planning-target label {
    display: grid; gap: 4px; color: var(--muted); font-size: var(--fs-sm);
  }
  .planning-manual input { width: 170px; }
  .planning-target input { width: 210px; }
  .planning-results { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: var(--radius-sm); overflow: hidden; }
  .planning-result { display: flex; justify-content: space-between; gap: 12px; padding: 12px; background: var(--surface); font-size: var(--fs-base); }
  .planning-result .label { color: var(--muted); }
  .planning-result .value { font-family: var(--font-mono); font-weight: 600; text-align: right; }
  .planning-result .value.mixed { font-family: inherit; letter-spacing: normal; }
  .planning-result .value.positive { color: var(--income); }
  .planning-result .value.nonpositive { color: var(--expense); }
  .planning-note { color: var(--faint); font-size: var(--fs-sm); margin-top: 16px; }
  .tax-workspace { min-width: 0; }
  .tax-toolbar, .tax-toolbar-group {
    display: flex; align-items: end; gap: 8px; flex-wrap: wrap;
  }
  .tax-toolbar { justify-content: space-between; margin-bottom: 18px; }
  .tax-toolbar-group label {
    display: grid; gap: 4px; color: var(--muted); font-size: var(--fs-sm);
  }
  .tax-toolbar input[type="number"] { width: 150px; }
  .tax-table-section { padding-bottom: 10px; }
  .tax-table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  #tax-legacy-table { min-width: 760px; }
  .tax-row-title { font-weight: 600; color: var(--ink); }
  .tax-row-detail { color: var(--faint); font-size: var(--fs-sm); margin-top: 2px; }
  .tax-confidence {
    display: inline-flex; align-items: center; border: 1px solid var(--line-strong);
    border-radius: 999px; padding: 3px 8px; font-size: var(--fs-xs);
    font-weight: 600; white-space: nowrap;
  }
  .tax-confidence.high { color: var(--income); border-color: var(--income-line); background: var(--income-soft); }
  .tax-confidence.medium { color: var(--warn); border-color: var(--warn-line); background: var(--warn-soft); }
  .tax-confidence.low { color: var(--expense-ink); border-color: var(--error-line); background: var(--error-bg); }
  .tax-confidence.none { color: var(--muted); }
  .tax-amount { font-family: var(--font-mono); font-weight: 600; white-space: nowrap; }
  .tax-amount.income { color: var(--income); }
  .tax-amount.expense { color: var(--expense); }
  .tax-results-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .tax-results-grid .section { min-width: 0; }
  .tax-dialog {
    width: min(460px, calc(100% - 28px)); color: var(--ink); background: var(--surface);
    border: 1px solid var(--line); border-radius: var(--radius); padding: 22px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  .tax-dialog::backdrop { background: var(--bg); opacity: 0.82; }
  .tax-dialog h2 { margin-bottom: 8px; }
  .tax-warning-list { margin: 0; padding-left: 20px; color: var(--muted); }
  .tax-warning-list li + li { margin-top: 8px; }
  .section {
    border-radius: var(--radius);
    padding: var(--pad);
    margin-bottom: var(--gap-block);
  }
  .section.unposted { background: transparent; border-style: dashed; box-shadow: none; }
  .section-head {
    display: flex; align-items: baseline; gap: 10px; margin-bottom: 14px;
  }
  .section-head h2 { font-size: var(--fs-md); font-weight: 600; }
  .totals { display: flex; gap: 32px; margin-bottom: 6px; flex-wrap: wrap; }
  .totals .value {
    font-size: var(--fs-lg); font-weight: 600;
    font-family: var(--font-mono); letter-spacing: -0.01em;
  }
  .totals .value.in { color: var(--income); }
  .totals .value.out { color: var(--expense); }
  .totals .value.transfer { color: var(--transfer); }
  h3 { margin: 18px 0 2px; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: var(--fs-base); }
  th, td { text-align: left; padding: 9px 6px; border-bottom: 1px solid var(--line); }
  th.num, td.num {
    text-align: right; font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
  }
  /* .num cells are pure numbers/money almost everywhere; th.num headers
     and the rare word-bearing cell ("Not recorded") opt back to the body
     sans stack — same escape hatch as .dash-value.mixed. */
  th.num, td.num.mixed { font-family: inherit; }
  tr:last-child td { border-bottom: none; }
  td.vendor-name { font-weight: 600; }
  td.vendor-name a { text-decoration: none; }  /* color from the base link rule */
  td.vendor-name a:hover { text-decoration: underline; }
  .none-note { color: var(--muted); font-size: var(--fs-base); padding: 6px 2px; }

  /* ---------------------------------------------------------------
     Consolidated components (redesign Phase 2)
     --------------------------------------------------------------- */

  /* One stat vocabulary. There used to be three unrelated systems —
     .dash-value on Home and Tools, .totals .value on Summary/Shopify/
     Export, .planning-result .value on Tax and Financial Insights —
     each with its own font rule and its own .mixed escape hatch. They
     are the same thing at three scales, so the typography is now one
     rule and only the size differs. Same technique as the shared
     raised-panel rule above: group the selectors, don't duplicate the
     declarations. */
  .stat-value, .dash-value, .totals .value, .planning-result .value {
    font-weight: 600; letter-spacing: -0.02em; line-height: 1.15;
    font-variant-numeric: tabular-nums lining-nums;
  }
  .stat-value { font-size: var(--fs-xl); }
  /* ui.stat(tone=...) has always emitted these classes. */
  .stat-value.in { color: var(--income); }
  .stat-value.out { color: var(--expense); }
  .stat-label, .totals .label, .planning-result .label, .dash-kicker {
    color: var(--muted);
  }

  /* The canonical stat containers. .planning-results and
     .tax-results-grid are aliased onto .stat-grid rather than kept as
     separate rules — the Tax Planning name was always wrong for
     something Financial Insights also used. */
  .stat-grid, .planning-results {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1px; background: var(--line);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .stat-cell, .planning-result {
    display: flex; justify-content: space-between; gap: var(--sp-3);
    padding: var(--sp-3); background: var(--surface);
    font-size: var(--fs-base);
  }
  /* Lead figures: the summary strips at the top of Invoices, Review and
     Vendors. The number leads at the display size the scale already owns,
     with its label stacked above it. Cells stay wide enough for a
     ten-character money figure, and a longer one wraps rather than clips. */
  .stat-grid-lead {
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: var(--sp-3); background: transparent; border: 0; overflow: visible;
  }
  .stat-grid-lead .stat-cell {
    flex-direction: column; justify-content: flex-start; gap: var(--sp-2);
    padding: var(--sp-4) var(--sp-5);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
  }
  .stat-grid-lead .stat-label { font-size: var(--fs-sm); font-weight: 600; }
  .stat-grid-lead .stat-value {
    font-size: var(--fs-2xl); min-width: 0; overflow-wrap: anywhere;
  }
  .stat-grid-lead .stat-value .dash-sub {
    font-size: var(--fs-sm); font-weight: 400; letter-spacing: 0; line-height: 1.4;
  }
  /* On a phone the lead figures stack, one per row, at the smaller size.
     The doubled class beats the generic .stat-grid phone rule further down. */
  @media (max-width: 640px) {
    .stat-grid.stat-grid-lead { grid-template-columns: minmax(0, 1fr); }
    .stat-grid-lead .stat-value { font-size: var(--fs-xl); }
  }
  .totals {
    display: flex; gap: var(--sp-6); flex-wrap: wrap;
    margin-bottom: var(--sp-1);
  }

  /* One sub-navigation idiom for switching between sibling views.
     Replaces five: _ledger_nav, _tax_nav and _insights_nav each built
     their own <nav> of links, .log-tabs rendered .btn-link pills, and
     _tools_sibling_nav rendered a middot-separated sentence inside a
     <p class="hint">. They all did the same job and looked like four
     different things.

     The sixth idiom, .range-toggle, is deliberately NOT merged here —
     see .segmented below. */
  .subnav {
    display: flex; gap: 2px; margin-bottom: var(--sp-5);
    border-bottom: 1px solid var(--line);
    overflow-x: auto; overflow-y: hidden; white-space: nowrap;
    scrollbar-width: none;
  }
  .subnav::-webkit-scrollbar { display: none; }
  .subnav a {
    padding: var(--sp-2) var(--sp-3); text-decoration: none;
    color: var(--muted); font-size: var(--fs-base); font-weight: 500;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color var(--t-state), border-color var(--t-state);
  }
  .subnav a:hover { color: var(--ink); }
  .subnav a.active {
    color: var(--ink); font-weight: 600; border-bottom-color: var(--brand);
  }
  /* A count beside a tab label. Neutral by default; .bad keeps the
     signal the old .log-badge carried, so an error count still reads as
     a problem rather than as decoration. */
  .subnav .count {
    margin-left: var(--sp-1); color: var(--faint);
    font-variant-numeric: tabular-nums;
  }
  .subnav .count.bad {
    padding: 1px 7px; border-radius: 999px;
    background: var(--error-bg); border: 1px solid var(--error-line);
    color: var(--expense); font-size: var(--fs-xs);
  }
  .subnav a.active .count { color: var(--muted); }
  .subnav a.active .count.bad { color: var(--expense); }

  /* Segmented control — filtering or toggling WITHIN one view, which is
     a different job from navigating between views and should not look
     identical to it. The spec called for collapsing six idioms to one;
     on inspection the honest answer is two, because a trend-window
     picker and a set of view tabs mean different things and flattening
     them would teach the user the wrong model. */
  .segmented, .range-toggle {
    display: inline-flex; gap: 2px; flex-wrap: wrap;
    margin-bottom: var(--sp-4); padding: 3px;
    background: var(--raised); border: 1px solid var(--line);
    border-radius: 999px;
  }
  .segmented a, .range-toggle .btn-link {
    padding: var(--sp-1) var(--sp-3); border-radius: 999px;
    font-size: var(--fs-sm); font-weight: 500; text-decoration: none;
    color: var(--muted); border: 1px solid transparent; background: none;
    transition: background-color var(--t-state), color var(--t-state);
  }
  .segmented a:hover, .range-toggle .btn-link:hover {
    color: var(--ink); background: var(--hover-fill); border-color: transparent;
  }
  .segmented a[aria-selected="true"],
  .range-toggle .btn-link.primary {
    background: var(--surface); color: var(--ink); font-weight: 600;
    border-color: var(--line-strong); box-shadow: none;
  }

  /* Layout utilities. These exist to retire 91 inline style= attributes
     in app.py, which were the one place spacing decisions were being
     made outside the design system — hardcoded 280px widths, a
     seven-property flex recipe pasted seven times, margin-top:32px.
     Deliberately a short, named, tokenised set rather than an
     open-ended utility framework: if a value isn't here it belongs in
     a component rule, not a new utility. */
  /* Visually hidden but available to screen readers — used by the
     table sort announcer and by checkbox labels in selection columns. */
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0;
    margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0);
    white-space: nowrap; border: 0;
  }
  .inline-form { display: inline; }
  .grow { flex: 1; min-width: 220px; }
  .field-row {
    display: flex; gap: var(--sp-2); align-items: center;
    flex-wrap: wrap; width: 100%; margin-top: var(--sp-2);
  }
  .field-row.tight { gap: var(--sp-1); flex-wrap: nowrap; margin-top: 0; }
  .mt-2 { margin-top: var(--sp-2); }
  .mt-3 { margin-top: var(--sp-3); }
  .mt-4 { margin-top: var(--sp-4); }
  .mt-6 { margin-top: var(--sp-6); }
  .mb-3 { margin-bottom: var(--sp-3); }
  .mb-4 { margin-bottom: var(--sp-4); }
  .mb-5 { margin-bottom: var(--sp-5); }
  .w-xs { width: 70px; }
  .w-sm { width: 130px; }
  .w-md { width: 160px; }
  .w-lg { width: 280px; }
  .measure { max-width: 420px; }
  .measure-sm { max-width: 320px; }
  .measure-prose { max-width: 62ch; }
  .is-hidden { display: none; }
  .inline-chip {
    display: inline-flex; align-items: center; gap: var(--sp-1);
    margin: 0 var(--sp-4) var(--sp-2) 0;
  }
  .row-inline {
    display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap;
  }
  .row-inline.mt-2 { margin-top: var(--sp-2); }
  .ml-2 { margin-left: var(--sp-2); }
  .pb-2 { padding-bottom: var(--sp-2); }
  .note-warn { color: var(--warn); margin-top: var(--sp-2); max-width: 62ch; }
  .is-warn { border-color: var(--warn-line); background: var(--warn-soft); }
  .tag-lg { padding: 2px var(--sp-3); font-size: var(--fs-sm); }

  /* ---------------------------------------------------------------
     Today region (shape brief: Home)
     --------------------------------------------------------------- */

  /* One dominant region, three balanced concerns, one CTA each. Home
     previously gave nine cards equal weight, so nothing was the next
     action — the audit's "no single next action". This is not a
     one-minute workflow either: it presents review work, cash health
     and exceptions side by side and lets the user pick, which is what
     the brief asks for over a forced funnel. */
  .today { margin-bottom: var(--sp-6); }
  .today-grid {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--sp-4);
  }
  /* Three cards need about 220px each; below that they stack. */
  @media (max-width: 900px) {
    .today-grid { grid-template-columns: minmax(0, 1fr); }
  }
  .today-card {
    display: flex; flex-direction: column; gap: var(--sp-2);
    padding: var(--sp-5); border-radius: var(--radius);
    background: var(--surface); border: 1px solid var(--line);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    min-width: 0;
  }
  /* The one card carrying work gets the emphasis; the other two are
     context. Equal weight everywhere is what caused the problem. */
  .today-card.is-primary { border-color: var(--brand-line); background: var(--brand-soft); }
  .today-card .kicker, .source-briefing-kicker {
    font-size: var(--fs-xs); font-weight: 600; color: var(--faint);
  }
  .today-card .figure {
    font-size: var(--fs-xl); font-weight: 600; letter-spacing: -0.03em;
    line-height: 1.1; font-variant-numeric: tabular-nums lining-nums;
  }
  .today-card .figure.calm { color: var(--muted); }
  .today-card .detail { color: var(--muted); font-size: var(--fs-base); }
  .today-card .cta { margin-top: auto; padding-top: var(--sp-3); }
  .today-card .freshness {
    color: var(--faint); font-size: var(--fs-xs);
    font-variant-numeric: tabular-nums;
  }
  .today-card .freshness.stale { color: var(--warn); }
  /* All-clear: the completion state the audit says is missing. */
  .today-clear {
    display: flex; align-items: baseline; gap: var(--sp-3);
    padding: var(--sp-5); border-radius: var(--radius);
    background: var(--surface); border: 1px solid var(--line);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  .today-clear .mark { color: var(--income); font-size: var(--fs-lg); }
  .today-clear .headline { font-size: var(--fs-md); font-weight: 600; }
  .today-clear .detail { color: var(--muted); }

  /* ---------------------------------------------------------------
     Connection briefing (Feature A)
     --------------------------------------------------------------- */

  /* A sibling to Today, not another Today card. Operational exceptions
     stay in Needs attention; this region is the wider view across the
     owner's source dashboards. */
  .source-briefing {
    margin-bottom: var(--sp-6); padding: var(--pad);
    border-radius: var(--radius);
    background: var(--surface);
    border: 1px solid var(--line);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  .source-briefing-head {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: var(--sp-4); margin-bottom: var(--sp-4);
  }
  .source-briefing-kicker {
    display: block; margin-bottom: var(--sp-1);
  }
  .source-briefing h2 { font-size: var(--fs-lg); font-weight: 600; }
  .source-briefing-scope {
    color: var(--faint); font-size: var(--fs-xs); white-space: nowrap;
  }
  .source-signal-list { list-style: none; display: grid; gap: var(--sp-2); }
  .source-signal {
    display: grid; grid-template-columns: 88px minmax(0, 1fr) auto;
    align-items: center; gap: var(--sp-4); padding: var(--pad-compact);
    color: var(--ink); text-decoration: none;
    background: var(--raised); border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    transition: border-color var(--t-state), background-color var(--t-state);
  }
  .source-signal:hover { border-color: var(--line-strong); background: var(--hover-fill); }
  .source-signal.urgent {
    border-color: var(--warn-line); background: var(--warn-soft);
  }
  .source-signal.urgent:hover { border-color: var(--warn); background: var(--warn-soft); }
  .source-signal-tier {
    color: var(--muted); font-size: var(--fs-xs); font-weight: 600;
  }
  .source-signal.urgent .source-signal-tier { color: var(--warn); }
  .source-signal-copy { font-size: var(--fs-md); line-height: 1.45; }
  .source-signal.informational .source-signal-copy { color: var(--muted); }
  .source-signal-open {
    color: var(--faint); font-size: var(--fs-xs); white-space: nowrap;
  }
  details.source-signal-context {
    margin-top: var(--sp-3); padding-top: var(--sp-3);
    border-top: 1px solid var(--line);
  }
  details.source-signal-context > summary {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--sp-3); padding: var(--sp-3) var(--sp-4);
    color: var(--muted); cursor: pointer; list-style: none;
    font-size: var(--fs-base); border-radius: var(--radius-sm);
  }
  details.source-signal-context > summary::-webkit-details-marker { display: none; }
  details.source-signal-context > summary:hover { color: var(--ink); background: var(--hover-fill); }
  details.source-signal-context > .source-signal-list { margin-top: var(--sp-2); }
  .source-signal-toggle { color: var(--faint); font-size: var(--fs-xs); }
  .source-signal-toggle::before { content: "Show"; }
  details.source-signal-context[open] .source-signal-toggle::before { content: "Hide"; }
  .source-briefing-empty {
    display: flex; align-items: baseline; gap: var(--sp-3);
    padding: var(--pad-compact); color: var(--muted);
  }
  .source-briefing-empty strong {
    display: block; color: var(--ink); font-size: var(--fs-md); font-weight: 600;
  }
  .source-briefing-empty span:not(.source-briefing-mark) { display: block; }
  .source-briefing-empty .btn-link { display: inline-block; margin-top: var(--sp-2); }
  .source-briefing-mark { color: var(--income); font-size: var(--fs-lg); }

  /* ---------------------------------------------------------------
     Shared Insights metric panel (Feature B)
     --------------------------------------------------------------- */

  /* One section, not a nest of metric cards. The one-pixel grid gaps
     separate scan columns while every metric remains a single source link. */
  .home-insights {
    margin-bottom: var(--sp-6); overflow: hidden;
    border-radius: var(--radius); background: var(--surface);
    border: 1px solid var(--line);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  .insight-metrics-head {
    display: flex; align-items: flex-end; justify-content: space-between;
    gap: var(--sp-4); padding: var(--pad);
  }
  .insight-metrics-head h2 { font-size: var(--fs-lg); font-weight: 600; }
  .insight-metrics-head p {
    margin-top: var(--sp-1); color: var(--muted); font-size: var(--fs-sm);
  }
  .insight-metrics-action {
    color: var(--accent); font-size: var(--fs-sm); white-space: nowrap;
  }
  .insight-metric-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 0; background: var(--surface); border-top: 1px solid var(--line);
  }
  .insight-metric {
    display: grid; grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--sp-1) var(--sp-3); min-width: 0; padding: var(--sp-5);
    color: var(--ink); text-decoration: none; background: var(--surface);
    box-shadow: inset -1px -1px 0 var(--line);
    transition: background-color var(--t-state);
  }
  .insight-metric:hover { background: var(--hover-fill); }
  .insight-metric-source {
    color: var(--faint); font-size: var(--fs-xs); font-weight: 600;
  }
  .insight-metric-label {
    grid-column: 1 / -1; min-width: 0; overflow-wrap: anywhere;
    color: var(--muted); font-size: var(--fs-base); line-height: 1.4;
  }
  .insight-metric-value {
    grid-column: 1 / -1; margin-top: var(--sp-2);
    font-size: var(--fs-lg); font-weight: 600; line-height: 1.15;
    letter-spacing: -0.02em; overflow-wrap: anywhere;
  }
  .insight-metric-delta {
    grid-column: 1 / -1; color: var(--faint); font-size: var(--fs-xs);
    line-height: 1.45; overflow-wrap: anywhere;
  }
  .insight-metric-delta .mono { font-size: inherit; }
  .insight-metric-open {
    grid-column: 1 / -1; margin-top: var(--sp-3);
    color: var(--faint); font-size: var(--fs-xs);
  }
  .insight-metrics-empty {
    display: flex; align-items: baseline; gap: var(--sp-3); flex-wrap: wrap;
    padding: var(--pad); border-top: 1px solid var(--line);
    color: var(--muted); font-size: var(--fs-base);
  }
  .insight-metrics-empty strong { color: var(--ink); font-weight: 600; }
  .insight-metrics-empty a { color: var(--accent); }

  /* ---------------------------------------------------------------
     Review drawer (shape brief: Review)
     --------------------------------------------------------------- */

  /* A drawer rather than a modal, so the queue stays visible while one
     item is decided — which matters when the decision is "is this the
     same as the row above it". */
  .drawer {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(460px, 92vw);
    z-index: 70; display: flex; flex-direction: column;
    background: var(--overlay); border-left: 1px solid var(--line-strong);
    box-shadow: -24px 0 64px rgba(0, 0, 0, 0.45);
    transform: translateX(100%);
    transition: transform var(--t-overlay) var(--ease);
  }
  /* The hidden attribute does the show/hide; transform does the motion.
     These were previously both handled by a transitioned `visibility`,
     which stayed computed as hidden for the length of the transition —
     long enough that the contents could not take focus, so opening the
     drawer left focus stranded on the trigger. */
  .drawer[hidden] { display: none; }
  .drawer.is-open { transform: none; }
  .drawer-scrim {
    position: fixed; inset: 0; z-index: 69; background: rgba(0, 0, 0, 0.45);
    opacity: 0; pointer-events: none;
    transition: opacity var(--t-overlay);
  }
  .drawer-scrim.is-open { opacity: 1; pointer-events: auto; }
  .drawer-head {
    display: flex; align-items: flex-start; gap: var(--sp-3);
    padding: var(--sp-5); border-bottom: 1px solid var(--line);
  }
  .drawer-head .who { font-size: var(--fs-md); font-weight: 600; min-width: 0; }
  .drawer-head .meta { color: var(--muted); font-size: var(--fs-sm); margin-top: 2px; }
  .drawer-close {
    margin-left: auto; background: none; border: 1px solid var(--line-strong);
    color: var(--muted); border-radius: 999px; padding: 4px 10px;
    font-size: var(--fs-sm);
  }
  .drawer-body { padding: var(--sp-5); overflow-y: auto; flex: 1; }
  .drawer-amount {
    font-size: var(--fs-xl); font-weight: 600; letter-spacing: -0.03em;
    font-variant-numeric: tabular-nums lining-nums; margin-bottom: var(--sp-4);
  }
  .drawer-field { margin-bottom: var(--sp-4); }
  .drawer-field > label {
    display: block; margin-bottom: var(--sp-2);
    font-size: var(--fs-xs); font-weight: 600; color: var(--faint);
  }
  .drawer-field select, .drawer-field input[type="text"] { width: 100%; }

  /* Approve is primary and alone. Deny is pushed to the opposite edge,
     tertiary, behind its own confirmation — spatially separated so a
     misclick cannot land on it. Note the weighting is inverted from the
     usual: Approve is the irreversible action here (it posts to the ledger and
     cannot be retracted), Deny only changes local status. */
  .drawer-foot {
    display: flex; align-items: center; gap: var(--sp-3);
    padding: var(--sp-4) var(--sp-5); border-top: 1px solid var(--line);
    background: var(--surface);
  }
  .drawer-foot .approve { flex: 1; justify-content: center; }
  .drawer-foot .deny-slot { margin-left: auto; }
  .drawer-foot .deny-slot button {
    background: none; border: 1px solid transparent; color: var(--muted);
    font-weight: 500;
  }
  .drawer-foot .deny-slot button:hover {
    color: var(--expense); border-color: var(--error-line);
    background: var(--error-bg);
  }

  /* The detail panels park here between openings. They must stay in the
     document rather than being removed, because their inputs carry
     form="approve-<id>" and still contribute to bulk submits while
     parked — hidden, not detached. */
  .review-panels { display: none; }
  .review-detail { display: flex; flex-direction: column; min-height: 0; }
  .review-detail.is-hidden { display: none; }
  .drawer .review-detail { flex: 1; overflow: hidden; }

  /* Row summary: one primary action, information not form controls. */
  td.review-cat .tag { max-width: 100%; }
  tr.is-open-in-drawer { background: var(--brand-soft); }
  tr.is-open-in-drawer:hover { background: var(--brand-soft); }

  /* ---------------------------------------------------------------
     Disclosure (redesign Phase 7)
     --------------------------------------------------------------- */

  /* Evidence, available but not shouting. Reconcile carried eleven
     always-rendered sections — a verdict followed by several hundred
     words of supporting prose and four tables, every one of them on
     screen whether or not anything was wrong. The verdict is the
     answer; the rest is why. Native <details>, so no JavaScript and
     the browser handles keyboard and find-in-page. */
  details.disclosure {
    border: 1px solid var(--line); border-radius: var(--radius);
    background: var(--surface); margin-bottom: var(--gap-block);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }
  details.disclosure > summary {
    display: flex; align-items: baseline; gap: var(--sp-3);
    padding: var(--pad); cursor: pointer; list-style: none;
    font-size: var(--fs-md); font-weight: 600; color: var(--ink);
  }
  details.disclosure > summary::-webkit-details-marker { display: none; }
  details.disclosure > summary:hover { background: var(--hover-fill); }
  details.disclosure > summary .why {
    margin-left: auto; color: var(--faint); font-size: var(--fs-xs);
    font-weight: 400;
  }
  details.disclosure[open] > summary { border-bottom: 1px solid var(--line); }
  details.disclosure[open] > summary .why::after { content: " \\2013 hide"; }
  details.disclosure:not([open]) > summary .why::after { content: " \\2013 show"; }
  .disclosure-body { padding: var(--pad); }
  /* A collapsed integration is a quiet one-liner, not a card. */
  .disclosure.conn-collapsed { margin-bottom: var(--sp-2); }
  .disclosure.conn-collapsed > summary {
    padding: var(--pad-compact); font-weight: 500;
  }
  .disclosure.conn-collapsed > summary .conn-name { color: var(--muted); }
  .disclosure.conn-collapsed[open] > summary .conn-name { color: var(--ink); }
  .disclosure-body > .section { border: none; box-shadow: none;
    padding: 0; margin: 0; background: none; }

  /* Back-link above a detail page's first section. */
  .subnav-back a {
    color: var(--muted); text-decoration: none; font-size: var(--fs-base);
  }
  .subnav-back a:hover { color: var(--ink); }

  /* ---------------------------------------------------------------
     Home briefing (redesign Phase 6)
     --------------------------------------------------------------- */

  /* The one place the app speaks in sentences before it shows numbers.
     Instrument Serif at display size, because this is the document
     register: a statement about the business rather than a control you
     operate. It replaced nine tiles competing for first place and a
     stack of up to three banners above them.

     Everything here is derived from data the app already had — the
     pending queue, the reconciliation snapshot, month totals, the
     ledger's skip records, the assistant's daily note. Nothing new is
     computed and nothing is invented; the briefing is an ordering of
     facts, not a generated opinion. */
  .briefing { margin-bottom: var(--sp-6); }
  .briefing-lead {
    font-family: var(--font-ui); font-size: var(--fs-2xl);
    font-weight: 600; line-height: 1.1; letter-spacing: -0.03em;
    color: var(--ink); max-width: 24ch; text-wrap: balance;
  }
  /* The second sentence is quieter by tone only: one face, one weight,
     no second color. */
  .briefing-lead .quiet { color: var(--muted); }
  .briefing-points {
    list-style: none; margin-top: var(--sp-5);
    display: grid; gap: var(--sp-3); max-width: 62ch;
  }
  .briefing-points li {
    display: flex; gap: var(--sp-3); align-items: baseline;
    font-size: var(--fs-md); color: var(--muted); line-height: 1.45;
  }
  .briefing-points .marker {
    color: var(--faint); flex: 0 0 auto; font-size: var(--fs-xs);
    transform: translateY(-2px);
  }
  .briefing-points li.urgent .marker { color: var(--warn); }
  .briefing-points strong {
    color: var(--ink); font-weight: 600;
    font-variant-numeric: tabular-nums lining-nums;
  }
  .briefing-points a { color: var(--ink); text-decoration: none;
    border-bottom: 1px solid var(--line-strong); }
  .briefing-points a:hover { border-bottom-color: var(--brand); }

  /* One orchestrated moment on the whole app: the briefing arrives in
     sequence on load. Nothing else on the page moves. */
  @media (prefers-reduced-motion: no-preference) {
    .briefing-lead, .briefing-points li {
      animation: kl-rise 420ms var(--ease) both;
    }
    .briefing-points li:nth-child(1) { animation-delay: 60ms; }
    .briefing-points li:nth-child(2) { animation-delay: 110ms; }
    .briefing-points li:nth-child(3) { animation-delay: 160ms; }
    .briefing-points li:nth-child(4) { animation-delay: 210ms; }
    .briefing-points li:nth-child(n+5) { animation-delay: 260ms; }
  }
  @keyframes kl-rise {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: none; }
  }

  /* ---------------------------------------------------------------
     Sidebar groups, badges, palette trigger (redesign Phase 4)
     --------------------------------------------------------------- */

  .nav-group {
    padding: var(--sp-4) 12px var(--sp-1);
    font-size: var(--fs-xs); font-weight: 600; color: var(--faint);
  }
  .nav-badge {
    margin-left: auto; padding: 1px 7px; border-radius: 999px;
    background: var(--brand-soft); border: 1px solid var(--brand-line);
    color: var(--brand-ink); font-size: var(--fs-2xs); font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .meta > span + span { margin-left: var(--sp-2); }
  /* Demo mode banner, shown above every page while a demo is open. */
  .demo-banner {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--sp-3); flex-wrap: wrap; margin-bottom: var(--sp-4);
    padding: var(--sp-3) var(--sp-4); border-radius: var(--radius-sm);
    background: var(--warn-soft); border: 1px solid var(--warn-line);
    color: var(--ink); font-size: var(--fs-base);
  }

  /* Invoice progress: Create, Send, Get paid. Read-only status. */
  .progress-steps {
    list-style: none; margin: 0 0 var(--gap-section); padding: 0;
    display: grid; gap: var(--sp-3);
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  @media (max-width: 900px) {
    .progress-steps { grid-template-columns: minmax(0, 1fr); }
  }
  .progress-step {
    display: grid; grid-template-columns: 28px minmax(0, 1fr);
    gap: var(--sp-3); align-items: start; padding: var(--pad-compact);
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }
  .step-mark {
    width: 28px; height: 28px; border-radius: 999px; display: grid;
    place-items: center; border: 1px solid var(--line-strong);
    color: var(--muted); font-size: var(--fs-sm); font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .progress-step.done .step-mark {
    background: var(--brand-soft); border-color: var(--brand-line);
    color: var(--brand-ink);
  }
  .progress-step[aria-current="step"] .step-mark { border-color: var(--ink); color: var(--ink); }
  .step-title { font-size: var(--fs-md); font-weight: 600; }
  .step-detail { color: var(--muted); font-size: var(--fs-base); }

  /* Pipeline board: one column per stage, scrolls sideways when wide. */
  .board {
    display: grid; grid-auto-flow: column; grid-auto-columns: minmax(220px, 1fr);
    gap: var(--sp-3); overflow-x: auto; padding-bottom: var(--sp-2);
  }
  .board-col {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: var(--sp-3); min-width: 0;
    display: flex; flex-direction: column; gap: var(--sp-3);
  }
  .board-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
  .board-head h2 { font-size: var(--fs-md); font-weight: 600; }
  .board-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
  .board-card {
    background: var(--raised); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: var(--sp-3);
    display: flex; flex-direction: column; gap: var(--sp-1);
  }
  .board-card a { font-weight: 600; overflow-wrap: anywhere; }
  .board-amount { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }

  /* Record detail: the work on the left, who it is about on the right. */
  .detail-grid {
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 300px);
    gap: var(--gap-section); align-items: start;
  }
  .detail-main, .detail-side { min-width: 0; }
  .detail-tags { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
  @media (max-width: 900px) {
    .detail-grid { grid-template-columns: minmax(0, 1fr); }
  }

  /* The header "New" menu: one native <details>, no script needed to open
     it. table.js only closes it on an outside click or Escape. */
  .new-menu { position: relative; }
  .new-menu > summary {
    list-style: none; display: inline-flex; align-items: center;
    gap: var(--sp-2); padding: 9px 16px; border-radius: 999px;
    background: var(--action); color: var(--action-ink);
    font-size: var(--fs-base); font-weight: 600; cursor: pointer;
    transition: background-color var(--t-state), box-shadow var(--t-state);
  }
  .new-menu > summary::-webkit-details-marker { display: none; }
  .new-menu > summary:hover { background: var(--white); }
  .new-menu > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .new-menu-list {
    position: absolute; right: 0; top: calc(100% + var(--sp-2)); z-index: 60;
    min-width: 220px; padding: var(--sp-2);
    background: var(--overlay); border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm); box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    display: flex; flex-direction: column; gap: 2px;
  }
  .new-menu-list a {
    padding: 9px 12px; border-radius: var(--radius-sm); color: var(--ink);
    text-decoration: none; font-size: var(--fs-base); font-weight: 500;
  }
  .new-menu-list a:hover, .new-menu-list a:focus-visible {
    background: var(--hover-fill); outline: none;
  }

  .nav-search {
    display: flex; align-items: center; gap: 10px; width: 100%;
    margin: 0 0 var(--sp-2); padding: 9px 12px;
    background: var(--raised); border: 1px solid var(--line);
    border-radius: var(--radius-sm); color: var(--muted);
    font-size: var(--fs-base); font-weight: 500; text-align: left;
    cursor: pointer; transition: border-color var(--t-state), color var(--t-state);
  }
  .nav-search:hover { border-color: var(--line-strong); color: var(--ink); }
  .nav-kbd {
    margin-left: auto; padding: 1px 6px; border-radius: 5px;
    background: var(--surface); border: 1px solid var(--line-strong);
    color: var(--faint); font-size: var(--fs-2xs);
    font-family: var(--font-mono); white-space: nowrap;
  }

  /* ---------------------------------------------------------------
     Command palette (redesign Phase 4)
     --------------------------------------------------------------- */

  /* The thing that makes a twelve-item sidebar honest. Every one of the
     app's ~48 views and a set of verbs live here, so shortening the rail
     moved things out of the way rather than out of reach. */
  dialog.palette {
    position: fixed; top: 12vh; left: 50%; transform: translateX(-50%);
    width: min(560px, calc(100vw - 2 * var(--sp-5)));
    max-height: 62vh; padding: 0; overflow: hidden;
    background: var(--overlay); color: var(--ink);
    border: 1px solid var(--line-strong); border-radius: var(--radius);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.55);
  }
  dialog.palette::backdrop { background: rgba(0, 0, 0, 0.55); }
  .palette-input {
    width: 100%; padding: var(--sp-4) var(--sp-5);
    background: none; border: none; border-bottom: 1px solid var(--line);
    border-radius: 0; color: var(--ink); font-size: var(--fs-md);
  }
  .palette-input:focus { outline: none; border-color: var(--line); }
  .palette-list { max-height: 46vh; overflow-y: auto; padding: var(--sp-2); }
  .palette-group {
    padding: var(--sp-3) var(--sp-3) var(--sp-1);
    font-size: var(--fs-xs); font-weight: 600; color: var(--faint);
  }
  .palette-item {
    display: flex; align-items: center; gap: var(--sp-3);
    padding: var(--sp-2) var(--sp-3); border-radius: var(--radius-sm);
    cursor: pointer; color: var(--ink); font-size: var(--fs-base);
  }
  .palette-item .where { margin-left: auto; color: var(--faint); font-size: var(--fs-xs); }
  .palette-item[aria-selected="true"] { background: var(--hover-fill); }
  .palette-item[aria-selected="true"] .where { color: var(--muted); }
  .palette-empty { padding: var(--sp-5); color: var(--muted); text-align: center; }
  .palette-foot {
    display: flex; gap: var(--sp-4); padding: var(--sp-2) var(--sp-5);
    border-top: 1px solid var(--line); color: var(--faint);
    font-size: var(--fs-2xs);
  }

  /* ---------------------------------------------------------------
     Header search chip (replaces the sidebar's nav-search on every
     width — a labelled, always-visible palette trigger rather than one
     that only existed inside a sidebar that itself disappears on
     mobile)
     --------------------------------------------------------------- */

  .header-search {
    width: auto; margin: 0; flex-shrink: 0;
  }

  /* ---------------------------------------------------------------
     Mobile bottom navigation + More sheet (redesign follow-up)

     Replaces collapsing the sidebar to an icon-only rail below 640px.
     Every destination keeps a real label at every width — an icon with
     no label is the exact mistake the grouped desktop rail was built to
     fix, and it does not stop being a mistake just because the window
     got narrower. Four daily-use destinations (Home, Review, Summary,
     Ledger — MOBILE_PINNED_TABS in ui.py) get a permanent, labelled tab;
     everything else in NAV_GROUPS lives in the labelled More sheet,
     grouped exactly as the desktop sidebar groups it. One data source,
     two renderings.
     --------------------------------------------------------------- */

  /* Desktop: the sidebar is the primary nav, this stays off. */
  .bottom-nav { display: none; }
  .bottom-nav-more {
    background: none; border: none; font-family: inherit; cursor: pointer;
  }

  @media (max-width: 640px) {
    .bottom-nav {
      display: flex; position: fixed; left: 0; right: 0; bottom: 0;
      /* Above the bulk-bar (60) and the drawer/scrim (69-70) — this is
         permanent chrome, not a transient overlay, so it sits just
         under them rather than fighting for the same layer. */
      z-index: 65;
      background: var(--overlay); border-top: 1px solid var(--line-strong);
      padding: var(--sp-1) var(--sp-2)
               calc(var(--sp-1) + env(safe-area-inset-bottom));
      /* This is a bare <nav> element, so it also inherits the generic
         `nav { margin-bottom: 28px; border-bottom: ...; gap: 2px;
         overflow-x: auto; white-space: nowrap }` rule meant for the
         old-style top tab bars — reset every property that rule sets
         and this one does not itself need, or the bar detaches from
         the viewport edge instead of sitting flush against it. */
      margin: 0; gap: 0; border-bottom: none; overflow: visible;
      white-space: normal; scrollbar-width: auto;
    }
    .bottom-nav a, .bottom-nav-more {
      flex: 1; display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 2px; min-height: 44px; position: relative;
      padding: var(--sp-1) 0; color: var(--muted); text-decoration: none;
    }
    .bottom-nav a.active { color: var(--ink); font-weight: 600; }
    .bottom-nav-label { font-size: var(--fs-2xs); font-weight: 500; }
    /* Same fix already used once for a badge with no room beside it
       (the old collapsed-rail badge) — it reads as a dot near the icon,
       not a number floating in space. */
    .bottom-nav .nav-badge {
      position: absolute; top: 2px; right: calc(50% - 22px);
      margin: 0; padding: 0 5px;
    }
  }

  /* A bottom sheet, not a centred card — position pinned explicitly
     (like dialog.palette pins itself near the top) rather than relying
     on the default centred dialog layout. */
  dialog.more-sheet {
    position: fixed; left: 0; right: 0; bottom: 0; top: auto;
    width: 100%; max-width: 100%; max-height: 80vh;
    padding: 0; overflow: hidden;
    background: var(--overlay); color: var(--ink);
    border: 1px solid var(--line-strong); border-bottom: none;
    border-radius: var(--radius) var(--radius) 0 0;
    box-shadow: 0 -24px 64px rgba(0, 0, 0, 0.5);
  }
  dialog.more-sheet::backdrop { background: rgba(0, 0, 0, 0.6); }
  .more-sheet-nav {
    max-height: calc(80vh - 64px); overflow-y: auto; overflow-x: visible;
    padding: var(--sp-3) var(--sp-3)
             calc(var(--sp-3) + env(safe-area-inset-bottom));
    /* Same generic bare-<nav> leak as .bottom-nav above — this is also
       a <nav>, and margin-bottom: 28px from that rule would otherwise
       add unwanted trailing space at the end of the scrollable list. */
    margin: 0; white-space: normal;
  }

  /* ---------------------------------------------------------------
     DataTable (redesign Phase 3)
     --------------------------------------------------------------- */

  /* Sortable headers. The app had 26 tables and none of them sorted,
     in a pywebview window that has no browser chrome and therefore no
     Ctrl-F either — so on any table past ~20 rows the user had no tool
     at all. The button inside the th is for keyboard and screen
     readers; aria-sort carries the state. */
  th.sortable { padding: 0; }
  th.sortable > button {
    all: unset; box-sizing: border-box; cursor: pointer;
    display: flex; align-items: center; gap: var(--sp-1);
    width: 100%; padding: 9px 6px;
    font: inherit; color: inherit; letter-spacing: inherit;
    text-transform: inherit;
  }
  th.num.sortable > button { justify-content: flex-end; }
  th.sortable > button:hover { color: var(--ink); }
  th.sortable > button:focus-visible {
    outline: 2px solid var(--brand); outline-offset: -2px;
  }
  /* The arrow holds its space at all times so a column does not shift
     sideways the moment it becomes sorted. */
  .sort-arrow {
    width: 8px; flex: 0 0 8px; opacity: 0;
    transition: opacity var(--t-state);
  }
  th[aria-sort] .sort-arrow { opacity: 1; color: var(--brand); }
  th.sortable:hover .sort-arrow { opacity: 0.45; }

  /* Row hover was previously opt-in via .table-hover and only ~30% of
     tables had it, so scanning behaviour changed screen to screen for
     no reason. It is now the default for every table in a .section,
     and the pointer cursor stays reserved for rows that actually
     navigate (.row-link). */
  .section table tbody tr { transition: background-color var(--t-state); }
  .section table tbody tr:hover { background: var(--raised); }

  /* Selection */
  td.select-cell, th.select-cell { width: 34px; padding-right: 0; }
  tr.is-selected { background: var(--brand-soft); }
  tr.is-selected:hover { background: var(--brand-soft); }

  /* Floating bulk-action bar. Appears only when a selection exists. */
  .bulk-bar {
    position: fixed; left: 50%; bottom: var(--sp-5); z-index: 60;
    transform: translateX(-50%) translateY(8px);
    display: flex; align-items: center; gap: var(--sp-4);
    padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-4);
    background: var(--overlay); color: var(--ink);
    border: 1px solid var(--line-strong); border-radius: 999px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
    opacity: 0; pointer-events: none;
    transition: opacity var(--t-overlay), transform var(--t-overlay);
  }
  .bulk-bar.is-open { opacity: 1; transform: translateX(-50%); pointer-events: auto; }
  .bulk-bar .count { font-weight: 600; font-variant-numeric: tabular-nums; }
  .bulk-bar .sep { width: 1px; height: 20px; background: var(--line-strong); }

  /* ---------------------------------------------------------------
     Dialog (redesign Phase 3)
     --------------------------------------------------------------- */

  /* Replaces window.confirm() at 22 call sites. The native element
     gives focus trapping, Escape, and inertness for free — the reason
     to switch is that a browser confirm cannot be styled, cannot carry
     the app's voice, and in a packaged desktop window looks like the
     app has broken out into the OS. */
  dialog.kl-dialog {
    border: 1px solid var(--line-strong); border-radius: var(--radius);
    background: var(--overlay); color: var(--ink);
    padding: var(--sp-5); max-width: 440px; width: calc(100vw - 2 * var(--sp-5));
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
  }
  dialog.kl-dialog::backdrop { background: rgba(0, 0, 0, 0.6); }
  dialog.kl-dialog h2 {
    font-size: var(--fs-md); font-weight: 600; margin-bottom: var(--sp-2);
  }
  dialog.kl-dialog p {
    color: var(--muted); font-size: var(--fs-base); margin-bottom: var(--sp-5);
  }
  .dialog-actions {
    display: flex; justify-content: flex-end; gap: var(--sp-2);
  }
  @media (prefers-reduced-motion: reduce) {
    .bulk-bar { transition: none; }
    /* The drawer must never depend on a transition completing to become
       usable. A transform transition that stalls leaves the panel parked
       off-screen with focus inside it, which is worse than no animation
       — so when motion is off, the open state is applied instantly. */
    .drawer, .drawer-scrim { transition: none; }
  }

  /* Ledger statements. Both rules are structural rather than decorative:
     a trial balance has to show where it foots, and one level of
     sub-account indent is how the chart of accounts' parent/child
     nesting reads on a statement. Subtotals separate with a rule, not a
     heavier type ramp. */
  tfoot td {
    border-top: 1px solid var(--line-strong); border-bottom: none;
    font-weight: 600;
  }
  td.acct-sub { padding-left: 24px; }
  .mapping-summary {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--sp-3); flex-wrap: wrap; color: var(--muted);
    margin-bottom: var(--sp-4);
  }
  .tax-map-form {
    display: flex; align-items: center; gap: var(--sp-2); min-width: 300px;
  }
  .tax-map-form select { flex: 1; min-width: 0; }
  .tax-map-form button { flex: 0 0 auto; }
  .ledger-account-section .section-head { flex-wrap: wrap; }

  /* Financial statements — Profit & Loss and Balance sheet share this
     grammar. A statement reads as stacked lines with subtotals ruled
     off, not as a data table: its shape is its hierarchy. <details>
     gives native disclosure down to the account level with no
     JavaScript, so the default view stays a handful of lines. */
  .statement { max-width: 620px; }
  .stmt-row, .stmt-group > summary {
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 20px; padding: 10px 2px;
  }
  .stmt-group > summary { cursor: pointer; list-style: none; }
  .stmt-group > summary::-webkit-details-marker { display: none; }
  .stmt-group > summary:hover { background: var(--hover-fill); }
  .stmt-caret { color: var(--faint); font-size: var(--fs-xs); margin-left: 8px; }
  .stmt-group[open] .stmt-caret { color: var(--muted); }
  .stmt-accounts { padding: 0 2px 10px 24px; }
  .stmt-account {
    display: flex; justify-content: space-between; gap: 20px;
    padding: 5px 0; color: var(--muted);
  }
  .stmt-account .code { color: var(--faint); margin-right: 8px; }
  /* Subtotals separate with a rule, never a heavier type ramp. The net
     line takes the double rule an accountant expects under a final
     figure. */
  .stmt-total { border-top: 1px solid var(--line-strong); font-weight: 600; }
  .stmt-total.net { border-top: 3px double var(--line-strong); }
  .stmt-label { color: var(--ink); }

  /* The document register. Instrument Serif appears here and on the
     Home briefing and the accountant-export cover — nowhere else, and
     never in body copy or a table. The rule it encodes: things you
     would hand an accountant are set as documents, things you work in
     are set as tools, and you can tell which is which before reading a
     word. Applied to the statement titles (Profit & Loss, Balance
     sheet, Trial balance) via .doc-title. */
  .doc-title {
    font-family: var(--font-doc); font-size: var(--fs-2xl);
    font-weight: 400; line-height: 1.05; letter-spacing: -0.015em;
    color: var(--ink);
  }
  /* An explanatory line that belongs to the row above it, not to the
     next one — pulled up tight and given prose measure. */
  .stmt-hint { margin: -4px 2px 10px 0; max-width: 60ch; }

  /* Row hover — the long, scannable tables (Vendors, Summary categories,
     Contractors, Nexus, Audit). Background highlight on every row helps
     tracking across columns; the pointer cursor is reserved for rows
     that actually carry a navigational link (.row-link) so a static
     row's hover never promises a click it can't deliver. */
  .table-hover tbody tr:hover { background: var(--raised); transition: background 100ms ease; }
  .table-hover tbody tr.row-link { cursor: pointer; }

  /* Follow-ups / Invoices cards */
  .fu-card {
    border-radius: var(--radius); padding: var(--pad); margin-bottom: var(--gap-block);
  }
  .fu-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .fu-name { font-weight: 600; font-size: var(--fs-md); }
  .fu-meta { color: var(--faint); font-size: var(--fs-sm); }
  /* Invoice overdue urgency: color/weight only, escalating with age.
     1-7 days warns, 8-30 reads as expense red, 31+ turns the whole
     head line red and bold. */
  .fu-meta .overdue-warn { color: var(--warn); }
  .fu-meta .overdue-late { color: var(--expense); }
  .fu-head.overdue-severe .fu-name,
  .fu-head.overdue-severe .fu-meta { color: var(--expense); font-weight: 700; }
  .fu-field { margin-top: 12px; }
  .fu-field label { display: block; margin-bottom: 4px; }
  .fu-subject { width: 100%; }
  .fu-body {
    width: 100%; min-height: 190px; resize: vertical; line-height: 1.5;
  }
  .fu-actions { display: flex; gap: 8px; margin-top: 14px; }

  /* Settings page */
  .field { margin-bottom: 24px; }
  .field label.q {
    display: block; font-weight: 600; font-size: var(--fs-md); margin-bottom: 4px;
  }
  .field input[type="number"] { width: 160px; }
  .prefix-input { display: inline-flex; align-items: center; gap: 6px; }
  .prefix-input .prefix { font-weight: 600; color: var(--muted); }
  .accounts { list-style: none; display: flex; flex-direction: column; gap: 8px; }
  .accounts li {
    display: flex; align-items: center; gap: 10px;
    padding: var(--pad-compact); border-radius: var(--radius-sm);
  }
  /* Wraps the checkbox + name so the whole row is the toggle's hit target,
     not just the 16px box (native <label> click-through). */
  .acct-row { display: flex; align-items: center; gap: 10px; cursor: pointer; flex: 1; min-width: 0; }
  .accounts .acct-name { font-weight: 500; font-size: var(--fs-base); }
  .accounts .acct-bal {
    margin-left: auto; color: var(--faint); font-size: var(--fs-base);
    font-variant-numeric: tabular-nums;
  }
  .insight-widget-fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
  .insight-widget-options .acct-row { align-items: flex-start; }
  .insight-widget-option input { margin-top: 3px; flex: none; }
  .insight-widget-copy {
    display: flex; flex-direction: column; gap: 3px; min-width: 0;
  }
  .insight-widget-meta {
    display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
    color: var(--faint); font-size: var(--fs-sm); line-height: 1.45;
  }
  .insight-widget-source {
    color: var(--muted); font-weight: 600; white-space: nowrap;
  }

  /* Insights */
  .flag {
    border-radius: var(--radius-sm); padding: var(--pad-compact);
    font-size: var(--fs-base); margin-bottom: 10px; border: 1px solid;
  }
  .flag-warn {
    background: var(--error-bg); border-color: var(--error-line);
    color: var(--expense-ink);
  }
  .flag-info {
    background: var(--accent-soft); border-color: var(--accent-line);
    color: var(--accent-ink);
  }
  .snapshot-meta { color: var(--faint); font-size: var(--fs-sm); margin-top: 16px; }

  .mono {
    font-family: var(--font-mono);
    font-size: var(--fs-base);
  }
  .btn-link {
    display: inline-block; text-decoration: none;
    background: transparent; border: 1px solid var(--line-strong); color: var(--ink);
    transition: background-color 0.15s, border-color 0.15s, box-shadow 0.15s;
  }
  .btn-link:hover { border-color: var(--hover-border-strong); background: var(--hover-fill); }
  .btn-link.primary { background: var(--action); border-color: var(--action); color: var(--action-ink); }
  .btn-link.primary:hover { background: var(--white); box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.08); }
  .btn-link[aria-disabled="true"] { opacity: 0.45; pointer-events: none; cursor: not-allowed; }
  .tax-projection-blocked p { max-width: 65ch; }
  .tax-projection-blocked .btn-link { margin-top: var(--sp-3); }
  input[type="file"] { font-size: var(--fs-base); color: var(--muted); }

  /* Setup wizard */
  .steps {
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    margin-bottom: 26px; font-size: var(--fs-sm); color: var(--faint);
  }
  .steps .step {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line-strong);
    background: var(--surface); white-space: nowrap;
  }
  .steps .step.done { border-color: var(--income-line); background: var(--income-soft); color: var(--income); }
  .steps .step.now { border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
  .cred-field { margin-bottom: 18px; }
  .cred-field label.q {
    display: block; font-weight: 600; font-size: var(--fs-md); margin-bottom: 4px;
  }
  .cred-field input[type="text"], .cred-field input[type="password"],
  .cred-field input[type="email"] { width: 100%; }
  .paste-note { color: var(--faint); font-size: var(--fs-sm); margin-top: 5px; }
  .test-row { display: flex; gap: 10px; align-items: center; margin: 14px 0; flex-wrap: wrap; }
  .test-status { font-size: var(--fs-base); max-width: 60ch; }
  .test-status.ok { color: var(--income); }
  .test-status.err { color: var(--expense); }
  .test-status.busy { color: var(--muted); }
  .hint-box {
    background: var(--warn-soft); border: 1px solid var(--warn-line);
    color: var(--warn); border-radius: var(--radius-sm);
    padding: 10px 13px; font-size: var(--fs-base); margin-top: 10px; max-width: 62ch;
  }
  .callout {
    background: var(--warn-soft); border: 1px solid var(--warn-line);
    border-radius: var(--radius-sm); padding: 12px 15px;
    font-size: var(--fs-base); margin: 14px 0; max-width: 66ch;
  }
  .callout.info { background: var(--accent-soft); border-color: var(--accent-line); }
  .callout strong { display: block; margin-bottom: 3px; }
  .wizard-actions { display: flex; gap: 10px; align-items: center; margin-top: 26px; flex-wrap: wrap; }
  .skip-link { color: var(--muted); font-size: var(--fs-base); }
  .checkpoint {
    border: 1px solid var(--line); background: var(--raised);
    border-radius: var(--radius-sm); padding: 10px 14px;
    margin: 16px 0; font-size: var(--fs-base);
  }
  .checkpoint .cp-title { font-weight: 600; margin-bottom: 2px; }
  .checkpoint .cp-verify { color: var(--income); font-size: var(--fs-base); margin-top: 4px; }
  ol.setup-list { margin: 10px 0 10px 22px; font-size: var(--fs-base); }
  ol.setup-list li { margin-bottom: 8px; }
  ol.setup-list a { color: var(--accent-ink); }

  /* Home dashboard: breadth at a glance — a denser grid of tile-cards,
     each one a link to its full tab. */
  .dash-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 14px; margin-bottom: 8px;
  }
  /* A grid item's default min-width is auto, meaning it refuses to
     shrink below its content's min-content width. Several dash cards
     embed a chart with a 640px native SVG width, so without this the
     track inflated to 640px and pushed the page sideways at any window
     under about 700px — invisible while .main-inner was a fixed 900px,
     and immediately visible once it became fluid. */
  .dash-grid > * { min-width: 0; }
  .dash-grid .chart-svg, .dash-grid figure.chart {
    max-width: 100%; overflow-x: auto;
  }
  a.dash-card {
    display: block; border-radius: var(--radius);
    padding: var(--pad); text-decoration: none; color: var(--ink);
    transition: border-color 0.15s, background-color 0.15s;
  }
  a.dash-card:hover { border-color: var(--line-strong); background: var(--dash-card-hover); }
  .dash-kicker {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 8px;
  }
  .dash-kicker .go {
    color: var(--accent); text-transform: none; letter-spacing: 0;
  }
  /* Hero figures use the UI sans with tabular figures, not the mono.
     At 28px a monospace money figure reads as terminal output; the
     sans reads as a designed number, and body's tabular-nums keeps the
     column alignment that made mono worth it in the first place. Mono
     keeps the dense table columns, where its fixed advance actually
     earns its texture. */
  .dash-value {
    font-size: var(--fs-xl); font-weight: 600; line-height: 1.1;
    letter-spacing: -0.03em;
  }
  .dash-value.in { color: var(--income); }
  .dash-value.out { color: var(--expense); }
  /* Monospace suits a pure number/money figure; it looks odd stretched
     across a full phrase like "3 waiting" or "1,240 sessions", where
     the words ride the same fixed-width tracking as the digits. One
     shared escape-hatch rule for every monospace-by-default value
     class (see anti-patterns.md #6) — .totals .value joined this rule
     rather than getting its own copy when Accountant Export needed a
     text date-range inside a .totals block.
     .dash-value left this rule when hero figures moved off the mono
     stack: it is no longer monospace by default, so it has nothing to
     opt out of. It keeps the tighter tracking though, which a phrase
     does not want — hence the letter-spacing reset below. */
  .dash-value.mixed { letter-spacing: normal; }
  .totals .value.mixed {
    font-family: inherit; letter-spacing: normal;
  }
  .dash-row { display: flex; gap: 22px; flex-wrap: wrap; }
  .dash-sub { color: var(--muted); font-size: var(--fs-base); margin-top: 6px; }
  .dash-note { color: var(--muted); font-size: var(--fs-base); }
  .dash-flag { color: var(--warn); font-weight: 600; }
  .dash-ok { color: var(--income); font-weight: 600; }

  /* Optional note captured on a pending card at approval time */
  .notes-input {
    max-width: 320px; width: 100%; font-size: var(--fs-base);
  }

  /* Plain-language commentary on the Summary tab. Deliberately quiet —
     a hairline raised panel, one step off the card surface. */
  .commentary {
    margin-top: 14px; padding: 10px 13px; font-size: var(--fs-base);
    color: var(--muted); background: var(--raised);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm); max-width: 66ch;
  }

  /* Export buttons on the Summary tab */
  .export-actions {
    display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 24px;
  }
  /* 8w / 12w / 12mo trend-window toggle */
  .range-toggle { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
  .range-toggle .btn-link { padding: 5px 14px; font-size: var(--fs-sm); }

  /* First-month cleanup nudge on the dashboard. Its own soft-accent
     treatment, distinct from the warn/error banners — an invitation,
     not an alarm. */
  .nudge {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    justify-content: space-between;
    border: 1px solid var(--accent-line); background: var(--accent-soft);
    color: var(--accent-ink); border-radius: var(--radius-sm);
    padding: 12px 16px; margin-bottom: 20px;
  }
  .nudge-text { font-size: var(--fs-base); max-width: 62ch; }
  .nudge-actions { display: flex; gap: 8px; align-items: center; }

  /* ------------------------------------------------------------
     Charts, v2. Hand-rolled SVG (charts.py); fills reference the
     gradient defs the shell renders once per page (ids kl-*), and
     every fill declares a solid fallback color, so nothing goes
     invisible if the defs are missing. Data ink is loud, chrome is
     recessive; every mark keeps a direct label or <title>, so color
     never carries meaning alone.
     ------------------------------------------------------------ */
  figure.chart { margin: 4px 0 6px; }
  .chart-svg {
    width: 100%; height: auto; display: block;
    font-family: var(--font-ui);
  }
  .chart-cap { color: var(--faint); font-size: var(--fs-sm); margin-top: 6px; }
  /* line / area */
  .chart-line {
    fill: none; stroke: var(--accent); stroke-width: 2.5;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .chart-line.draw {
    stroke-dasharray: 1; stroke-dashoffset: 0;
    animation: kl-draw 1s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .chart-area {
    fill: url(#kl-area-green) rgba(74, 222, 128, 0.14); stroke: none;
    animation: kl-fade 0.9s ease-out 0.15s both;
  }
  .chart-dot { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
  .chart-axis { stroke: var(--line-strong); stroke-width: 1; }
  .chart-grid { stroke: var(--line); stroke-width: 1; }
  .chart-tick { fill: var(--faint); font-size: var(--fs-xs); }
  /* horizontal bars */
  .chart-track { fill: rgba(255, 255, 255, 0.04); }
  .chart-bar {
    fill: url(#kl-bar-h) var(--accent);
    transform-box: fill-box; transform-origin: left center;
    animation: kl-grow-x 0.7s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .chart-bar.warn { fill: url(#kl-bar-h-warn) var(--warn); }
  .chart-bar.neutral { fill: url(#kl-bar-h-neutral) #D4D4D8; }
  .chart-label { fill: var(--muted); font-size: var(--fs-sm); }
  .chart-value {
    fill: var(--ink); font-size: var(--fs-sm); font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .chart-value.dim { fill: var(--faint); font-weight: 500; }
  .chart-value.warn { fill: var(--warn); }
  /* stat-tile mini column charts (dash-card pill bars) */
  .mini-chart { display: block; width: 100%; max-width: 190px; height: 44px; margin-top: 12px; }
  .chart-minibar {
    fill: url(#kl-bar-v) var(--accent);
    transform-box: fill-box; transform-origin: center bottom;
    animation: kl-grow-y 0.6s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .chart-minibar.dim { fill: rgba(74, 222, 128, 0.38); animation: none; }
  /* funnel — a three-step ordinal green ramp; label ink flips by
     step luminance so contrast always clears (verified AA on all
     three fills; re-verify if a step or color is ever added). */
  .chart-funnel-1 { fill: var(--accent); }
  .chart-funnel-2 { fill: #2FB566; }
  .chart-funnel-3 { fill: #177240; }
  .chart-funnel-label { fill: #08090B; font-size: var(--fs-sm); font-weight: 700; }
  .chart-funnel-sub { fill: rgba(8, 9, 11, 0.72); font-size: var(--fs-xs); font-weight: 600; }
  .chart-funnel-label.on-dark { fill: var(--white); }
  .chart-funnel-sub.on-dark { fill: rgba(255, 255, 255, 0.82); }
  /* gauge */
  .chart-gauge-fill.ok {
    fill: url(#kl-bar-h) var(--accent);
    transform-box: fill-box; transform-origin: left center;
    animation: kl-grow-x 0.7s cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .chart-gauge-fill.warn { fill: url(#kl-bar-h-warn) var(--warn); }
  .chart-gauge-tick { stroke: var(--ink); stroke-width: 1.5; }
  /* two small charts side by side (e.g. sessions + users sparklines) */
  .chart-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .chart-pair > div { min-width: 0; }
  .chart-title { margin-bottom: 4px; }
  @media (max-width: 640px) { .chart-pair { grid-template-columns: 1fr; } }

  @keyframes kl-draw { from { stroke-dashoffset: 1; } }
  @keyframes kl-fade { from { opacity: 0; } }
  @keyframes kl-grow-x { from { transform: scaleX(0); } }
  @keyframes kl-grow-y { from { transform: scaleY(0); } }

  /* Business health score card */
  .health-card {
    display: flex; gap: 22px; align-items: center; flex-wrap: wrap;
    border-radius: var(--radius); padding: var(--pad); margin-bottom: var(--gap-block);
  }
  .health-grade {
    width: 88px; height: 88px; flex-shrink: 0; border-radius: 50%;
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; border: 3px solid var(--line-strong);
    background: var(--raised);
  }
  .health-grade .g { font-size: 2.1rem; font-weight: 800; line-height: 1; }
  .health-grade .s {
    font-size: var(--fs-xs); color: var(--faint); margin-top: 3px;
    font-variant-numeric: tabular-nums;
  }
  .health-grade.good { border-color: var(--income); box-shadow: 0 0 24px rgba(74, 222, 128, 0.18); }
  .health-grade.good .g { color: var(--income); }
  .health-grade.warn { border-color: var(--warn); box-shadow: 0 0 24px rgba(251, 191, 36, 0.15); }
  .health-grade.warn .g { color: var(--warn); }
  .health-grade.bad { border-color: var(--expense); box-shadow: 0 0 24px rgba(248, 113, 113, 0.15); }
  .health-grade.bad .g { color: var(--expense); }
  .health-body { flex: 1; min-width: 260px; }
  .health-summary { font-size: var(--fs-md); margin-bottom: 12px; }
  .health-summary strong { color: var(--ink); }
  .health-factors { display: flex; flex-direction: column; gap: 7px; }
  .health-factor {
    display: flex; align-items: baseline; gap: 10px; font-size: var(--fs-base);
    flex-wrap: wrap;
  }
  .health-factor .hf-label {
    min-width: 118px; color: var(--ink); font-weight: 500;
  }
  .health-factor.off .hf-label { color: var(--faint); font-weight: 400; }
  .hf-detail { color: var(--faint); font-size: var(--fs-sm); }
  .health-note { color: var(--faint); font-size: var(--fs-sm); margin-top: 12px; }

  /* Growth tab */
  .growth-row { padding: 10px 0; border-top: 1px solid var(--line); }
  .growth-row:first-child { border-top: none; }
  .growth-row-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 12px; margin-bottom: 4px;
  }
  .focus-list { margin: 4px 0 0 20px; font-size: var(--fs-base); }
  .focus-list li { margin-bottom: 7px; }

  /* Logs */
  .log-box {
    background: var(--log-bg); color: var(--log-ink);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: 14px 16px; font-family: var(--font-mono);
    font-size: var(--fs-sm); line-height: 1.5; overflow-x: auto;
    white-space: pre-wrap; word-break: break-word; max-height: 420px; overflow-y: auto;
  }
  /* .log-tabs and .log-badge retired — Logs now uses the shared .subnav,
     and its error count is a toned .subnav .count (see above). */

  /* Connections */
  .conn-row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .conn-row .conn-name { font-weight: 600; font-size: var(--fs-md); }
  .conn-detail { color: var(--muted); font-size: var(--fs-base); margin-top: 4px; }
  .conn-tested { color: var(--muted); font-size: var(--fs-sm); margin-top: 6px; }
  .conn-tested .mono { font-size: var(--fs-sm); }
  .conn-tested.never { color: var(--faint); }
  /* Persistent connection-state colors: a failed last test stays red and
     a 48h+-old stamp stays amber across page loads — not just in the
     one-shot banner after a Re-test click. */
  .conn-tested.error { color: var(--expense); }
  .conn-tested.stale { color: var(--warn); }
  /* The Gmail read-only trust line — deliberately brighter than the
     surrounding detail text; the promise is the point of the card. */
  .conn-trust { color: var(--ink); font-size: var(--fs-base); margin-top: 6px; }

  /* Connections page grouping: a label + divider between clusters of
     cards, no change to the cards themselves. .conn-group-label joins
     the shared eyebrow rule above for its type treatment. */
  .conn-group { display: flex; align-items: center; gap: 10px; margin: 28px 0 4px; }
  .conn-group:first-child { margin-top: 0; }
  .conn-group-label { font-size: var(--fs-sm); font-weight: 600; color: var(--faint); }
  .conn-group-line { flex: 1; height: 1px; background: var(--line); }

  /* Gmail-reader hints on Invoices / Follow-ups cards and vendor rows */
  .gmail-note { color: var(--muted); font-size: var(--fs-sm); margin: 2px 0 10px; }

  /* Automation: per-job next-run line (read from the OS scheduler).
     .jobs opts this one list into wrapping so the full-width sub-line
     can sit under the row without touching the other .accounts uses. */
  .accounts.jobs li { flex-wrap: wrap; }
  .next-run { flex-basis: 100%; color: var(--muted); font-size: var(--fs-sm); }
  .next-run .mono { font-size: var(--fs-sm); }
  .next-run.none { color: var(--faint); }

  /* AI assistant: floating pill + slide-up chat panel. Local Ollama
     only — the app is identical without it (the button just reads
     "AI offline"). Never a modal: no backdrop, page stays usable. */
  .ai-fab {
    position: fixed; bottom: 24px; right: 24px; z-index: 1200;
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--accent);
    color: var(--ink); border-radius: 999px; padding: 10px 18px;
  }
  .ai-fab:hover:not(.offline) {
    background: var(--raised); box-shadow: 0 0 0 4px var(--accent-soft);
  }
  .ai-fab.offline { color: var(--muted); border-color: var(--line); }
  .ai-fab.offline:hover { background: var(--surface); box-shadow: none; }
  .ai-fab-icon {
    width: 16px; height: 16px; flex-shrink: 0;
    fill: none; stroke: currentColor; stroke-width: 1.6;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .ai-panel {
    position: fixed; bottom: 88px; right: 24px; z-index: 1200;
    width: 380px; max-width: calc(100vw - 48px); max-height: 500px;
    background: var(--raised); border: 1px solid var(--line-strong);
    border-radius: var(--radius);
    display: flex; flex-direction: column;
    opacity: 0; transform: translateY(12px); pointer-events: none;
    transition: opacity 0.18s, transform 0.18s;
  }
  .ai-panel.open { opacity: 1; transform: none; pointer-events: auto; }
  .ai-head {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 12px 16px 8px; font-weight: 600; font-size: var(--fs-base);
  }
  .ai-scope { color: var(--faint); font-size: var(--fs-xs); font-weight: 500; }
  /* Persistent warning pill whenever a cloud provider (OpenAI /
     Anthropic) is selected — data leaves the machine, so the state is
     always visible, not just at selection time. Never shown for the
     local Ollama default. */
  .ai-cloud-pill {
    color: var(--warn); border: 1px solid var(--warn-line);
    background: var(--warn-soft); border-radius: 999px;
    font-size: var(--fs-xs); font-weight: 600; padding: 1px 8px;
    margin-left: 6px; white-space: nowrap;
  }
  .ai-log {
    flex: 1; overflow-y: auto; overflow-x: hidden; padding: 4px 16px; min-height: 120px;
    display: flex; flex-direction: column; gap: 10px;
    scrollbar-width: thin;
  }
  .ai-msg { max-width: 85%; }
  .ai-msg.user { align-self: flex-end; text-align: right; }
  .ai-msg.assistant { align-self: flex-start; }
  .ai-msg .ai-bubble {
    display: inline-block; text-align: left;
    font-size: var(--fs-base); white-space: pre-wrap; word-break: break-word;
  }
  .ai-msg.user .ai-bubble {
    background: var(--accent); color: var(--bg);
    border-radius: 999px; padding: 7px 14px;
    word-wrap: break-word; overflow-wrap: break-word;
    white-space: pre-wrap; max-width: 100%; box-sizing: border-box;
  }
  .ai-msg.assistant .ai-bubble {
    background: var(--surface); color: var(--ink);
    border-radius: var(--radius-sm); padding: 8px 12px;
  }
  .ai-time {
    color: var(--faint); font-family: var(--font-mono);
    font-size: var(--fs-xs); margin-top: 2px;
  }
  .ai-bubble.streaming::after {
    content: "▋"; margin-left: 2px;
    animation: kl-ai-blink 1s steps(2) infinite;
  }
  @keyframes kl-ai-blink { 50% { opacity: 0; } }
  .ai-chips { display: flex; flex-direction: column; gap: 6px; padding: 8px 16px; }
  .ai-chip {
    background: var(--surface); border: 1px solid var(--line-strong);
    color: var(--muted); border-radius: 999px;
    font-size: var(--fs-sm); font-weight: 500; padding: 6px 12px; text-align: left;
  }
  .ai-chip:hover { color: var(--ink); border-color: var(--hover-border-strong); }
  .ai-form {
    display: flex; gap: 8px; align-items: flex-end;
    padding: 10px 16px 14px; border-top: 1px solid var(--line);
  }
  .ai-form textarea { flex: 1; resize: none; max-height: 96px; }
  .ai-send { background: transparent; border-color: var(--line-strong); color: var(--ink); }
  .ai-send:hover:not(:disabled) {
    background: var(--accent); color: var(--bg); border-color: var(--accent);
  }

  /* Responsive.

     Two breakpoints now, not one. The app ships as a resizable desktop
     window, so the band between a phone and a full-width monitor is
     where it actually spends its life — and it was the one band nothing
     targeted. 1100px is where .main-inner.wide stops fitting its own
     maximum. */
  @media (max-width: 1100px) {
    .main-inner { padding: 28px 22px 72px; }
    /* Two-up grids that assume desk width become one column here rather
       than at 640px, where they were already long past comfortable. */
    .chart-pair, .tax-results-grid { grid-template-columns: 1fr; }
    .stat-grid, .planning-results {
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    }
    /* The briefing headline is set for a wide measure; hold its shape
       but stop it dominating a half-width window. */
    .briefing-lead { font-size: var(--fs-2xl); max-width: 24ch; }
  }

  /* Any grid or flex child that can contain a fixed-width chart needs
     the same min-width release as .dash-grid above. */
  .stat-grid > *, .chart-pair > *, .planning-results > * { min-width: 0; }

  @media (max-width: 640px) {
    .wrap { padding: 24px 14px 64px; }
    header { flex-direction: column; align-items: flex-start; gap: 10px; }
    /* The sidebar is desktop-only now — mobile gets the labelled bottom
       tab bar + More sheet instead (see the "Mobile bottom navigation"
       block above). No icon-only collapse: an unlabelled destination is
       a hard no at any width, not a narrow-window compromise. */
    .sidebar { display: none; }
    /* Ctrl+K means nothing without a keyboard; the visible label stays. */
    .header-search .nav-kbd { display: none; }
    .main-inner { padding: 22px 14px calc(60px + var(--bottom-nav-h)); }
    /* These three float above the page edge on desktop; on mobile they
       need to clear the new bottom tab bar by the same margin instead
       of sitting underneath it. */
    .ai-fab { bottom: calc(24px + var(--bottom-nav-h)); }
    .ai-panel { bottom: calc(88px + var(--bottom-nav-h)); }
    .bulk-bar { bottom: calc(var(--sp-5) + var(--bottom-nav-h)); }
    /* A 40px serif headline needs a shorter measure on a phone or it
       sets two words per line. */
    .briefing-lead { font-size: var(--fs-xl); max-width: none; }
    .briefing-points li { font-size: var(--fs-base); }
    .source-briefing { padding: var(--pad-compact); }
    .source-briefing-head { align-items: flex-start; }
    .source-briefing-scope { white-space: normal; text-align: right; }
    .source-signal {
      grid-template-columns: minmax(0, 1fr) auto; gap: var(--sp-2) var(--sp-3);
    }
    .source-signal-tier { grid-column: 1; grid-row: 1; }
    .source-signal-open { grid-column: 2; grid-row: 1; }
    .source-signal-copy { grid-column: 1 / -1; grid-row: 2; }
    .insight-metrics-head { align-items: flex-start; padding: var(--pad-compact); }
    .insight-metric-grid { grid-template-columns: 1fr; }
    .insight-metric, .insight-metrics-empty { padding: var(--pad-compact); }
    .category-select { max-width: 100%; }
    .card { gap: 12px; }
    .card-main { flex-basis: 100%; }
    .card .amount { order: 2; }
    .card .actions { order: 3; margin-left: auto; }
    .totals { gap: 20px; }
    .section table { display: block; overflow-x: auto; }
    .tax-map-form { min-width: 260px; }
    /* -----------------------------------------------------------------
       Card reflow — OPT-IN, per table, via .card-reflow.
       Horizontal scroll is still the default and still correct for
       *scanning* tables, where the column is the point: you compare
       revenue down the Contacts list, or spot the low item down Stock on
       hand, and stacking those into cards puts each number a screen
       apart. It is wrong for *record* tables, where a row is one thing
       you inspect and the figure you want (an invoice's balance) is the
       rightmost column and therefore always off-screen.
       Applied to Invoices and Recurring templates only. Adding a third
       means asking which kind of table it is first.
       ----------------------------------------------------------------- */
    .card-reflow, .card-reflow tbody, .card-reflow tr, .card-reflow td {
      display: block; width: auto;
    }
    .section table.card-reflow { overflow-x: visible; }
    /* thead stays on screen as a sort bar rather than being hidden.
       Sorting lives entirely in these cells (table.js turns each one
       into a button), so display:none here would silently remove the
       only ordering tool on the surface with no browser chrome and no
       Ctrl-F. */
    .card-reflow thead tr {
      display: flex; flex-wrap: wrap; gap: var(--sp-1);
      padding-bottom: var(--sp-2); border-bottom: 1px solid var(--line);
    }
    .card-reflow thead th {
      display: block; width: auto; padding: 0; border: 0; text-align: left;
    }
    .card-reflow thead th:empty { display: none; }
    .card-reflow tbody tr {
      padding: var(--sp-2) 0; border-top: 1px solid var(--line);
    }
    .card-reflow tbody tr:first-child { border-top: 0; }
    .card-reflow tbody td {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: var(--sp-2); padding: 3px 0; border: 0; text-align: right;
      white-space: normal;
    }
    /* The column name travels with the value, so a card is readable
       without the header row it came from. data-label is rendered from
       the same column list that builds thead, so the two cannot drift. */
    .card-reflow tbody td::before {
      content: attr(data-label); color: var(--muted);
      font-size: var(--fs-sm); font-family: var(--font-ui);
      letter-spacing: normal; text-align: left; flex: 0 0 auto;
    }
    .card-reflow tbody td[data-label=""]::before { content: none; }
    .card-reflow tbody td[data-label=""] { justify-content: flex-end; }
    .card-reflow tfoot { display: block; border-top: 1px solid var(--line); }
    .card-reflow tfoot tr { display: flex; justify-content: space-between; }
    .planning-results { grid-template-columns: 1fr; }
    .planning-controls { align-items: flex-start; }
    .tax-toolbar, .tax-toolbar-group { align-items: stretch; }
    .tax-toolbar, .tax-toolbar-group { min-width: 0; max-width: 100%; }
    .tax-toolbar-group { width: 100%; }
    .tax-toolbar-group label { flex: 1 1 140px; min-width: 0; }
    .tax-toolbar input[type="number"], .tax-toolbar input[type="date"],
    .tax-toolbar select { width: 100%; max-width: 100%; }
    .tax-results-grid { grid-template-columns: 1fr; }
    .fu-actions { flex-wrap: wrap; }
    .fu-actions button { flex: 1; }
    /* Charts are SVG with a fixed 640-unit viewBox; scaling that down to
       fit a narrow card shrinks the text right along with the shapes
       (a 12.8px label lands at ~6.7px on a 375px phone — illegible).
       Same fix already used for wide tables on this breakpoint: stop
       forcing the chart to fit, scroll it horizontally instead so every
       number stays exactly as legible as it is on desktop. */
    figure.chart { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .chart-svg { width: 640px; max-width: none; }
    /* The dash-grid's minmax(300px, 1fr) floor never relaxes on its
       own — on a true small-phone viewport (~320-350px) that forces
       the WHOLE page to scroll sideways, not just the grid. Force a
       single column instead; it always fits. */
    .dash-grid { grid-template-columns: 1fr; }
  }

  /* Touch targets: on coarse pointers every control clears 44px
     without changing desktop density (WCAG 2.5.5 comfort target). */
  @media (pointer: coarse) {
    button, .btn-link, .range-toggle .btn-link {
      min-height: 44px; display: inline-flex; align-items: center; justify-content: center;
    }
    .side-nav a { min-height: 44px; }
    input[type="text"], input[type="number"], input[type="date"],
    input[type="password"], input[type="email"], select { min-height: 44px; }
    .accounts li { min-height: 44px; }
    input[type="checkbox"] { width: 20px; height: 20px; }
  }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
  }
"""

# Shared SVG gradient defs for the v2 charts, rendered once per page by
# render_shell (hidden but rendered, so every chart can reference the
# kl-* ids). Chart CSS declares solid fallbacks on every fill.
CHART_DEFS = f"""
<div aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden;">
<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" focusable="false"><defs>
<linearGradient id="kl-bar-h" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#15803D"/><stop offset="1" stop-color="{ACCENT_HEX}"/></linearGradient>
<linearGradient id="kl-bar-h-warn" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#B45309"/><stop offset="1" stop-color="#FBBF24"/></linearGradient>
<linearGradient id="kl-bar-h-neutral" x1="0" y1="0" x2="1" y2="0">
<stop offset="0" stop-color="#52525B"/><stop offset="1" stop-color="#D4D4D8"/></linearGradient>
<linearGradient id="kl-bar-v" x1="0" y1="1" x2="0" y2="0">
<stop offset="0" stop-color="#166534"/><stop offset="1" stop-color="{ACCENT_HEX}"/></linearGradient>
<linearGradient id="kl-area-green" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{ACCENT_HEX}" stop-opacity="0.30"/>
<stop offset="1" stop-color="{ACCENT_HEX}" stop-opacity="0"/></linearGradient>
</defs></svg></div>"""

# The Klearing brand mark (K with a checkmark), recolored for the dark
# shell and rendered inline in the sidebar next to the app name.
# Colors reference the same CSS custom properties as the rest of the
# page (valid on inline SVG in an HTML document) rather than duplicating
# their hex values, so the mark tracks the palette automatically.
BRAND_MARK = """<svg class="brand-mark" viewBox="0 0 512 512" aria-hidden="true">
<rect x="8" y="8" width="496" height="496" rx="104" fill="var(--raised)"/>
<rect x="8" y="8" width="496" height="496" rx="104" fill="none" stroke="var(--line-strong)" stroke-width="10"/>
<line x1="168" y1="132" x2="168" y2="380" stroke="var(--ink)" stroke-width="62" stroke-linecap="round"/>
<line x1="180" y1="266" x2="330" y2="130" stroke="var(--ink)" stroke-width="62" stroke-linecap="round"/>
<polyline points="180,266 288,378 428,192" fill="none" stroke="var(--accent)" stroke-width="62" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

# Small helper used by every credential field in the wizard and on
# Connections: wires format-check-as-you-type plus the Test Connection
# button, both against the same server endpoint (the server is the
# authority; the endpoint runs format checks first and only then a
# live call).
TEST_JS = """
function wireTest(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  const button = root.querySelector('[data-test-button]');
  const status = root.querySelector('[data-test-status]');
  const hint = root.querySelector('[data-test-hint]');
  const gate = root.dataset.gates ? document.getElementById(root.dataset.gates) : null;
  const url = root.dataset.testUrl;
  let timer = null;

  function inputs() {
    return Array.from(root.querySelectorAll('input, select')).filter(i => i.name);
  }
  function formData() {
    const data = new FormData();
    for (const input of inputs()) {
      if (input.type === 'file') {
        if (input.files.length) data.append(input.name, input.files[0]);
      } else {
        data.append(input.name, input.value);
      }
    }
    return data;
  }
  function show(kind, message, hintText) {
    status.className = 'test-status ' + kind;
    status.textContent = message;
    if (hint) {
      hint.style.display = hintText ? '' : 'none';
      hint.textContent = hintText || '';
    }
  }
  async function run(live) {
    const data = formData();
    data.append('live', live ? '1' : '0');
    if (live) show('busy', 'Testing…', null);
    try {
      const response = await fetch(url, { method: 'POST', body: data });
      const result = await response.json();
      if (result.ok) {
        show('ok', '✓ ' + result.message, null);
        if (live && gate) gate.disabled = false;
        if (live) root.dispatchEvent(new CustomEvent('test-passed', { detail: result }));
      } else if (live || result.stage === 'format') {
        show('err', result.message, result.hint);
        if (gate) gate.disabled = true;
      } else {
        show('busy', '', null);
      }
    } catch (err) {
      if (live) show('err', 'The test request itself failed: ' + err, null);
    }
  }
  if (button) button.addEventListener('click', (e) => { e.preventDefault(); run(true); });
  for (const input of inputs()) {
    if (input.type === 'file') {
      input.addEventListener('change', () => run(true));
      continue;
    }
    input.addEventListener('paste', () => setTimeout(() => run(true), 60));
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (input.value.trim()) run(false); }, 500);
    });
  }
}
"""


def money(amount):
    return f"${amount:,.2f}"


def confirm_onsubmit(message, busy_label=None):
    """HTML-attribute-safe confirm handler that blocks the POST unless
    confirmed. With busy_label, the submit button is disabled and
    relabeled once the user confirms — an in-flight state that guards
    against double submission on slow POSTs (a live SMTP send, a
    disconnect, a delete). Disabling inside the handler is the standard
    double-submit guard; the submission itself still proceeds.

    Works both as a <form onsubmit="..."> (this = the form; finds its
    submit button) and as a <button onclick="..."> on forms that share
    one <form> across multiple submit buttons distinguished by
    formaction (this = the clicked button itself, disabled directly) —
    the Follow-ups/Invoices send-or-skip pattern needs the latter, since
    querying the form for "the" submit button can't tell which of two
    was actually clicked."""
    if busy_label:
        js = (f"if (!confirm({json.dumps(message)})) return false; "
              "var b = (this.tagName === 'BUTTON') ? this : "
              "this.querySelector('button[type=submit]'); "
              "if (b) { b.disabled = true; "
              f"b.textContent = {json.dumps(busy_label)}; "
              "} return true;")
        return escape(js)
    return escape(f"return confirm({json.dumps(message)})")


def sub_nav(items, active, aria_label):
    """The one way to switch between sibling views.

    Replaces five separate idioms that all did this job and all looked
    different: three hand-rolled <nav> builders (Ledger, Tax, Insights),
    a row of .btn-link pills (Logs), and a middot-separated sentence
    inside a <p class="hint"> (Tools).

    items: sequence of (key, label, href), optionally (…, count), and
    optionally (…, count, tone). A count of None renders nothing; 0
    renders "0", because on a review queue an explicit zero is
    information and a blank is ambiguous. tone="bad" keeps the red
    treatment the Logs error badge used to have — a count that means
    "something is wrong" should not look like a count that means
    "four files".
    """
    links = ""
    for item in items:
        key, label, href = item[0], item[1], item[2]
        count = item[3] if len(item) > 3 else None
        tone = item[4] if len(item) > 4 else None
        current = key == active
        cls = ' class="active"' if current else ""
        aria = ' aria-current="page"' if current else ""
        count_cls = f"count {tone}" if tone else "count"
        badge = (f'<span class="{count_cls}">{escape(str(count))}</span>'
                 if count is not None else "")
        links += f'<a href="{escape(href, quote=True)}"{cls}{aria}>{escape(label)}{badge}</a>'
    return f'<nav class="subnav" aria-label="{escape(aria_label, quote=True)}">{links}</nav>'


def disclosure(summary, body, note="", is_open=False):
    """Supporting detail, collapsed by default.

    The rule this encodes: a screen should answer its question first and
    keep the evidence one click away. Explanation that is always on
    reads as noise the moment a user has understood it once, and
    Reconcile in particular was several hundred words of it.
    """
    attr = " open" if is_open else ""
    note_html = f'<span class="why">{escape(note)}</span>' if note else ""
    return (f'<details class="disclosure"{attr}><summary>{escape(summary)}'
            f'{note_html}</summary><div class="disclosure-body">{body}'
            f"</div></details>")


def stat(label, value, sub=None, tone=None):
    """One label-and-number cell, for use inside a .stat-grid.

    The single stat vocabulary that replaced .dash-value, .totals .value
    and .planning-result .value — see the grouped typography rule in
    STYLE. tone is 'in', 'out' or None.
    """
    tone_cls = f" {tone}" if tone else ""
    sub_html = f'<div class="dash-sub">{sub}</div>' if sub else ""
    return (f'<div class="stat-cell"><span class="stat-label">{escape(label)}</span>'
            f'<span class="stat-value{tone_cls}">{value}{sub_html}</span></div>')


def empty_state(title, body, action_href=None, action_label=None):
    """The page-level "there is nothing here" state.

    One of exactly two registers the app is allowed: this designed box
    for when a whole view has no content, and .none-note for a single
    empty region inside an otherwise-populated page. There used to be
    four — those two plus a bespoke _ledger_empty_state() and the
    charts' NO_DATA — which meant the choice was made by author habit
    rather than by rule.

    An action is strongly encouraged: an empty state that explains
    nothing and offers nothing is indistinguishable from a bug.
    """
    action = ""
    if action_href and action_label:
        action = (f'<p class="mt-4"><a class="btn-link primary" '
                  f'href="{escape(action_href, quote=True)}">'
                  f'{escape(action_label)}</a></p>')
    return (f'<div class="empty"><p><strong>{escape(title)}</strong></p>'
            f"<p>{body}</p>{action}</div>")


def confirm_attrs(message, title=None, confirm_label=None, busy_label=None,
                  danger=False):
    """Form attributes that open the app's own confirmation dialog.

    The replacement for confirm_onsubmit(). Same job, but the styled
    <dialog> in frontend/app/table.js handles it instead of the browser:
    window.confirm() can't be styled, can't carry the app's voice, and
    in a packaged pywebview window it reads as though the app has broken
    out into the OS.

    Emitted as data- attributes rather than an inline handler so the
    behaviour degrades honestly — if table.js fails to load, the form
    simply submits as a normal POST. That is a deliberate trade: the
    confirm is a guard against misclicks, not a security control, and
    silently breaking submission would be worse than losing the prompt.
    Destructive actions that must not proceed unconfirmed should also
    carry a server-side check.
    """
    attrs = [f'data-confirm="{escape(message, quote=True)}"']
    if title:
        attrs.append(f'data-confirm-title="{escape(title, quote=True)}"')
    if confirm_label:
        attrs.append(f'data-confirm-label="{escape(confirm_label, quote=True)}"')
    if busy_label:
        attrs.append(f'data-busy-label="{escape(busy_label, quote=True)}"')
    if danger:
        attrs.append("data-confirm-danger")
    return " ".join(attrs)


# Hand-rolled line icons (20x20, stroke-only via the .nav-icon class so
# they inherit the link color and match the charts.py aesthetic — no icon
# font or library). One per nav item, applied consistently to all.
NAV_ICONS = {
    "home": '<path d="M3 9l7-6 7 6"/><path d="M5 8v8h10V8"/>',
    "pending": '<circle cx="10" cy="10" r="7"/><path d="M10 6v4l3 2"/>',
    "summary": '<path d="M3 16h14"/><path d="M5 16V9"/><path d="M10 16V4"/>'
               '<path d="M15 16v-6"/>',
    "reconcile": '<path d="M10 3v14"/><path d="M4 7h12"/>'
                 '<path d="M4 7l-2 4h4z"/><path d="M16 7l-2 4h4z"/>',
    "vendors": '<path d="M3 9l6-6 8 8-6 6z"/><circle cx="7" cy="7" r="1.2"/>',
    "contacts": '<circle cx="10" cy="7" r="3"/>'
                '<path d="M4 17c0-3.3 2.7-5 6-5s6 1.7 6 5"/>',
    "insights": '<path d="M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5z"/>'
                '<circle cx="10" cy="10" r="2.3"/>',
    "growth": '<path d="M3 15l5-5 3 3 6-7"/><path d="M13 6h4v4"/>',
    "shopify": '<path d="M4 7h12l-1 10H5z"/><path d="M7 7a3 3 0 016 0"/>',
    "ledger": '<path d="M3 4h5a2 2 0 012 2v10a2 2 0 00-2-2H3z"/>'
              '<path d="M17 4h-5a2 2 0 00-2 2v10a2 2 0 012-2h5z"/>',
    "inventory": '<path d="M3 7l7-4 7 4v6l-7 4-7-4z"/><path d="M3 7l7 4 7-4"/>'
                 '<path d="M10 11v6"/>',
    "tax": '<path d="M4 3h12v14H4z"/><path d="M7 7h6M7 10h6M7 13h4"/>',
    "followups": '<path d="M3 5h14v10H3z"/><path d="M3 6l7 5 7-5"/>',
    "invoices": '<path d="M6 3h6l3 3v11l-2-1-2 1-2-1-2 1V3z"/>'
                '<path d="M8 9h5M8 12h4"/>',
    "tools": '<path d="M4 6h12M4 10h12M4 14h12"/><circle cx="8" cy="6" r="1.7"/>'
             '<circle cx="13" cy="10" r="1.7"/><circle cx="7" cy="14" r="1.7"/>',
    "logs": '<path d="M6 5h9M6 10h9M6 15h9"/><circle cx="3.4" cy="5" r="0.7"/>'
            '<circle cx="3.4" cy="10" r="0.7"/><circle cx="3.4" cy="15" r="0.7"/>',
    "settings": '<circle cx="10" cy="10" r="3"/><path d="M10 2v2M10 16v2M2 10h2'
                'M16 10h2M4.6 4.6l1.4 1.4M14 14l1.4 1.4M15.4 4.6L14 6M6 14l-1.4 1.4"/>',
}

# (label, href, icon key). One coherent list drives the sidebar.
# The sidebar, grouped. Fifteen flat items was a file menu, not
# navigation: Home sat next to Tax sat next to Logs with nothing
# indicating that some are daily and some are annual, and at narrow
# widths it collapsed to fifteen unlabelled hand-drawn glyphs.
#
# Fifteen items in five groups. Three merges did the reducing, and each
# one put things together that were always one job:
#   Review   = Pending + Follow-ups + Invoices   (three queues, one habit)
#   Settings = Settings + Connections + Automation + Logs
#   Tools    = its six sub-pages, which already had their own sub-nav
#
# Everything else — and every sub-view — is reachable from the command
# palette, which is what makes a short sidebar honest rather than a
# place to hide things.
NAV_GROUPS = [
    (None, [("Home", "/", "home"),
            ("Review", "/review", "pending")]),
    ("Money", [("Summary", "/summary", "summary"),
               ("Contacts", "/contacts", "contacts"),
               ("Invoices", "/invoices", "invoices"),
               ("Vendors", "/vendors", "vendors"),
               ("Bank vs books", "/reconcile", "reconcile"),
               ("Shopify", "/shopify", "shopify")]),
    ("Books", [("Ledger", "/ledger", "ledger"),
               ("Inventory", "/inventory", "inventory"),
               ("Tax", "/tax", "tax"),
               ("Tools", "/tools", "tools")]),
    ("Understand", [("Insights", "/insights", "insights"),
                    ("Growth", "/growth", "growth")]),
    ("System", [("Settings", "/settings", "settings")]),
]

# Flat view of the same thing, for callers that just need every tab.
NAV_TABS = [item for _, items in NAV_GROUPS for item in items]

# Everything the command palette can reach: (label, href, group, method).
# This is the other half of the sidebar — the rail shows the places worth
# a permanent slot, and this shows every view plus the handful of verbs
# worth running without navigating first.
#
# Kept as a literal rather than derived from the route table because the
# labels here are written for search ("Ledger — Profit & Loss" so that
# both "ledger" and "pnl" find it), not for the page header.
# (label, href, group, method, search aliases).
#
# The aliases exist because subsequence matching cannot resolve an
# abbreviation: "pnl" is not a subsequence of "Profit & Loss" (there is
# no n), so without them the most-typed shorthand in the whole app found
# nothing. Anything a user is likely to type but would not read on the
# page belongs here.
PALETTE_ENTRIES = [
    ("Home", "/", "Pages", None, "dashboard start"),
    ("Review — Transactions", "/review?tab=transactions", "Review", None,
     "pending queue approve deny inbox"),
    ("Review — Follow-ups", "/review?tab=followups", "Review", None,
     "leads quotes klaviyo"),
    ("Review — Payments", "/review?tab=payments", "Review", None,
     "detected payment apply match overdue chase deposit stripe bank"),
    ("Review — Invoice reminders", "/review?tab=invoices", "Review", None,
     "invoices overdue reminders chase wave receivables"),
    ("Review — Inventory acquisitions", "/review?tab=inventory", "Review", None,
     "inventory purchases funding account fifo cogs reconciliation"),
    ("Uncategorized transactions", "/uncategorized", "Review", None,
     "uncat cleanup"),
    ("Summary", "/summary", "Money", None, "totals categories month"),
    ("Contacts", "/contacts", "Money", None,
     "crm customers clients people who buys margin per customer"),
    ("Contacts — Pipeline", "/contacts?tab=pipeline", "Money", None,
     "crm pipeline stages leads key accounts dormant"),
    ("Invoices", "/invoices", "Money", None,
     "invoice billing customers receivables ar owed"),
    ("Invoices — Drafts", "/invoices?tab=drafts", "Money", None,
     "unsent draft invoice"),
    ("Invoices — A/R aging", "/invoices?tab=aging", "Money", None,
     "ar aging overdue outstanding receivables 30 60 90 buckets"),
    ("Invoices — Recurring", "/invoices?tab=recurring", "Money", None,
     "recurring repeat subscription retainer schedule template monthly"),
    ("Review — Recurring", "/review?tab=recurring", "Review", None,
     "recurring due queued approve repeat retainer"),
    ("New invoice", "/invoices/new", "Money", None,
     "create bill charge customer"),
    ("Vendors", "/vendors", "Money", None, "suppliers spend subscriptions"),
    ("Bank vs books", "/reconcile", "Money", None,
     "reconcile bank wave balance difference match"),
    ("Shopify", "/shopify", "Money", None, "orders store products"),
    ("Ledger — Sync", "/ledger?tab=sync", "Books", None,
     "import plaid exceptions skips"),
    ("Ledger — Trial balance", "/ledger?tab=trial-balance", "Books", None,
     "tb debits credits"),
    ("Ledger — Profit & Loss", "/ledger?tab=pnl", "Books", None,
     "pnl p&l income statement earnings"),
    ("Ledger — Balance sheet", "/ledger?tab=balance-sheet", "Books", None,
     "bs assets liabilities equity"),
    ("Ledger — Accounts", "/ledger?tab=accounts", "Books", None,
     "chart of accounts coa"),
    ("Tax — Legacy archive", "/tax?tab=archive", "Books", None,
     "transactions archive legacy wave categorize schedule c"),
    ("Tax — Statements", "/tax?tab=statements", "Books", None,
     "income statement balance position"),
    ("Tax — Projections", "/tax?tab=projections", "Books", None,
     "estimate quarterly federal iowa"),
    ("Tax — Planning", "/tax?tab=planning", "Books", None, "sku target income"),
    ("Inventory — Stock on hand", "/inventory?tab=stock", "Books", None,
     "stock cogs cost of goods fifo units on hand"),
    ("Inventory — Shopify history", "/inventory?tab=history", "Books", None,
     "historical analytics import csv past sales trend"),
    ("Tools — Data health", "/tools/data-health", "Books", None,
     "sync gaps missing import ofx csv"),
    ("Tools — Accountant export", "/tools/accountant-export", "Books", None,
     "cpa close package pdf month end"),
    ("Tools — Unit economics", "/tools/unit-economics", "Books", None,
     "margin cost price"),
    ("Tools — Sales tax nexus", "/tools/nexus", "Books", None,
     "nexus state threshold"),
    ("Tools — 1099 contractors", "/tools/contractors", "Books", None,
     "1099 contractor payments"),
    ("Tools — Audit log", "/tools/audit", "Books", None,
     "history trail decisions"),
    ("Insights — Overview", "/insights", "Understand", None,
     "shopify plaid inventory cross source metrics"),
    ("Insights — Financial", "/insights?tab=financial", "Understand", None,
     "runway burn cash margin cac ltv"),
    ("Insights — Marketing", "/insights?tab=marketing", "Understand", None,
     "ga4 traffic sessions funnel"),
    ("Growth", "/growth", "Understand", None, "revenue trend channels"),
    ("Settings", "/settings", "System", None, "preferences thresholds"),
    ("Connections", "/settings?tab=connections", "System", None,
     "integrations wave plaid stripe shopify gmail"),
    ("Automation", "/settings?tab=automation", "System", None,
     "schedule daily check cron"),
    ("Logs", "/settings?tab=logs", "System", None, "errors debug output"),
    ("Check for new transactions", "/check", "Actions", "post",
     "sync bank fetch"),
    ("Sync the ledger", "/ledger/sync", "Actions", "post", "post journal import"),
    ("Lock Klearing", "/lock", "Actions", "post", "sign out logout"),
]


def palette_payload():
    """The palette's data, as a JSON island the shell writes once."""
    entries = [
        {"label": label, "href": href, "group": group, "kw": keywords,
         **({"method": method} if method else {})}
        for label, href, group, method, keywords in PALETTE_ENTRIES
    ]
    # </script> inside a JSON string would end the block early.
    return json.dumps(entries).replace("</", "<\\/")


def nav_icon(key):
    return (f'<svg class="nav-icon" viewBox="0 0 20 20" aria-hidden="true">'
            f'{NAV_ICONS.get(key, "")}</svg>')


def render_sidebar(active, badges=None):
    """The left navigation rail: brand mark, then grouped icon-and-label
    links with the active one highlighted (visually and via
    aria-current). The palette trigger used to live here too; it is now
    a persistent header chip (see render_shell) so it stays labelled and
    reachable even when this sidebar is hidden.

    badges maps a tab label to a count shown beside it — used by Review
    so the number of things waiting is visible without opening it. A
    count of 0 renders nothing: a zero badge on a queue reads as an alert
    for work that does not exist.

    Desktop only. On a narrow window this is hidden entirely in favour
    of render_mobile_nav's labelled bottom tab bar + More sheet — not an
    icon-only collapse of this rail, which is exactly the pattern this
    redesign was meant to retire.
    """
    badges = badges or {}

    def link(label, href, icon):
        is_active = label == active
        cls = ' class="active" aria-current="page"' if is_active else ""
        count = badges.get(label)
        badge = (f'<span class="nav-badge">{escape(str(count))}</span>'
                 if count else "")
        return (f'<a href="{href}"{cls} title="{escape(label)}">'
                f'{nav_icon(icon)}'
                f'<span class="nav-label">{escape(label)}</span>'
                f"{badge}</a>")

    groups = "".join(
        nav_group_html("nav", group_label,
                       "".join(link(*item) for item in items))
        for group_label, items in NAV_GROUPS)
    return (f'<aside class="sidebar"><div class="sidebar-brand">{BRAND_MARK}'
            f'<span class="brand-name">{APP_NAME}</span></div>'
            f'<nav class="side-nav" aria-label="Sections">{groups}</nav></aside>')


def table_head(columns):
    """A <thead> built from a column spec, for tables that card-reflow.

    columns is a sequence of (label, sort, css_class); sort is
    "text"/"num"/"date" or None, css_class is usually "" or "num".

    The point of declaring columns once is that a card-reflowed cell
    carries its own column name in data-label, and that name has to be
    the header's name. Rendering both from this list makes them the same
    string by construction rather than by two people remembering to edit
    two places (see anti-patterns #3, and #12 on documentation drift).
    Pair it with cell_label() when building the rows.
    """
    cells = ""
    for label, sort, css_class in columns:
        attrs = f' class="{css_class}"' if css_class else ""
        attrs += f' data-sort="{sort}"' if sort else ""
        cells += f"<th{attrs}>{escape(label)}</th>"
    return f"<thead><tr>{cells}</tr></thead>"


def cell_label(columns, index):
    """The data-label attribute for the cell in a given column.

    Empty for a column with no header (an actions column), which the
    stylesheet reads as "render no label for this one".
    """
    label = columns[index][0]
    return f' data-label="{escape(label, quote=True)}"'


def nav_group_html(prefix, group_label, links_html):
    """One labelled nav group, announced as a group rather than as loose
    text sitting above some links.

    The label used to be a bare <div class="nav-group">, which meant a
    screen reader only encountered it when reading the page linearly.
    Anyone tabbing link to link -- the normal way to use navigation --
    never heard it at all, so the visual grouping Phase 4 built had no
    non-visual equivalent.

    role="group" + aria-labelledby gives the links a named container
    without adding a landmark. Splitting each group into its own <nav>
    would have stronger screen-reader support, but six navigation
    landmarks in one page makes landmark navigation worse than the
    problem being fixed.

    prefix namespaces the generated id because the sidebar and the
    mobile More sheet both render every group into the same document,
    and a duplicate id would point aria-labelledby at whichever came
    first.
    """
    if not group_label:
        # The ungrouped pair (Home, Review) is deliberately not wrapped:
        # a group of "everything with no heading" is a container with
        # nothing to announce.
        return links_html
    slug = re.sub(r"[^a-z0-9]+", "-", group_label.lower()).strip("-")
    group_id = f"{prefix}-group-{slug}"
    return (f'<div role="group" aria-labelledby="{group_id}">'
            f'<div class="nav-group" id="{group_id}">{escape(group_label)}</div>'
            f"{links_html}</div>")


# Real create destinations only. Each target already exists as a route, so
# this menu adds no new mutation path.
NEW_MENU_ITEMS = [
    ("Invoice", "/invoices/new"),
    ("Recurring invoice", "/invoices?tab=recurring"),
    ("Contact", "/contacts"),
]


def render_new_menu():
    """The Wave-style create button: one place to start new work."""
    links = "".join(f'<a href="{href}">{escape(label)}</a>'
                    for label, href in NEW_MENU_ITEMS)
    return ('<details class="new-menu"><summary>'
            '<svg class="nav-icon" viewBox="0 0 20 20" aria-hidden="true">'
            '<path d="M10 4v12M4 10h12"/></svg>New</summary>'
            f'<div class="new-menu-list">{links}</div></details>')


def render_search_chip():
    """The command palette trigger, rendered once in the page header so
    it is visible and labelled at every width — unlike the old
    sidebar-only version, which disappeared along with the rest of the
    sidebar's labels on a narrow window."""
    return (
        '<button class="nav-search header-search" type="button" '
        'data-palette-open title="Search and jump to anything (Ctrl+K)">'
        '<svg class="nav-icon" viewBox="0 0 20 20" aria-hidden="true">'
        '<circle cx="9" cy="9" r="5.5"/><path d="M13 13l4 4"/></svg>'
        '<span class="nav-label">Search or jump to&hellip;</span>'
        '<kbd class="nav-kbd">Ctrl K</kbd></button>')


# The four daily-use destinations pinned to the mobile bottom tab bar.
# Everything else in NAV_GROUPS lives in the More sheet instead — see
# render_mobile_nav. Home and Review are the ungrouped pair NAV_GROUPS
# already treats as always-visible; Summary and Ledger are the next two
# most frequent touchpoints (month-to-date totals, reconciliation).
MOBILE_PINNED_TABS = ["Home", "Review", "Summary", "Ledger"]


def render_mobile_nav(active, badges=None):
    """The mobile equivalent of the sidebar: a fixed bottom tab bar with
    four labelled destinations plus a labelled More, and a More sheet
    listing everything else from NAV_GROUPS, grouped exactly the way the
    desktop sidebar groups them. Same source data as render_sidebar, two
    renderings — never a second, hand-maintained nav list.

    badges is the same dict render_sidebar takes; the Review tab here
    carries the identical live count.
    """
    badges = badges or {}
    destinations = {label: (href, icon) for _, items in NAV_GROUPS
                    for label, href, icon in items}

    def _link(label, href, icon, label_cls):
        is_active = label == active
        cls = ' class="active" aria-current="page"' if is_active else ""
        count = badges.get(label)
        badge = (f'<span class="nav-badge">{escape(str(count))}</span>'
                 if count else "")
        return (f'<a href="{href}"{cls}>{nav_icon(icon)}'
                f'<span class="{label_cls}">{escape(label)}</span>'
                f"{badge}</a>")

    tabs = "".join(
        _link(label, *destinations[label], "bottom-nav-label")
        for label in MOBILE_PINNED_TABS)

    more = ""
    for group_label, items in NAV_GROUPS:
        rest = [item for item in items if item[0] not in MOBILE_PINNED_TABS]
        if not rest:
            continue
        more += nav_group_html(
            "more", group_label,
            "".join(_link(label, href, icon, "nav-label")
                    for label, href, icon in rest))

    return f"""
  <nav class="bottom-nav" aria-label="Primary">
    {tabs}
    <button type="button" class="bottom-nav-more" data-more-open
            aria-haspopup="dialog" aria-controls="more-sheet">
      <svg class="nav-icon" viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="4" cy="10" r="1.4"/><circle cx="10" cy="10" r="1.4"/>
        <circle cx="16" cy="10" r="1.4"/>
      </svg>
      <span class="bottom-nav-label">More</span>
    </button>
  </nav>
  <dialog class="more-sheet" id="more-sheet" aria-label="More destinations">
    <div class="drawer-head">
      <div class="who">More</div>
      <button type="button" class="drawer-close" data-more-close>Close</button>
    </div>
    <nav class="side-nav more-sheet-nav"
         aria-label="More destinations">{more}</nav>
  </dialog>"""


def render_business_switcher():
    """A header dropdown for switching the active local business —
    rendered only when more than one is configured. Imported lazily so
    pre-setup pages never trip over missing config."""
    try:
        import business_registry
        businesses = business_registry.list_businesses()
        if len(businesses) < 2:
            return ""
        active = business_registry.active_business() or {}
        options = ""
        for business in businesses:
            selected = " selected" if business["id"] == active.get("id") else ""
            options += (f'<option value="{escape(business["id"])}"{selected}>'
                        f'{escape(business["name"])}</option>')
        return f"""
        <form method="post" action="/business/switch" style="display:inline;">
          <select name="business_id" onchange="this.form.submit()"
                  title="Active business">{options}</select>
        </form>"""
    except Exception:
        return ""


ASSISTANT_HTML = """
  <button id="ai-fab" class="ai-fab" aria-expanded="false" aria-controls="ai-panel">
    <svg class="ai-fab-icon" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M3 4.5h14v9.5H8.5L4.5 17v-3H3z"/></svg>
    <span class="ai-fab-label">Ask Klearing</span>
  </button>
  <div id="ai-panel" class="ai-panel" role="complementary" aria-label="Klearing assistant">
    <div class="ai-head"><span id="ai-title">Klearing assistant</span>
      <span id="ai-scope" class="ai-scope">local model &middot; nothing leaves this computer</span></div>
    <div id="ai-log" class="ai-log" aria-live="polite"></div>
    <div id="ai-chips" class="ai-chips">
      <button type="button" class="ai-chip">Why is my reconciliation flagged?</button>
      <button type="button" class="ai-chip">Which vendor should I look at this month?</button>
      <button type="button" class="ai-chip">How does this month compare to last?</button>
    </div>
    <form id="ai-form" class="ai-form">
      <textarea id="ai-input" rows="1" placeholder="Ask about your numbers"
                aria-label="Message the assistant"></textarea>
      <button id="ai-send" class="ai-send" type="submit">Send</button>
    </form>
  </div>"""

ASSISTANT_JS = """
(function () {
  var btn = document.getElementById('ai-fab');
  if (!btn) return;
  var panel = document.getElementById('ai-panel');
  var log = document.getElementById('ai-log');
  var form = document.getElementById('ai-form');
  var input = document.getElementById('ai-input');
  var send = document.getElementById('ai-send');
  var chips = document.getElementById('ai-chips');
  var online = false;
  var busy = false;
  // Provider-aware offline hint; /ai/status replaces it (e.g. a cloud
  // provider with no key says where to add one).
  var offlineHint = 'Start Ollama to use the assistant.';

  var hist = [];
  try { hist = JSON.parse(sessionStorage.getItem('kl_ai_hist') || '[]'); }
  catch (e) { hist = []; }
  function saveHist() {
    try { sessionStorage.setItem('kl_ai_hist', JSON.stringify(hist.slice(-40))); }
    catch (e) {}
  }
  function stamp() { return new Date().toTimeString().slice(0, 5); }

  function addMsg(role, text, ts) {
    var wrap = document.createElement('div');
    wrap.className = 'ai-msg ' + role;
    var bubble = document.createElement('div');
    bubble.className = 'ai-bubble';
    bubble.textContent = text;
    var time = document.createElement('div');
    time.className = 'ai-time';
    time.textContent = ts || stamp();
    wrap.appendChild(bubble); wrap.appendChild(time);
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return bubble;
  }

  hist.forEach(function (m) { addMsg(m.role, m.text, m.ts); });
  if (hist.length && chips) { chips.remove(); chips = null; }

  fetch('/ai/status').then(function (r) { return r.json(); })
    .then(function (s) {
      online = !!s.available;
      if (s.hint) offlineHint = s.hint;
      if (!online) {
        btn.classList.add('offline');
        btn.querySelector('.ai-fab-label').textContent = 'AI offline';
      }
      if (s.cloud) {
        // Persistent "Cloud AI active" pill: data leaves the machine
        // while OpenAI/Anthropic is selected, so the state stays
        // visible in the panel header the whole time.
        var title = document.getElementById('ai-title');
        var scope = document.getElementById('ai-scope');
        if (title) {
          var pill = document.createElement('span');
          pill.className = 'ai-cloud-pill';
          pill.textContent = 'Cloud AI active';
          title.appendChild(pill);
        }
        if (scope) {
          scope.textContent = 'cloud model · data leaves this machine';
        }
      }
    })
    .catch(function () { btn.classList.add('offline'); });

  var open = sessionStorage.getItem('kl_ai_open') === '1';
  function setOpen(v) {
    open = v;
    panel.classList.toggle('open', v);
    btn.setAttribute('aria-expanded', v ? 'true' : 'false');
    try { sessionStorage.setItem('kl_ai_open', v ? '1' : '0'); } catch (e) {}
    if (v) {
      log.scrollTop = log.scrollHeight;
      input.focus();
    } else {
      // Non-modal by design — the panel never traps focus while open,
      // so the only restoration needed on close is returning to the one
      // control that ever opens it.
      btn.focus();
    }
  }
  if (open) setOpen(true);
  btn.addEventListener('click', function () { setOpen(!open); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      setOpen(false);
    }
  });

  function setBusy(v) { busy = v; input.disabled = v; send.disabled = v; }

  function ask(text) {
    text = (text || '').trim();
    if (!text || busy) return;
    if (chips) { chips.remove(); chips = null; }
    if (!online) {
      addMsg('assistant', offlineHint);
      return;
    }
    addMsg('user', text);
    hist.push({ role: 'user', text: text, ts: stamp() }); saveHist();
    input.value = '';
    setBusy(true);
    var bubble = addMsg('assistant', '');
    bubble.classList.add('streaming');
    var mainEl = document.querySelector('main[data-section]');
    var section = mainEl ? mainEl.getAttribute('data-section') : 'Home';
    fetch('/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, section: section })
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var acc = '';
      function pump() {
        return reader.read().then(function (step) {
          if (step.done) return acc;
          acc += dec.decode(step.value, { stream: true });
          bubble.textContent = acc;
          log.scrollTop = log.scrollHeight;
          return pump();
        });
      }
      return pump();
    }).then(function (acc) {
      bubble.classList.remove('streaming');
      hist.push({ role: 'assistant', text: acc, ts: stamp() }); saveHist();
    }).catch(function () {
      bubble.classList.remove('streaming');
      bubble.textContent = offlineHint;
    }).then(function () { setBusy(false); });
  }

  form.addEventListener('submit', function (e) { e.preventDefault(); ask(input.value); });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input.value); }
  });
  var chipEls = document.querySelectorAll('.ai-chip');
  for (var i = 0; i < chipEls.length; i++) {
    (function (chip) {
      chip.addEventListener('click', function () { ask(chip.textContent); });
    })(chipEls[i]);
  }
})();
"""


def render_shell(active_tab, header_extra, content, message=None, error=None,
                 show_nav=True, extra_js="", wide=False, nav_badges=None):
    banner = ""
    assistant_html = ASSISTANT_HTML
    try:
        import shopify_client
        if shopify_client.is_configured():
            assistant_html = assistant_html.replace(
                '<button type="button" class="ai-chip">How does this month compare to last?</button>',
                '<button type="button" class="ai-chip">How does this month compare to last?</button>'
                '<button type="button" class="ai-chip">How is my Shopify store doing this month?</button>')
    except Exception:
        pass
    if error:
        banner = f'<div class="banner error">{escape(error)}</div>'
    elif message:
        banner = f'<div class="banner">{escape(message)}</div>'

    script = f"<script>{TEST_JS}{extra_js}</script>" if (extra_js or "wireTest" in content) else ""
    footer = (f'<footer title="Data folder: '
              f'{escape(str(paths.data_dir()), quote=True)}">'
              'Your data stays on this machine.</footer>')

    if not show_nav:
        # Unlock / setup: no sidebar, centered single column (unchanged).
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_NAME}</title>
  <style>{STYLE}</style>
</head>
<body>
  <div class="wrap{' wide' if wide else ''}">
    <header><h1>{APP_NAME}</h1>{header_extra or ""}</header>
    {banner}{content}{footer}
  </div>
  {script}
</body>
</html>"""

    header_extra = render_business_switcher() + (header_extra or "")
    demo_banner = ""
    if paths.is_demo():
        demo_banner = (
            '<div class="demo-banner" role="status"><span>This is a demo with '
            'made-up data. Nothing here is real.</span>'
            '<form method="post" action="/demo/exit" class="inline-form">'
            '<button class="check" type="submit">Exit demo</button></form></div>')
    page_title = escape(active_tab or APP_NAME)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_NAME}</title>
  <style>{STYLE}</style>
</head>
<body>
  <a class="skip-nav" href="#main">Skip to content</a>
  {CHART_DEFS}
  <div class="app-shell">
    {render_sidebar(active_tab, nav_badges)}
    <main class="main" id="main" tabindex="-1"
          data-section="{escape(active_tab or 'Home')}">
      <div class="main-inner{' wide' if wide else ''}">
        {demo_banner}
        <header>
          <h1>{page_title}</h1>
          <div class="header-actions">{render_search_chip()}{header_extra}{render_new_menu()}</div>
        </header>
        {banner}
        {content}
        {footer}
      </div>
    </main>
  </div>
  {render_mobile_nav(active_tab, nav_badges)}
  {assistant_html}
  {script}
  <script>{ASSISTANT_JS}</script>
  <script>window.KLEARING_PALETTE = {palette_payload()};</script>
  <script src="/static/app/table.js?v=10" defer></script>
</body>
</html>"""
