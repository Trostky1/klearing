# Competitor benchmark reference

Research gathered during the Home/Dashboard section audit (July 2026).
Re-search a specific product if this reference is more than a few months
stale by the time you use it, or if the section under audit needs detail
not covered here — this is a starting point, not a permanent snapshot.

## The five benchmarked apps

**Monarch Money** — best overall UX, consumer net-worth/budgeting.
Customizable drag-and-drop dashboard widgets (net worth, recent
transactions, investment performance, upcoming expenses). Household view
(shows the whole household's picture, not just the logged-in user).
AI Assistant + "AI Insights" button on the budget widget. Shared goals
and credit-score monitoring for couples.

**YNAB** — best budgeting/transaction UI, zero-based budgeting. Dashboard
is explicitly an "action hub, not just a viewing surface" — category
balances, target tracking, progress bars, and a checklist-style
onboarding embedded right in the home screen. Separate reports section
for trend/overspend analysis.

**Copilot** — best data visualization, consumer spend tracking. "Free to
Spend" hero number at the top of the dashboard. Spending-progress bar for
the month. At-risk-category flags. Month-over-month and year-over-year
category breakdowns. Cash flow chart (income/spend/net over time).
Contextual tooltips introduce dashboard elements without overwhelming
new users.

**Tiller Money** — spreadsheet-native, power-user. Foundation Template
acts as an all-in-one dashboard: top 10 income sources, top 10 spend
sources, net worth, budget vs. actual — all auto-populated in Google
Sheets. "AutoCat" for fully customizable auto-categorization rules.

**Lunch Money** — minimalist, ad-free, closest philosophically to
Klearing's restrained register. Single bar chart (planned vs. actual
spend this month) as the primary visualization. Highly flexible manual
segmentation. "Stats and Trends" tab for deeper queries. No garish
colors, no clutter — the design principle most aligned with Klearing's
"tool disappears into the task" goal.

## Cross-cutting patterns (appear in 3+ of the 5)

- **Balance/net-worth trend over time**, not just a point-in-time number
  (Monarch, Tiller, Lunch Money)
- **Category spend breakdown visible on the home screen itself**, not
  behind a click (Copilot, YNAB, Tiller, Lunch Money)
- **Dashboard framed as an action hub**, not a read-only status page
  (YNAB explicitly, Copilot implicitly via at-risk flags)

## What Klearing has that none of the 5 have

- **Deterministic (non-AI) health score with weighted, explainable
  factors.** Monarch's answer to "how am I doing" is an AI Insights
  button; Klearing's is `health.py`'s plain-threshold composite. This is
  a real differentiator worth protecting, not converging toward AI.
- **Books reconciliation status** (Wave vs. bank agreement). None of the
  5 do double-entry reconciliation against a separate system of record —
  they're consumer budgeting apps, not small-business bookkeeping tools.
  This is Klearing's actual niche; don't chase consumer-app features that
  don't serve it.

## What to explicitly NOT chase

- **Widget customization / drag-to-reorder** (Monarch). Evaluated once
  for the Home dashboard and declined — it cuts against Klearing's
  deterministic, no-clutter, "beginner-friendly, one person, one
  machine" design register (see PRODUCT.md). Don't re-propose without a
  concrete reason the prior call was wrong for a specific new context.
- **AI-anything.** Klearing's whole positioning is "no AI, deterministic,
  plain thresholds on your real numbers" — visible in the health card's
  own copy. A competitor having an AI feature is not a gap to close.

## How to use this for a new section audit

Match the section under audit to the closest competitor surface (e.g.
Vendors ↔ Copilot's category breakdown + at-risk flags; Growth ↔
Copilot's cash-flow-over-time chart; Insights ↔ none of these 5 map
well, since it's GA4/marketing data, not personal finance — note that
explicitly rather than forcing a comparison that doesn't fit).
