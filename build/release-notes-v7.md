## Klearing 7

This release covers pre-release hardening for the first build meant to leave this machine, plus everything merged into main since 6.1: native-ledger tax canonicalization, state income tax support, inventory reconciliation, and a customizable Home Insights widget.

## Highlights

- Pre-release hardening: the app now binds to 127.0.0.1 only in both native-window and headless server modes (no more LAN-reachable dev binding), and every request is validated against an exact Host allowlist (`localhost`/`127.0.0.1` on the configured port), closing a DNS-rebinding gap that could defeat the session cookie's `samesite=lax` protection.
- Native ledger is now canonical for tax: projections, exports, and the tax workspace read from the ledger instead of the legacy Wave queue, with contractor (Feature C) handoff preserved.
- State income tax support alongside the existing federal projection.
- Inventory acquisition reconciliation: legacy quantity-only acquisitions can now be posted against a funding account or explicitly marked unresolvable, through the same Review drawer used elsewhere.
- New customizable Home Insights widget: pin the cross-source metrics you care about (Shopify revenue, bank cash position and top expense, inventory FIFO value, payout matching, margin) with deep links back to the real source platform. Read-only, no network calls beyond what's already synced locally.
- Command palette / nav entries fixed and regression-guarded (42 entries checked) after a stale Tax legacy-archive link was found during release verification.
- CI now attaches both platform packages to the release atomically, instead of each OS's build racing to attach its own artifact independently.

## Known open items

- Shopify COGS is intentionally not posted for sales whose inventory acquisition/cost data is unavailable; those orders are recorded as skipped so the ledger is not misstated.
- The responsive layout above the 1100px breakpoint still needs broader device/browser verification.
- The Home Insights widget ships with strong regression coverage but has not yet been exercised against a live Shopify/Plaid-connected install.

## Installation

Download `Klearing-windows.zip` and unzip it, or download `Klearing.exe` directly. Windows may show a SmartScreen prompt because this build is unsigned.
