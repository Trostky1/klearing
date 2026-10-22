# Klearing Full-App Audit Skill

Load this skill to run a cross-cutting audit of the entire Klearing codebase.
This is not a per-section visual audit — it finds issues that only appear
when you look at the whole app at once.

Run after every major release. Run before any public launch.

---

## How to Invoke

```
Read .context/skills/klearing-audit.md and run a full app audit.
Complete every phase in order. Do not fix anything — report only.
Output a single prioritized fix list at the end.
```

---

## Prerequisites

Before starting any phase, read:
1. .context/CONTEXT.md
2. .context/skills/anti-patterns.md
3. .context/skills/section-inventory.md
4. .context/skills/design-tokens.md

---

## Phase 1 — Route Integrity

**Goal:** Every link in the app goes somewhere real.

Checks:
- Every `href` in every render function resolves to a real route in app.py
- Every "open in Settings", "go to Pending", "go to Connections" link
  points to the correct path
- Every Tools hub card links to a real sub-page
- No 404s reachable from normal navigation
- Back-links on detail pages (vendor detail, audit, etc.) resolve correctly
- New screens from v0.4.2 (Data Health, Accountant Export) are wired
  into the Tools hub and reachable

Output: **Broken Route List** — path, source location, what it links to

---

## Phase 2 — Cross-Screen Consistency

**Goal:** Patterns that exist on one screen exist on all screens where they apply.

Checks:
- Every screen that can have zero data shows a calm fallback state
  (no screen vanishes, no blank table, no unhandled empty)
- Every screen that references another section links to it
  (no plain-text "go to Pending" without an anchor tag)
- Every render function accepts message/error params and forwards
  them to render_shell — no orphan functions that can't show banners
- Every detail/drill-down page has a working back-link
- confirm_onsubmit() used everywhere — no inline confirm() strings
- Monospace font applied consistently: digits/money only,
  never on mixed word+number phrases
- Every card that claims to open a tab is wrapped in an anchor tag
- "Cloud AI active" warning pill appears on every page when a cloud
  provider is active, not just the chat panel

Output: **Consistency Issues List** — screen, pattern, what's missing

---

## Phase 3 — Deferred Items Scan

**Goal:** Find deferred items from section-inventory.md that are now unblocked.

For each deferred item:
- Is the blocking dependency now resolved?
- Did v0.4.2 ship something that makes this easier or cheaper to build?
- Is it still relevant or has the product moved past it?

Flag as:
- UNBLOCKED — can build now, dependency resolved
- STILL BLOCKED — dependency unchanged
- OBSOLETE — product has moved past this, remove from list

Output: **Deferred Items Status Table**

---

## Phase 4 — Regression Coverage Audit

**Goal:** Every screen and feature has at least one test.

Checks:
- List every render function in app.py
- Cross-reference against existing test files
- Flag any render function with zero test coverage
- Flag any new feature from v0.4.0-v0.4.2 with no corresponding test
- Flag any test that references a route or function that no longer exists
- Check that the port 8642 rule is enforced — no test probes the live port

Output: **Coverage Gap List** — function name, what's missing

---

## Phase 5 — Design Token Drift

**Goal:** No hardcoded colors, no unauthorized font sizes, no rogue spacing.

Checks:
- Grep entire codebase for hex values not in design-tokens.md
- Grep for font-size values not in the --fs-* scale
- Check that ACCENT_HEX is still the single source for #4ADE80
- Check that all 6 panel selectors still compose from .panel base rule
- Check that .btn-link still composes from the base button rule
- Check that new screens added since #36 follow the shared patterns
- Check charts.py for any new hardcoded hex introduced since the
  ACCENT_HEX consolidation

Output: **Token Drift List** — file, line, value, what it should be

---

## Phase 6 — AI Tool Accuracy

**Goal:** The AI assistant's 6 tools return accurate, current data.

Checks:
- get_reconciliation_data() — verify it reads from the correct tables
  and returns all fields defined in ollama_client.py
- get_vendor_summary() — verify vendor list matches actual vendor data,
  jump detection uses correct threshold from settings
- get_cash_position() — verify safe_to_spend calculation is correct
  (cash minus pending_out)
- get_summary_data() — verify prior period window matches Summary page logic
- get_subscription_list() — verify it reads from the same source as
  the Vendors subscription section
- get_flagged_items() — verify it catches all flagged states across
  all sections (reconcile, vendors, invoices, follow-ups, Stripe in-transit)
- write_memory() — verify it writes to agent_memory table correctly
  and that daily_insight key is populated by check_bank.py

Output: **AI Tool Issues List** — tool name, field, issue

---

## Phase 7 — Integration Health

**Goal:** Every integration degrades gracefully when offline or unconfigured.

For each integration (Wave, Plaid/bank, Stripe, Gusto, Gmail,
GA4, Klaviyo, Ollama, OpenAI, Anthropic):
- Is the service gated before use (only runs if configured)?
- Does a failure write to error.log without crashing the daily check?
- Does the UI show a calm "not connected" state rather than an error?
- Is the last-tested timestamp updated after each check?
- Does the Connections page accurately reflect current status?
- Are rate limit errors caught explicitly and retried next run?

Output: **Integration Issues List** — service, issue type, severity

---

## Phase 8 — Anti-Pattern Recurrence Scan

**Goal:** Known anti-patterns haven't reappeared in new code.

For each anti-pattern in anti-patterns.md, grep the codebase:

1. Unlinked references to Settings/Pending/Connections in copy
2. Sections that vanish when empty instead of showing fallback
3. Monospace on mixed word+number phrases
4. Hardcoded hex colors (covered by Phase 5 but check HTML too)
5. Inline confirm() strings instead of confirm_onsubmit()
6. Parent-scoped classes reused outside their host container
7. Clicks or submits that silently do nothing
8. Unbounded tables without row cap and "showing N of M" note
9. One word used with two meanings on the same page
10. Cloud AI sending data without explicit consent dialog
11. Any new per-container link style instead of the base :where() rule
12. Any new font-size literal instead of --fs-* token
13. Any new card variant not composing from .panel base

Output: **Recurrence List** — anti-pattern number, file, line, description

---

## Phase 9 — Launch Readiness Check

**Goal:** Is the app ready for public launch?

Checks:
- README.md accurately describes the product without overclaiming privacy
- PRODUCT.md has the accurate data/privacy section
- No copy says "no data leaves the machine" while integrations are active
- Policy pages exist or are planned (privacy policy, terms)
- First-run wizard covers all required setup steps
- Error messages are user-friendly (no raw Python exceptions exposed)
- All external links in the app are valid and current
- Version number in the app matches latest GitHub release (v0.4.2)
- docs/FIRST-LAUNCH.md is current for both Windows and macOS

Output: **Launch Blockers List** — item, severity (blocker/warning/nice-to-have)

---

## Output Format

After all 9 phases, produce a single consolidated report:

### Klearing Full-App Audit — [date]

**Summary:**
- Phase 1 Route Integrity: X issues
- Phase 2 Cross-Screen Consistency: X issues
- Phase 3 Deferred Items: X unblocked, X still blocked, X obsolete
- Phase 4 Regression Coverage: X gaps
- Phase 5 Design Token Drift: X issues
- Phase 6 AI Tool Accuracy: X issues
- Phase 7 Integration Health: X issues
- Phase 8 Anti-Pattern Recurrence: X issues
- Phase 9 Launch Readiness: X blockers, X warnings

**Prioritized Fix List:**
P1 — [issue] — [file/location] — [phase]
P2 — [issue] — [file/location] — [phase]
P3 — [issue] — [file/location] — [phase]

**Unblocked Deferred Items (ready to build):**
- [item] — [why unblocked]

**Launch Blockers:**
- [item] — [severity]

---

## After the Audit

Do not fix anything during the audit run.
Bring the prioritized fix list back for approval.
Fixes go on a branch feat/audit-fixes-[date].
Update anti-patterns.md with any new recurring patterns found.
Run python .context/sync.py after updating any skill files.
