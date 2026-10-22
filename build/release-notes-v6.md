## Klearing 6.1

This release includes the full Desk visual redesign merged in PR #74, plus the native ledger reliability fixes that shipped alongside it.

## Highlights

- Full eight-phase Desk visual redesign: typography, color, spacing, elevation, component consolidation, navigation, palette, bulk review, briefing/disclosure patterns, and responsive layout.
- Native ledger reliability fixes: Plaid deposits route to the correct revenue/income accounts, transfers post through the appropriate paired/clearing entries, and Shopify inventory and COGS handling avoids inventory drift.
- Windows package now ships the redesigned frontend, all four bundled typefaces, and the complete migration set.

## Known open items

- Shopify COGS is intentionally not posted for sales whose inventory acquisition/cost data is unavailable; those orders are recorded as skipped so the ledger is not misstated.
- The responsive layout above the 1100px breakpoint still needs broader device/browser verification.

## Installation

Download `Klearing-windows.zip` and unzip it, or download `Klearing.exe` directly. Windows may show a SmartScreen prompt because this build is unsigned.
