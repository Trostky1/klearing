# Wave and HoneyBook redesign map

Goal: borrow layout patterns from Wave and HoneyBook. Keep Klearing colors
(dark base, green accent). Keep every route, form field, and button wiring.

Rules for every batch:
- Change markup and CSS only. Do not rename routes, form names, ids, or
  `data-` hooks that `frontend/` scripts or tests read.
- Colors come from `:root` tokens in `ui.py`. No new hex values.
- No tracked-capital labels, no eyebrows, no cards inside cards.
- After each batch: run the regression scripts in `scripts/`, then compare
  before and after screenshots.

## Batches

| Batch | Klearing screens | Wave pattern | HoneyBook pattern |
|---|---|---|---|
| 1 Shell | sidebar, page header, `Create new` button | grouped nav, `Create new` menu | dark rail, search + New in top bar |
| 2 Money in | Invoices, invoice detail, Contacts, contact detail | stat strip, status pills, Create / Send / Get paid steps, customer tabs | project detail tabs, right info panel |
| 3 Books | Review queue, Ledger, Vendors, Bank vs books | select-and-act table bar, filter chips | none |
| 4 Reports | Home, Summary, Insights, Tax | cash flow chart, payable and owing table, report cards | finance overview tabs |
| 5 Pipeline | CRM pipeline, follow-ups | none | kanban board |
| 6 System | Settings, Connections, Automation, Setup | none | integration card grid, settings side tabs |

## Reference screens (Mobbin)

Wave: Transactions with bulk bar, Invoices with stat strip, invoice detail
steps, Customer invoices tab, Dashboard cash flow, Reports index, Cash Flow
report, Advisor side bar.

HoneyBook: Projects kanban, project Financials tab, Files list, Finance
overview, Contacts, Settings integrations, invoice builder panels.
