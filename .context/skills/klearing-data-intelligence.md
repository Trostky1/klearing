# Klearing Data Intelligence Audit Skill

Load this skill to audit how effectively Klearing communicates data.
This is not a visual bug hunt or a feature gap analysis.
This asks one question: does the user actually understand what the data means?

Run after every major feature addition.
Run before any public launch.
Run whenever a new chart, metric, or number is added to any screen.

---

## How to Invoke

```
Read .context/skills/klearing-data-intelligence.md and run a full 
data intelligence audit. Complete every phase in order.
Do not fix anything — report only.
Output a prioritized improvement list at the end.
```

---

## Prerequisites

Read before starting:
1. .context/CONTEXT.md
2. .context/skills/chart-patterns.md
3. .context/skills/design-tokens.md
4. .context/skills/section-inventory.md

---

## The Core Question

For every number, chart, and metric in the app, ask:

1. **So what?** Does this number tell the user what to do next?
2. **Compared to what?** Is this number shown in context or isolation?
3. **Why?** Does the user know what caused this number to change?
4. **Is this the right number?** Or is it the easy number to compute?
5. **What's missing?** What correlation or connection would make this more useful?

---

## Phase 1 — Number Audit

**Goal:** Every number shown to the user has context, units, and meaning.

For every numeric value displayed across all screens, check:

CONTEXT
- Is there a comparison? (vs last month, vs threshold, vs average)
- Is there a trend direction? (up, down, stable)
- Is there a benchmark? (what's normal for this business)
- If it's a standalone number with no context — flag it

UNITS AND FORMATTING
- Is the unit clear? ($, days, %, count)
- Are large numbers formatted for readability? ($12,400 not $12400.00)
- Are negative numbers visually distinct from positive? (color, sign)
- Are percentages clearly percentages and not decimals?
- Are "days" numbers rounded to whole numbers? (42 days not 41.7)
- Are money values consistent decimal places? (always 2, never mixed)

MEANING
- Does the label explain what it measures?
- Could a non-accountant understand it without documentation?
- Is there a tooltip, caption, or sub-label that explains unusual metrics?
- Is the health score's weighted formula visible or explainable on demand?

Flag as:
- ORPHAN — number with no comparison or context
- UNCLEAR — unit, label, or meaning is ambiguous
- MISLEADING — number is technically correct but implies wrong thing
- MISSING — a number that should exist here but doesn't

Output: **Number Issues List** — screen, element, issue type, description

---

## Phase 2 — Chart Audit

**Goal:** Every chart answers a specific question, not just displays data.

For every chart across all screens:

PURPOSE
- What question does this chart answer?
- Is that question one the user actually has on this screen?
- Would a table communicate this better than a chart?
- Would a single number communicate this better than a chart?

CHART TYPE FIT
- sparkline: used only for trend over time with multiple data points?
- mini_bars: used only for dashboard tile quick trends?
- horizontal_bar_chart: items in ranked order by magnitude?
- funnel_chart: genuinely sequential stages where each feeds the next?
- gauge: used only for runway — nothing else?
- Is any chart type being stretched beyond its intended use?

READABILITY
- Can the user read the values without hovering?
- Are axis labels present where needed?
- Is the zero baseline shown when negative values are possible?
- Are colors semantically meaningful? (green=good, red=bad, consistent)
- Is the chart readable at 375px mobile width?
- Is the chart readable at the narrowest desktop window?
- Do chart colors match the design token meanings?
  (--income for income, --expense for expense, --warn for warnings)

EMPTY AND EDGE STATES
- What does the chart show with 1 data point?
- What does it show with 0 data points?
- What does it show with identical values (flat line)?
- What does it show with extreme outliers?

MISSING CHARTS
- Is there data on this screen that would be clearer as a chart
  but is currently shown as a table or plain number?
- Specifically flag: any table with more than 5 rows that has
  a magnitude column — that's a horizontal_bar_chart candidate

Output: **Chart Issues List** — screen, chart, issue type, description

---

## Phase 3 — Correlation and Connection Audit

**Goal:** Data that is related should be shown as related.

This is the highest-value phase. Most finance apps show data in silos —
the user has to mentally connect the dots. Klearing should connect them.

For each screen, identify cross-screen data relationships that exist
in the DB but are not surfaced to the user:

HOME DASHBOARD
- Does the Needs Review count connect to what's causing the reconciliation residual?
- Does the vendor spend jump connect to the cash runway change?
- Does the health score change connect to which specific factor changed?
- Is the "biggest risk right now" actually the biggest risk, or just the most recent flag?

RECONCILE
- Does the residual amount connect to specific pending items that explain it?
- Does reconciliation history show whether this is a recurring pattern?
- When flagged, does the page suggest the most likely cause based on
  the actual pending queue (not static boilerplate)?

VENDORS
- Does a vendor spend jump connect to the reconciliation residual?
- Does a new subscription detection connect to the cash runway impact?
- Does the lifetime spend on a vendor connect to their current month anomaly?

SUMMARY
- Does the net change connect to which specific category drove it?
- Does a bad month connect to which vendor or category was the outlier?
- Does the prior period comparison explain WHY not just HOW MUCH?

INSIGHTS
- Are anomalies cross-referenced with their source data?
- Does "spending more on X" link to the vendor detail page?
- Are patterns identified across multiple months, not just point-in-time?

GROWTH
- Does margin change connect to which expense category changed?
- Does Stripe revenue connect to invoice status (are invoices being collected)?
- Does labor cost (Gusto) connect to revenue per headcount?

CASH POSITION / RUNWAY
- Does runway connect to which specific expense is the biggest drain?
- Does "safe to spend" connect to what's in the pending queue?
- Does runway change month-over-month get explained?

For each missing connection, assess:
- Is the data already in the DB to support this connection?
- Is this a display change (low effort) or a new query (medium effort)?
- Would the AI assistant surface this naturally, or does it need
  to be hardcoded in the UI?

Flag as:
- DISPLAY — data exists, just not shown together (low effort)
- QUERY — needs a new DB query but data is available (medium effort)
- MISSING DATA — data doesn't exist yet, would need new collection (high effort)

Output: **Correlation Gaps List** — screens involved, what's missing,
data availability, effort level

---

## Phase 4 — Reasoning and Explanation Audit

**Goal:** When the app flags something, it explains why.

For every flagged state, warning, or anomaly in the app:

CHECK
- Does the flag explain the reason in plain English?
- Does the explanation name the specific data that triggered it?
- Does it distinguish between "definitely a problem" and "might be worth checking"?
- Does it suggest a next action?
- Is the explanation dynamic (based on actual data) or static boilerplate?

HEALTH SCORE
- Is each factor's contribution to the score visible?
- When the score changes, does the user know which factor changed?
- Is the weighting of each factor documented somewhere accessible?
- Can the user see "your score would be X if Y were resolved"?

ANOMALY DETECTION
- Vendor spend jump: does it say "AWS jumped 62% because last month
  was unusually low ($120 vs typical $340)" or just "62% jump"?
- Subscription detection: does it say how confident it is?
- Reconciliation flag: does it distinguish between "pending explains it"
  vs "unexplained residual"?

EMPTY STATES
- Do empty states explain why something is empty?
  "No vendors this month" vs "No vendors — connect a bank account
  or import a CSV to see vendor analysis"
- Do they suggest what to do next?

Flag as:
- STATIC — boilerplate explanation not tied to actual data
- MISSING — flag exists but no explanation at all
- VAGUE — explanation present but doesn't name the specific cause
- GOOD — explanation is specific, dynamic, and actionable

Output: **Reasoning Issues List** — screen, element, issue type,
current text, what it should say

---

## Phase 5 — Data Density Audit

**Goal:** Screens show the right amount of data — not too much, not too little.

For each screen:

TOO SPARSE
- Is there a screen that shows fewer than 3 meaningful data points?
- Is there a screen where the user would immediately want more detail
  but can't get it?
- Are there metrics that exist in the DB but aren't surfaced anywhere?

TOO DENSE
- Is there a screen where the user would be overwhelmed on first visit?
- Are there metrics shown that the user almost never needs?
- Are there tables with too many columns to read at a glance?

PROGRESSIVE DISCLOSURE
- Does the screen show the most important information first?
- Is detail available on demand (drill-down) rather than always visible?
- Is the hierarchy clear: summary → detail → raw data?

FIRST-TIME USER VS POWER USER
- Does a new user with 1 month of data see a useful screen?
- Does a user with 2 years of data get more value than a new user?
- Are there screens that only become useful after N months of data?
  If so, is there a useful state for users who don't have that history yet?

Output: **Data Density Issues List** — screen, issue type, description

---

## Phase 6 — AI Assistant Data Coverage Audit

**Goal:** The AI assistant can answer the questions users actually ask.

Pull the 6 tool definitions from ollama_client.py and test each:

For each tool, ask: can the AI answer these questions using this tool?

get_cash_position:
- "How long can I keep operating at current burn?"
- "Can I afford to hire a contractor next month?"
- "What's the minimum I need to invoice this month to cover expenses?"

get_reconciliation_data:
- "Why is my reconciliation flagged?"
- "Is this residual amount normal for me?"
- "Which pending items are probably causing this?"

get_vendor_summary:
- "Which vendor should I review this month?"
- "Am I paying for something I forgot about?"
- "Which of my subscriptions has the biggest impact on runway?"

get_summary_data:
- "Was this month better or worse than last month?"
- "What drove the change in my net income?"
- "Am I trending in the right direction?"

get_flagged_items:
- "What do I need to deal with today?"
- "Is anything urgent right now?"
- "What would my accountant flag if they looked at this?"

get_subscription_list:
- "What am I paying monthly that I might not need?"
- "What's my total monthly subscription burn?"
- "Which subscription has been running the longest?"

For each question the tool CANNOT answer with its current data,
flag whether:
- The data exists in the DB and just needs to be added to the tool
- The data doesn't exist and would need new collection

Output: **AI Coverage Gaps List** — tool, question, what's missing

---

## Output Format

After all 6 phases, produce a single consolidated report:

### Klearing Data Intelligence Audit — [date]

**Summary:**
- Phase 1 Number Audit: X orphans, X unclear, X misleading, X missing
- Phase 2 Chart Audit: X purpose issues, X type issues, X readability issues
- Phase 3 Correlation Gaps: X display, X query, X missing data
- Phase 4 Reasoning Issues: X static, X missing, X vague
- Phase 5 Data Density: X too sparse, X too dense, X disclosure issues
- Phase 6 AI Coverage: X questions unanswerable

**Top 10 Highest-Impact Improvements:**
Ranked by: (user value) × (implementation ease)

1. [improvement] — [screen] — [effort: low/medium/high]
2. ...

**Quick Wins (display changes only, no new queries):**
- [improvement] — [screen] — [what to change]

**Correlation Opportunities (data exists, just not connected):**
- [connection] — [screens involved] — [what query needed]

**Needs New Data (collect before building):**
- [metric] — [why valuable] — [what would capture it]

---

## After the Audit

Do not fix anything during the audit run.
Bring the top 10 list back for approval.
Quick wins go first — they have the highest ROI.
Correlation improvements go second.
New data collection goes last.

Fixes branch: feat/data-intelligence-[date]
Update anti-patterns.md with any new data presentation patterns found.
Run python .context/sync.py after updating skill files.
