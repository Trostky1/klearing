
# Klearing Financial UI Audit Skill

Load this skill to audit Klearing's UI against the standard set by
best-in-class financial tools: Mercury, Wave, Stripe Dashboard, 
Klaviyo, Linear, and Vercel. These are the aesthetic and interaction 
references for Klearing's dark ledger design language.

This is not a bug hunt. This asks: does the UI feel like a serious,
trusted financial tool or does it feel like a side project?

Run before any public launch.
Run after any major UI change.
Run whenever a new screen or component is added.

---

## How to Invoke

```
Read .context/skills/klearing-financial-ui.md and run a full 
financial UI audit. Complete every phase in order.
Do not fix anything â€” report only.
Output a prioritized improvement list at the end.
```

---

## Prerequisites

Read before starting:
1. .context/skills/klearing-ui.md
2. .context/skills/design-tokens.md
3. .context/skills/anti-patterns.md
4. .context/CONTEXT.md

---

## The Reference Standard

These are the products Klearing's UI should feel comparable to:

**Mercury** â€” dark financial dashboard, clean data tables, confident 
typography, zero decoration, every pixel earns its place.
Benchmark: if a Mercury engineer opened Klearing, would they wince?

**Stripe Dashboard** â€” the gold standard for financial data density.
Clear hierarchy, monospace numbers everywhere they matter, status 
pills that mean something, table rows you can scan in one pass.
Benchmark: can a user scan a table row and understand it in 2 seconds?

**Wave** â€” clean, approachable small-business accounting. 
Not Mercury-dark but trustworthy and uncluttered.
Benchmark: would a freelancer feel comfortable handing this to their accountant?

**Klaviyo** â€” data-dense marketing analytics. Strong use of 
sparklines, trend indicators, delta pills, percentage changes.
Benchmark: do the metrics feel alive or frozen?

**Linear** â€” the best dark-mode productivity UI. Keyboard-first,
information-dense, no wasted space, typography that works at small sizes.
Benchmark: does the UI feel fast and responsive or heavy and slow?

**Vercel Dashboard** â€” deployment/analytics UI. Excellent use of 
status indicators, monospace values, clean empty states.
Benchmark: do status states communicate instantly without reading?

---

## Phase 1 â€” Typography and Number Rendering

**Goal:** Numbers look like financial data, not website copy.

Check every screen for:

MONOSPACE CONSISTENCY
- Every dollar amount uses --font-mono (no exceptions)
- Every date uses --font-mono
- Every percentage uses --font-mono  
- Every count/quantity uses --font-mono where alignment matters
- Mixed phrases (words + numbers) use system font â€” confirmed?
- Table columns with numbers are right-aligned for vertical scanning

NUMBER FORMATTING
- Dollar amounts: always 2 decimal places ($1,240.00 not $1240)
- Large amounts: comma-separated ($12,400.00 not $12400.00)
- Negative amounts: explicit minus sign AND red color, not just color
- Percentages: always include % symbol (62% not 0.62)
- Days: whole numbers only (42 days not 41.7 days)
- Zero amounts: shown as $0.00 not blank or "--"
- Null/unknown: shown as "â€”" (em dash) not blank, null, or "N/A"

TYPOGRAPHY HIERARCHY
- Is there a clear visual hierarchy: heading â†’ subheading â†’ label â†’ value?
- Can a user find the most important number on each screen in 2 seconds?
- Are labels smaller and muted than their values?
- Are section headers visually distinct from card headers?
- Is body text comfortably readable at --fs-base?
- Does small text (--fs-xs, --fs-sm) remain readable at 12px minimum?

STRIPE-STYLE TABLE ROWS
- Can each table row be scanned left-to-right in one pass?
- Is the most important column (vendor name, amount) leftmost?
- Are status indicators (flags, badges) visually grouped with their data?
- Do alternating row treatments exist where tables are long?
- Are column widths consistent â€” no jumping between rows?

Output: **Typography Issues List** â€” screen, element, issue, fix

---

## Phase 2 â€” Status Communication

**Goal:** The user knows the state of everything without reading carefully.

This is Mercury's strongest suit. Every account, connection, and 
transaction has an instantly readable status.

Check every status indicator across all screens:

STATUS PILLS AND BADGES
- Is every status represented by both color AND a label?
  (never color-only â€” colorblind users exist)
- Are status meanings consistent across screens?
  Green = good/active, Amber = warning/pending, Red = problem/overdue
  everywhere â€” no exceptions
- Do status pills use the correct tokens?
  --income for positive, --expense for negative, --warn for caution,
  --muted for neutral/inactive
- Are statuses scannable in a list without reading each one?

CONNECTION STATES (Connections page)
- Does each connection show: connected / not connected / error / 
  testing â€” all four states?
- Is "last tested" always visible, never hidden?
- Does a failed connection explain why it failed?
- Is reauthentication clearly prompted when a token expires?
- Does the test button give immediate feedback (not just silent reload)?

TRANSACTION STATES (Pending page)
- Can the user tell at a glance: how many pending, how urgent, 
  how long waiting?
- Is "pending" visually distinct from "approved" and "skipped"?
- Do overdue follow-ups look different from fresh ones?
- Is the queue ordered by urgency or just by date?

RECONCILIATION STATES
- Does "flagged" look alarming enough to act on?
- Does "in sync" look calm and confident â€” not just absence of red?
- Is the residual amount prominent when flagged?

INVOICE STATES
- Are overdue invoices red and prominent?
- Are recently paid invoices visually resolved (not just gone)?
- Does days-overdue have urgency treatment? 
  (7 days = amber, 30+ days = red, 60+ days = something louder)

Output: **Status Issues List** â€” screen, element, current state, 
what's missing

---

## Phase 3 â€” Data Table Quality

**Goal:** Tables feel like Stripe's transaction list, not an HTML table.

Klearing has multiple data tables across Vendors, Summary, Insights,
Tools, and Audit. Check each one:

SCANNABILITY
- Is the primary identifier (name, date, vendor) in the first column?
- Are monetary columns right-aligned?
- Are all rows the same height?
- Is row density appropriate? (not too tight, not too airy)
- Can a user find a specific row without reading every row?

COLUMN DESIGN
- Are there more than 6 columns? If so, consider progressive disclosure
- Are column headers short and unambiguous?
- Do columns have consistent widths across page loads?
- Are there any columns that are almost always empty? Remove them.

ROW INTERACTIONS
- Does hovering a row provide visual feedback?
- Are clickable rows visually distinct from non-clickable rows?
- Do drill-down links look like links, not just underlined text?
- Is the click target the full row or just a small link?

SORTING AND FILTERING
- Can the user sort by any column that has magnitude?
- Is the current sort order indicated?
- Does filtering update instantly without a page reload?
- Is there a search for tables with more than 20 rows?

EMPTY TABLE STATES
- Does an empty table explain why it's empty?
- Does it suggest what action would populate it?
- Does it look designed, not like a bug?

STRIPE-STANDARD CHECK
Look at each table and ask:
"Would this table fit in the Stripe Dashboard without looking wrong?"
If no â€” flag it.

Output: **Table Issues List** â€” screen, table, issue, fix

---

## Phase 4 â€” Interaction and Feedback Quality

**Goal:** Every action has immediate, appropriate feedback.

Check every interactive element across all screens:

BUTTON STATES
- Does every button have: default, hover, active/pressed, 
  disabled, and loading states?
- Is the loading state visually distinct from disabled?
- Does the button label change during loading? 
  ("Save" â†’ "Saving..." â†’ "Saved")
- Are destructive actions (skip, deny) visually distinct from
  constructive actions (approve, send)?
- Are primary and secondary button hierarchy clear at a glance?

FORM INPUTS
- Do inputs have placeholder text that explains the expected format?
- Do inputs show validation inline, not just on submit?
- Are error messages specific? ("Invalid date" not "Error")
- Do inputs have visible focus states?
- Are required fields marked?

CONFIRMATION FLOWS
- Does every destructive action require confirmation?
- Is the confirmation specific? ("Skip this follow-up for John Smith?" 
  not "Are you sure?")
- Is the cancel action always available and prominent?
- Does confirming an action show what happened? (success banner)

FEEDBACK TIMING
- Do success states appear within 200ms of action?
- Do error states explain what went wrong and what to do?
- Do loading states appear within 100ms to prevent double-clicks?
- Do banners auto-dismiss or require manual dismissal â€” is this consistent?

MERCURY-STANDARD CHECK
Mercury's interactions feel instant and confident. Ask:
"Does this interaction feel confident or hesitant?"
Hesitant = slow feedback, unclear state, ambiguous result.

Output: **Interaction Issues List** â€” screen, element, issue, fix

---

## Phase 5 â€” Visual Hierarchy and Layout

**Goal:** The most important information is the most visually prominent.

Check each screen's information hierarchy:

ABOVE THE FOLD
- What is the first thing the user sees on each screen?
- Is that the most important thing for this screen's job?
- Is there a clear hero metric or status on every screen?
- Does the page title match what the screen actually does?

CARD AND SECTION LAYOUT
- Are cards grouped by relationship, not just placed in order?
- Is there a clear reading direction (top-left to bottom-right)?
- Are related items visually close and unrelated items visually separated?
- Do sections have clear headings that explain what they contain?

WHITE SPACE
- Is there enough breathing room between sections?
- Are elements touching that shouldn't be?
- Does the layout feel dense-but-intentional (Mercury) or 
  cramped (over-engineered)?
- Is white space consistent across sections of the same type?

SIDEBAR AND NAVIGATION
- Is the active state obvious at a glance?
- Are navigation items ordered by frequency of use?
- Does the sidebar feel like part of the app or bolted on?
- At narrow widths, does navigation remain accessible?

MOBILE AND NARROW WINDOW
- At 375px: is every screen functional and readable?
- At 768px: does the layout adapt or just scale down?
- Are touch targets at least 44px tall on all interactive elements?
- Is horizontal scrolling eliminated on all screens?

LINEAR-STANDARD CHECK
Linear's layouts feel intentional and spacious even when dense.
Ask: "Does this layout feel designed or assembled?"
Assembled = elements placed without a clear grid or rhythm.

Output: **Layout Issues List** â€” screen, section, issue, fix

---

## Phase 6 â€” Color and Semantic Meaning

**Goal:** Color communicates meaning, not decoration.

Check every use of color across all screens:

SEMANTIC CONSISTENCY
- Green (--income, --accent): always means positive/good/income
- Red (--expense): always means negative/bad/expense/overdue
- Amber (--warn): always means caution/pending/watch
- Teal (--transfer): always means neutral movement/transfer
- Muted (--muted, --faint): always means secondary/inactive/metadata

Check each screen for:
- Any green used decoratively (not for positive meaning)
- Any red used for non-negative meaning
- Any color used inconsistently vs its semantic definition
- Status indicators that contradict the color's meaning

ACCENT COLOR DISCIPLINE
- --accent (#4ADE80) appears on exactly ONE element per screen
- It draws the eye to the most important interactive element
- It is never used for decoration
- It is never used in charts except as the income/positive series

DARK THEME SPECIFIC
- Is contrast sufficient on all text? (minimum 4.5:1 for body text)
- Do borders use --line or --line-strong appropriately?
- Are elevated surfaces (modals, tooltips, dropdowns) visually 
  distinct from the page surface?
- Does the dark theme feel intentional (Mercury) or just inverted?

WAVE-STANDARD CHECK
Wave uses color sparingly and purposefully. Ask:
"If all color was removed, would the layout still communicate hierarchy?"
If no â€” the layout is relying on color as structure, not accent.

Output: **Color Issues List** â€” screen, element, current color, 
semantic meaning, issue

---

## Phase 7 â€” Empty States and Edge Cases

**Goal:** Every state is a designed state, not an afterthought.

Check every screen for:

EMPTY STATES
- No data yet: does the screen explain why and what to do?
- Does the empty state look designed, not like a bug?
- Does it match the tone of the rest of the app?
- Does it suggest a specific next action with a real link?
- Is the empty state illustration or icon appropriate (if present)?

LOADING STATES
- Do screens with external data show a loading state?
- Is the loading state fast to appear (< 100ms)?
- Does the loading state match the shape of the content it replaces?
- Are Stripe/Gusto/Gmail screens visibly loading when fetching?

ERROR STATES
- Do network errors explain what failed in plain English?
- Do errors suggest what the user can do (retry, check connection)?
- Are errors dismissable?
- Do errors go to the Logs page automatically?

EDGE CASE DATA
- What does the dashboard look like with 1 transaction?
- What does the vendor page look like with 1 vendor?
- What does the summary look like with $0 in and $0 out?
- What does the growth page look like in month 1?
- What does reconciliation look like with no bank connected?

VERCEL-STANDARD CHECK
Vercel's empty and error states feel calm and helpful.
Ask: "Does this empty state make the user feel stuck or guided?"
Stuck = blank space with no explanation.
Guided = clear next step with a real action.

Output: **State Issues List** â€” screen, state type, issue, fix

---

## Phase 8 â€” Financial Tool Trust Signals

**Goal:** The app looks like something you'd trust with your business finances.

This phase is subjective but critical. These are the signals that 
make users trust a financial tool:

CHECK EACH:

DATA FRESHNESS
- Is it always clear when data was last updated?
- Do stale connections show their last-sync time?
- Does the user know if they're looking at real-time or cached data?

AUDITABILITY
- Can the user see where a number came from?
- Is there a path from every summary number to its source transactions?
- Is the Audit log prominent enough to find?

PRECISION
- Are numbers precise where precision matters (reconciliation, invoices)?
- Are numbers appropriately rounded where precision is misleading (runway)?
- Is the $1.00 noise threshold documented and visible?

PROFESSIONAL LANGUAGE
- Does copy sound like a CFO wrote it or a startup intern?
- Are accounting terms used correctly? (reconcile, not "sync check")
- Are plain-English explanations available alongside technical terms?
- Is the tone confident and calm, not hedging and apologetic?

EXPORT QUALITY
- Does the PDF close package look like something an accountant 
  would accept?
- Are exports labeled with dates, versions, and "generated by Klearing"?
- Are numbers in exports formatted identically to the UI?
- Is the mandatory footer present and professional?

CONSISTENCY OF DETAIL
- Are loading behaviors identical across similar screens?
- Do all modals have the same border radius and shadow?
- Are all success banners the same height and color?
- Do all error messages follow the same format?

MERCURY-STANDARD FINAL CHECK
Open Mercury's dashboard in your mind. Now open Klearing.
Ask: "Would a CFO or accountant trust Klearing with their books 
based on how it looks?"
If there is any hesitation â€” find the source and flag it.

Output: **Trust Signal Issues List** â€” screen, element, issue, 
severity (blocks trust / reduces trust / minor)

---

## Output Format

After all 8 phases, produce a single consolidated report:

### Klearing Financial UI Audit â€” [date]

**Summary:**
- Phase 1 Typography: X issues
- Phase 2 Status Communication: X issues
- Phase 3 Data Tables: X issues
- Phase 4 Interactions: X issues
- Phase 5 Layout: X issues
- Phase 6 Color: X issues
- Phase 7 Empty States: X issues
- Phase 8 Trust Signals: X issues

**Top 10 Highest-Impact UI Improvements:**
Ranked by trust impact Ã— implementation ease

1. [improvement] â€” [screen] â€” [effort: low/medium/high]
2. ...

**Blocks Trust (fix before launch):**
- [issue] â€” [screen] â€” [why it blocks trust]

**Quick Wins (CSS/copy only, no logic changes):**
- [improvement] â€” [screen] â€” [what to change]

**Consistency Passes (same fix across multiple screens):**
- [pattern] â€” [screens affected] â€” [fix once, apply everywhere]

---

## After the Audit

Do not fix anything during the audit run.
Bring the report back for approval.
Trust blockers go first â€” no exceptions.
Quick wins go second â€” high ROI, low risk.
Consistency passes go third â€” batch them into one PR.

Branch: feat/financial-ui-[date]
Update anti-patterns.md with any new financial UI patterns found.
Run python .context/sync.py after updating skill files.

