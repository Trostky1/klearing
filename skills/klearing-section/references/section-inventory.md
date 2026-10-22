# Section inventory

Regenerated 2026-07-26 against the rebuilt app, at the end of the Desk
redesign. The previous version documented 21 render functions and a
12-item sidebar and predated Shopify, Ledger and Tax entirely — roughly a
third of the surface was missing from it, which is why it is regenerated
here rather than patched. If this file and `app.py` disagree, `app.py`
wins; update this in the same change.

Design rules live in `skills/klearing-ui/SKILL.md` and its references.
The redesign's own decisions are in
`docs/superpowers/specs/2026-07-26-visual-redesign-design.md`.

## Sidebar

Fifteen items in five groups (`ui.NAV_GROUPS`). Everything else is
reachable from the command palette (`Ctrl+K`, or `/` when focus is not in
a field), whose entries are `ui.PALETTE_ENTRIES`.

```
Home
Review          badge: total across all three queues
——— MONEY ———
Summary · Contacts · Invoices · Vendors · Reconcile · Shopify
——— BOOKS ———
Ledger · Inventory · Tax · Tools
——— UNDERSTAND ———
Insights · Growth
——— SYSTEM ———
Settings
```

The count is guarded by `scripts/ui_consolidation_regression.py`. Growing
it is allowed but must be argued for in that guard's comment — the point
is that a new top-level slot is a deliberate decision, not that the
sidebar can never change.

## Screens

| Route | Render function | What it is |
|---|---|---|
| `/` | `render_home_page` | Briefing (serif lead + facts) then 8 stat cards; health card fills async |
| `/review?tab=transactions` | `_review_transactions_content` | Approval queue as a selectable table, row cap 150, bulk approve/deny |
| `/review?tab=recurring` | `_review_recurring_content` | Recurring invoice cycles that came due, approve or skip |
| `/review?tab=followups` | `_review_followups_content` | Drafted lead follow-ups, send or skip |
| `/review?tab=invoices` | `_review_invoices_content` | Overdue invoice reminders, send or skip |
| `/uncategorized` | `render_uncategorized_page` | Approved items sitting in a catch-all category |
| `/summary` | `render_summary_page` | Date-ranged totals, category breakdowns, net trend |
| `/summary/category` | `render_category_detail_page` | One category's items, capped at 100 |
| `/invoices?tab=recurring` | `_recurring_templates_content` | Templates that bill on a schedule, pause/resume/delete |
| `/invoices/recurring/{id}` | `_recurring_template_editor` | One template: who, cadence, line items |
| `/contacts?tab=all` | `render_contacts_page` | Every contact with lifetime revenue and margin |
| `/contacts?tab=pipeline` | `render_contacts_page` | The same contacts grouped by pipeline stage |
| `/contacts/{id}` | `render_contact_detail` | One contact's orders, invoices, margin, stage, notes |
| `/vendors` | `render_vendors_page` | Spend by vendor with jump flags, subscriptions |
| `/vendors/detail` | `render_vendor_detail_page` | One vendor's history, per-item annotate |
| `/reconcile` | `render_reconcile_page` | Verdict + comparison chart; evidence in disclosures |
| `/shopify` | `render_shopify_page` | Orders, products, customers, fulfilment |
| `/shopify/order/{id}` | `shopify_order` | One order: totals + line-items table |
| `/shopify/product/{id}` | `shopify_product` | One product: stats + the orders containing it |
| `/ledger?tab=…` | `render_ledger_page` | Sync · Trial balance · Profit & Loss · Balance sheet · Accounts |
| `/tax?tab=…` | `render_tax_page` | Transactions · Statements · Projections · Planning |
| `/tools/data-health` | `render_data_health` | Sync gaps, manual OFX/QFX/CSV import |
| `/tools/accountant-export` | `render_accountant_export` | Month close package: PDF, CSVs, Notion |
| `/tools/unit-economics` | `render_unit_economics` | Cost/price calculator |
| `/tools/nexus` | `render_nexus` | Per-state revenue vs threshold |
| `/tools/contractors` | `render_contractors` | 1099 candidates |
| `/tools/audit` | `render_audit` | Decision trail, capped at 200 |
| `/insights?tab=…` | `render_insights_page` | Financial · Marketing |
| `/growth` | `render_growth_page` | Revenue trends, channels, efficiency |
| `/settings?tab=general` | `render_settings_page` | Thresholds, accounts, AI provider, backup |
| `/settings?tab=connections` | `render_connections_page` | 13 integrations; unconnected ones collapse |
| `/settings?tab=automation` | `render_automation_page` | Scheduler jobs |
| `/settings?tab=logs` | `render_logs_page` | app · daily · weekly · error |
| `/unlock` | `render_unlock_page` | App password, no sidebar |
| `/setup/*` | `setup_wizard.py` | 7-step first run, plus Plaid and GA4 sub-wizards |

## Moved routes

All 308 permanent redirects, query string preserved (`_MOVED` in
`app.py`). Do not remove these — they have been bookmarked and typed for
months, and a redesign has no business turning a known URL into a 404.

```
/pending      -> /review?tab=transactions
/followups    -> /review?tab=followups
/invoices     -> /review?tab=invoices
/connections  -> /settings?tab=connections
/automation   -> /settings?tab=automation
/logs         -> /settings?tab=logs
/tools        -> /tools/data-health
```

`render_tools_hub` was deleted: six static cards with no live data, a
menu pretending to be a dashboard.

## Shared components

Adding a screen means reusing these, not writing new ones. Each replaced
a set of near-duplicates, and the regression suite fails if a second
implementation appears.

| Component | Use |
|---|---|
| `ui.sub_nav(items, active, label)` | The one way to switch between sibling views |
| `.segmented` / `.range-toggle` | Filtering *within* a view — deliberately distinct from sub_nav |
| `ui.stat` / `.stat-grid` / `.stat-cell` | The one stat vocabulary |
| `ui.empty_state(...)` / `.none-note` | The only two emptiness registers |
| `ui.disclosure(summary, body, note)` | Supporting evidence, collapsed by default |
| `ui.confirm_attrs(...)` | Opens the styled dialog. `confirm_onsubmit` survives only where one form is shared by several buttons |
| `<th data-sort="num\|text\|date">` | Makes a column sortable |
| `ui.table_head(cols)` + `ui.cell_label(cols, i)` | Declares a table's columns once, so a card-reflowed cell's `data-label` is the header's own string |
| `.card-reflow` on a `<table>` | Opt-in: rows become labelled cards at ≤640px instead of scrolling sideways |
| `data-select="#bar-id"` + `data-bulk-name` | Row selection and bulk submit |
| `.doc-title` | Instrument Serif — statement titles only, never body or tables |

Client behaviour is all in `frontend/app/table.js`: sorting, selection,
dialogs, command palette. It is progressive enhancement — every page
still works if it fails to load.

## Conventions worth not relearning

- Mutations are POST → 303 → `?msg=`/`?err=` banner. `_make_redirect`
  picks `?` or `&` because several bases now carry a tab.
- Numeric table columns get `class="num"`; a word-bearing one adds
  `.mixed` to opt out of the mono stack.
- A conditionally-rendered card shows a calm all-clear when checked and
  fine, rather than vanishing.
- Row caps come with an honest overflow note stating the true total.
- **Tables at ≤640px: scroll by default, cards by exception.** Horizontal
  scroll stays right for *scanning* tables, where the column is the point
  — you compare revenue down Contacts, or spot the low item down Stock on
  hand, and stacking those puts each number a screen apart. `.card-reflow`
  is for *record* tables, where a row is one thing you inspect and the
  figure you want is the rightmost column and therefore always off-screen
  (Invoices' Outstanding). Applied to Invoices and Recurring templates
  only; adding a third means deciding which kind of table it is first.
  `thead` stays visible as a sort bar rather than being hidden — sorting
  lives entirely in those cells, and this app has no browser chrome and
  no Ctrl-F.
- Bulk selection exists on the Transactions queue only. Payments,
  Recurring, Follow-ups and Wave reminders all decide one row at a time,
  deliberately — `/review/approve` is transaction-shaped (categories, the
  Wave duplicate guard), and money leaving the building is the last place
  to make approving many at once frictionless.
- A figure the books never stated is rendered `&mdash;`, never `0.00`.
  Contacts' margin column is the worked example: cost of goods is
  withheld entirely for an order with an uncostable line, so margin
  covers only the costed subset and says how many that is.
