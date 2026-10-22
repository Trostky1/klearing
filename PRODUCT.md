# Klearing

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Solo and small-business owners doing their own bookkeeping on one local
machine. The product should be beginner-friendly, require no terminal use,
and make business information understandable without specialist accounting
knowledge.

## Product Purpose

Klearing is locally run bookkeeping software for small to medium businesses.
It brings business information into one place so owners can more easily view
the state of their business and streamline bookkeeping, projections, taxes,
and related operational work.

## Positioning

Klearing combines local-first bookkeeping with a broad, understandable view
of the business: review queues, financial reporting, projections, tax
workflows, and optional integrations live together while core data remains on
the owner's machine.

## Operating Context

Klearing runs as a packaged local web application at `localhost:8642`. Users
connect optional services, review or edit incoming work, approve or skip it,
and inspect reports and business insights. The first-run Setup flow guides
connections; the app is distributed as an unsigned Windows executable or
macOS app.

## Capabilities and Constraints

- Bank transactions, follow-ups, invoice reminders, reports, projections,
  tax workflows, and business insights are available in one application.
- Incoming work lands in review queues; nothing is silently posted or sent.
- Core transaction data, reports, exports, and AI context stay local.
- External integrations are opt-in and may include Plaid, Stripe, Gusto,
  Gmail, GA4, Klaviyo, OpenAI, Anthropic, and Ollama.
- The web UI is server-rendered HTML with plain CSS and vanilla JavaScript;
  there is no frontend build step.

## Brand Commitments

- The product name is Klearing.
- The experience should be clear, practical, and approachable for owners who
  manage their own books.

## Evidence on Hand

- Existing review, reporting, ledger, projection, tax, integration, and
  export workflows in the repository.
- Packaged Windows and macOS builds documented in `README.md`.

## Product Principles

- Keep core business data local by default.
- Make the full business picture easier to access and understand.
- Turn bookkeeping, projection, and tax work into clear, reviewable actions.

## Accessibility & Inclusion

Accessibility is a durable product commitment. UI work must preserve clear
hierarchy, readable content, keyboard-usable controls, meaningful labels and
states, sufficient contrast, and responsive use across supported viewport
sizes.

## Data & Privacy

Core data—transactions, categories, reports, AI analysis, exports, and
business health information—lives on the owner's machine. Integrations are
opt-in, and the product must clearly communicate what data leaves the device,
where it goes, and when it is used.

## Surfaces

Setup and its connection subflows; review queues; Summary; Vendors; Insights;
Ledger; Projections; Tax; Follow-ups; Invoices; Logs; and Settings, including
connection and automation controls.
