"""
Regression suite for the color/panel/button/typescale consolidation and
setup-wizard ARIA changes (PR #36, branch feat/ui-consolidation).

Static/import-level checks against ui.py and setup_wizard.py — no
server, no DB, no network, no credentials. Safe to run in CI or a
fresh checkout, same spirit as scripts/plaid_sandbox_smoke.py but with
no live dependency to skip: every check here always runs.

Run it:

    venv/Scripts/python scripts/ui_consolidation_regression.py   # Windows
    venv/bin/python scripts/ui_consolidation_regression.py       # macOS/Linux

Exits 0 if every check passes, 1 if any fails (prints which).
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import setup_wizard  # noqa: E402
import ui  # noqa: E402

PANEL_SELECTORS = [".card", ".section", ".fu-card", ".accounts li",
                   "a.dash-card", ".health-card"]
FS_STEPS = ["xs", "sm", "base", "md", "lg", "xl"]

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def _own_rule_bodies(selector, style, group_body):
    """All of the selector's OWN standalone rule bodies (base rule plus
    any legitimate responsive overrides, e.g. '.card' also gets a
    '@media (max-width: 640px) { .card { gap: 12px; } }' override) —
    excluding the shared group rule it composes from. A negative
    lookahead excludes near-miss selectors like .card-main.

    .health-card is the LAST selector in the group list, so it sits
    directly before the group's own '{' — the same regex that finds
    '.card {' also matches that spot. Filter out any match whose body
    is exactly the group's body (the tell that it's the group match,
    not a real standalone rule for this selector)."""
    pattern = re.escape(selector) + r"(?![\w-])\s*\{([^{}]*)\}"
    matches = re.findall(pattern, style)
    return [body for body in matches if body != group_body]


# --------------------------------------------------------- Pass 1: panel


def check_panel_selectors_share_base_rule():
    style = ui.STYLE
    group_pattern = (
        r"\.card,\s*\.section,\s*\.fu-card,\s*\.accounts li,\s*"
        r"a\.dash-card,\s*\.health-card\s*\{([^{}]*)\}"
    )
    group_match = re.search(group_pattern, style)
    check(
        "shared .panel group rule exists with all 6 selectors",
        group_match is not None,
        "expected one rule for '.card, .section, .fu-card, .accounts li, "
        "a.dash-card, .health-card' — selector list or formatting changed",
    )
    if not group_match:
        return

    group_body = group_match.group(1)
    for prop in ("background: var(--surface)", "border: 1px solid var(--line)",
                 "box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04)"):
        check(f"shared rule declares {prop!r}", prop in group_body)

    for selector in PANEL_SELECTORS:
        own_bodies = _own_rule_bodies(selector, style, group_body)
        check(f"{selector} has its own (non-shared) rule", len(own_bodies) >= 1,
              "expected at least one standalone rule beyond the shared group")
        for own_body in own_bodies:
            for leaked_prop in ("background: var(--surface)",
                                 "border: 1px solid var(--line)",
                                 "box-shadow: inset"):
                check(
                    f"{selector} does not redundantly restate {leaked_prop!r}",
                    leaked_prop not in own_body,
                    "found a duplicate of the shared property — panel rule "
                    "consolidation has regressed",
                )


def check_hint_is_not_parent_scoped():
    """`.hint` must style itself, not depend on a form-field ancestor.

    It shipped as only `.field .hint` and `.cred-field .hint`, so 24 of
    73 usages -- every hint on Contacts, Inventory, Recurring invoices
    and Tools -- silently rendered as full-size body ink. The class name
    reads generic and autocomplete offers it, which is exactly why it
    got reused outside a form field; nothing errored, the page just
    rendered wrong.

    Guarding the shape rather than the values: a bare rule must exist,
    and re-scoping it under a parent must fail here rather than in
    somebody's eyes six screens later.
    """
    style = ui.STYLE
    bare = re.search(r"(?:^|\n)\s*\.hint\s*\{([^{}]*)\}", style)
    check(".hint has a parent-agnostic rule", bare is not None,
          "expected a standalone '.hint { ... }' — if it has been "
          "re-scoped under .field/.cred-field, every hint outside a form "
          "field is unstyled again")
    if bare:
        for prop in ("color: var(--muted)", "font-size: var(--fs-sm)"):
            check(f".hint declares {prop!r}", prop in bare.group(1))
    for scoped in (".field .hint", ".cred-field .hint"):
        check(f"{scoped} is not reintroduced",
              re.search(r"(?:^|\n)\s*" + re.escape(scoped) + r"\s*\{",
                        style) is None,
              "a parent-scoped copy is back; it makes the bare rule look "
              "optional and invites the same silent failure")


def check_nav_groups_are_announced():
    """Sidebar groups must be groups to a screen reader, not loose text.

    The visual grouping shipped in Phase 4 as a bare
    `<div class="nav-group">Money</div>` sitting above some links. It was
    never aria-hidden -- the audit note claiming that was wrong against
    the shipped code -- but it had no role and no id either, so nothing
    tied it to the links it labels. Anyone tabbing link to link, which is
    how navigation is actually used, never encountered it.

    Both renderings are checked because render_sidebar and
    render_mobile_nav put every group into the same document, so the
    generated ids have to stay unique across the pair.
    """
    html = ui.render_sidebar("Contacts") + ui.render_mobile_nav("Contacts")

    ids = re.findall(r'class="nav-group" id="([^"]+)"', html)
    refs = re.findall(r'aria-labelledby="([^"]+)"', html)
    check("every nav group label carries an id",
          len(ids) == html.count('class="nav-group"'),
          f"{len(ids)} ids for {html.count('class=' + chr(34) + 'nav-group' + chr(34))} labels")
    check("nav group ids are unique across both renderings",
          len(ids) == len(set(ids)),
          f"duplicates: {sorted({i for i in ids if ids.count(i) > 1})}")
    check("every aria-labelledby resolves to a rendered id",
          bool(refs) and all(ref in ids for ref in refs),
          f"unresolved: {sorted(set(refs) - set(ids))}")
    check("each group label is wrapped in a role=group",
          html.count('role="group"') == len(ids),
          f'{html.count(chr(34).join(["role=", "group", ""]))} groups for {len(ids)} labels')

    for landmark in ('<nav class="side-nav" aria-label="Sections">',
                     'aria-label="More destinations"',
                     'aria-label="Primary"'):
        check(f"nav landmark is named: {landmark[:40]}", landmark in html)

    check_group_wrapper_wins_the_cascade()


def _declarations_for(selector, style):
    """Every rule body in `style` whose selector list contains `selector`,
    in source order.

    Deliberately crude — a real CSS parser is not worth carrying for one
    assertion — but it is order-preserving, which is the whole point: the
    thing being guarded is *which declaration wins*, and that is decided
    by position among equal-specificity rules.
    """
    bodies = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
        selectors = match.group(1)
        # Strip any @media/@supports preamble clinging to the front of
        # the selector list so `.side-nav [role="group"]` still matches
        # when it appears inside an at-rule.
        selectors = selectors.rsplit("}", 1)[-1]
        if selector in selectors:
            bodies.append(match.group(2))
    return bodies


def check_group_wrapper_wins_the_cascade():
    """The group wrapper must still be a flex column *after* the cascade.

    This is a near-miss worth guarding permanently. Wrapping the links in
    a div to carry role="group" made the wrapper default to display:block,
    which stops the rail's flex `gap` applying between links inside a
    group -- they closed up to 2px and -1px instead of a uniform 3px.
    Every suite passed. Only measuring the rendered spacing caught it.

    The obvious check -- asserting the corrected rule appears in ui.STYLE
    -- cannot catch the realistic regression, because a later rule
    re-targeting the same selector wins on source order while leaving the
    original substring sitting there intact. A substring test would
    report PASS on a stylesheet that renders the original bug.

    So assert the *last* declaration of each property, which is what the
    browser actually applies.
    """
    selector = '.side-nav [role="group"]'
    bodies = _declarations_for(selector, ui.STYLE)
    check(f"{selector} is declared at all", bool(bodies),
          "the group wrapper has no rule; it will default to display:block")
    if not bodies:
        return

    # Last value wins among equal-specificity rules, so read right to left.
    def winning(prop):
        for body in reversed(bodies):
            found = re.findall(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
            if found:
                return found[-1].strip()
        return None

    for prop, expected in (("display", "flex"),
                           ("flex-direction", "column"),
                           ("gap", "3px")):
        actual = winning(prop)
        check(f"{selector} resolves to {prop}: {expected}",
              actual == expected,
              f"last declaration is {prop}: {actual!r} -- a later rule "
              f"overrides the fix and the links close up again")


def check_card_reflow_css():
    """The stylesheet half of card reflow.

    The markup half — that every reflowed cell carries its own column's
    name — is asserted in scripts/app_render_regression.py, which
    already has app and its fixtures loaded; this suite is deliberately
    DB-free.
    """
    style = ui.STYLE
    check("card reflow is scoped to the narrow breakpoint",
          style.index(".card-reflow, .card-reflow tbody")
          > style.index("@media (max-width: 640px)"),
          "the reflow must not apply at desktop width")
    check("thead survives card reflow so sorting survives with it",
          ".card-reflow thead tr {" in style
          and ".card-reflow thead { display: none" not in style,
          "sorting lives in thead; hiding it removes the only ordering "
          "tool on a surface with no Ctrl-F")
    check("an unlabelled column renders no label prefix",
          '.card-reflow tbody td[data-label=""]::before { content: none; }'
          in style)


# ------------------------------------------------------- Pass 1: accent


def check_accent_hex_single_source(ui_source):
    check(
        "ui.ACCENT_HEX equals the expected accent green",
        ui.ACCENT_HEX == "#4ADE80",
        f"got {ui.ACCENT_HEX!r}",
    )

    # This file itself legitimately needs the literal value to assert
    # against (it's the expected fixture value, not a second source of
    # truth) — exclude it from the "no literal elsewhere" scan.
    this_file = Path(__file__).resolve()
    py_files = [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(part in {"venv", "node_modules", "__pycache__", "build", "dist"}
                   for part in p.parts)
        and p.resolve() != this_file
    ]
    literal_sites = []
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "4ADE80" in line and "ACCENT_HEX = " not in line:
                literal_sites.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    check(
        "no literal #4ADE80 anywhere in the codebase except the "
        "ACCENT_HEX assignment",
        not literal_sites,
        f"found in: {', '.join(literal_sites)}" if literal_sites else "",
    )

    definitions = re.findall(r"ACCENT_HEX\s*=\s*[\"']#4ADE80[\"']", ui_source)
    check(
        "ACCENT_HEX is defined exactly once in ui.py",
        len(definitions) == 1,
        f"found {len(definitions)} definitions",
    )

    # Everything downstream must trace back to the constant, not a copy.
    check(".chart-funnel-1 references var(--accent), not a literal",
          ".chart-funnel-1 { fill: var(--accent); }" in ui.STYLE)
    for gradient_id in ("kl-bar-h", "kl-bar-v", "kl-area-green"):
        check(
            f"CHART_DEFS' {gradient_id} gradient uses the live accent value",
            f'stop-color="{ui.ACCENT_HEX}"' in ui.CHART_DEFS,
        )


# ---------------------------------------------------------- Pass 2: type


def check_fs_tokens_defined_and_referenced():
    for step in FS_STEPS:
        token = f"--fs-{step}"
        definitions = re.findall(re.escape(token) + r"\s*:", ui.STYLE)
        check(f"{token} is defined exactly once", len(definitions) == 1,
              f"found {len(definitions)} definitions")

        usages = len(re.findall(r"var\(" + re.escape(token) + r"\)", ui.STYLE))
        check(f"{token} is referenced at least once", usages >= 1,
              f"found {usages} usages")

    # Three of the four documented literal exceptions were retired by the
    # 2026-07-26 redesign, which widened the scale from 6 steps spanning
    # 10-20px to 9 spanning 11-56px. Folding them in was the point: they
    # existed because the old scale had no step near them, and now it
    # does. This suite previously asserted they stay literal — it now
    # asserts the opposite, which is the correct guard against a later
    # change quietly reintroducing off-scale sizes.
    retired = {
        ".main-inner > header h1 { font-size: var(--fs-xl);": "main app h1",
        ".dash-value {\n    font-size: var(--fs-xl);": ".dash-value",
        ".dash-mini-label { font-size: var(--fs-2xs); }": ".dash-mini-label",
    }
    for snippet, label in retired.items():
        check(f"{label} is on the type scale, not a literal",
              snippet in ui.STYLE)

    # The one survivor: its 88px circle constrains it below --fs-2xl, and
    # the component is slated for rebuild. It stays literal and stays
    # documented until then.
    check("health-grade digit remains the one documented literal exception",
          ".health-grade .g { font-size: 2.1rem;" in ui.STYLE)

    # No stray off-scale rem sizes anywhere else. The scale is only worth
    # having if nothing routes around it.
    scale = {"0.6875rem", "0.75rem", "0.8125rem", "0.875rem", "1rem",
             "1.25rem", "1.75rem", "2.5rem", "3.5rem", "2.1rem"}
    stray = {size for size in re.findall(r"font-size:\s*([\d.]+rem)", ui.STYLE)
             if size not in scale}
    check("no off-scale rem font sizes", not stray, f"stray: {sorted(stray)}")


# ------------------------------------------------------ Pass 3: wizard ARIA


def check_wizard_aria():
    steps_html = setup_wizard.render_steps_bar(3)
    check('render_steps_bar() emits exactly one aria-current="step"',
          steps_html.count('aria-current="step"') == 1)
    check("the aria-current step is the one actually marked 'now'",
          '<span class="step now" aria-current="step">3.' in steps_html)
    check("earlier steps get no aria-current",
          '<span class="step done">1.' in steps_html
          and '<span class="step done">2.' in steps_html)

    block_html = setup_wizard.test_block(
        "root-id", "/test-url", "<div>fields</div>", gate_id=None,
    )
    check('test_block() status span carries aria-live="polite"',
          '<span class="test-status" data-test-status aria-live="polite">'
          in block_html)

    # The two Plaid-specific status regions aren't behind test_block() —
    # source-check them directly (no live Plaid state needed to render).
    wizard_source = Path(setup_wizard.__file__).read_text(encoding="utf-8")
    check('plaid-link-status carries aria-live="polite"',
          'id="plaid-link-status" aria-live="polite"' in wizard_source)
    check('oauth-status carries aria-live="polite"',
          'id="oauth-status" aria-live="polite"' in wizard_source)


def check_polish_pass_tokens():
    """The 3 colors design-tokens.md used to list as a known untokenized
    gap (#FFFFFF, #FCD34D, #17171B) — promoted to --white, --delta-watch,
    --dash-card-hover during feat/polish-pass. Guards against the bare
    hex quietly creeping back in."""
    for hex_value, token in (("#FFFFFF", "--white"), ("#FCD34D", "--delta-watch"),
                             ("#17171B", "--dash-card-hover")):
        check(f"{token} is defined", f"{token}: {hex_value};" in ui.STYLE)
        # The token's own :root definition is the one legitimate literal —
        # every other use should go through var(--token) instead.
        total_uses = ui.STYLE.count(hex_value)
        check(f"{hex_value} appears only in the {token} definition, nowhere else",
              total_uses == 1, f"found {total_uses} literal occurrences")


def check_redesign_consolidation(app_source):
    """Phase 2/3 of the 2026-07-26 redesign.

    Each of these guards a consolidation that is easy to undo by accident:
    a new screen that hand-rolls its own sub-nav, a new inline style=, a
    fourth stat system. The point of the phase was that there is now one
    of each, so the regression is any second one appearing.
    """
    # One stat typography rule, not three.
    check("stat typography is one grouped rule",
          ".stat-value, .dash-value, .totals .value, .planning-result .value"
          in ui.STYLE)

    # One sub-nav idiom, plus the deliberately-separate segmented control.
    check(".subnav component exists", ".subnav {" in ui.STYLE)
    check(".segmented component exists", ".segmented, .range-toggle {" in ui.STYLE)
    for gone in (".log-tabs {", "Also in Tools:"):
        check(f"retired idiom is gone: {gone.strip()}",
              gone not in ui.STYLE and gone not in app_source)
    for hand_rolled in ('<nav aria-label="Ledger sections">',
                        '<nav aria-label="Tax sections">',
                        '<nav aria-label="Insights sections">'):
        check(f"hand-rolled nav replaced: {hand_rolled[:34]}",
              hand_rolled not in app_source)

    # Zero inline style= attributes in app.py — the rule that spacing
    # decisions live in the stylesheet, enforced the way colour is.
    inline = re.findall(r'style="', app_source)
    check("no inline style= attributes in app.py", not inline,
          f"found {len(inline)}")

    # The shared interaction layer is wired into every page.
    check("table.js loaded by the shell", "/static/app/table.js" in ui.STYLE
          or "/static/app/table.js" in Path(ui.__file__).read_text(encoding="utf-8"))
    check("sortable header styling present", "th.sortable > button {" in ui.STYLE)
    check("bulk bar styling present", ".bulk-bar {" in ui.STYLE)
    check("dialog styling present", "dialog.kl-dialog {" in ui.STYLE)

    # Helpers produce escaped output — these strings reach HTML attributes.
    attrs = ui.confirm_attrs('a "quoted" & <tagged> message')
    check("confirm_attrs escapes quotes", '"' not in attrs.split('data-confirm="')[1].split('"')[0]
          or "&quot;" in attrs)
    check("confirm_attrs escapes markup", "<tagged>" not in attrs)
    nav = ui.sub_nav([("k", "Label", "/x?a=1&b=2")], "k", "Aria & label")
    check("sub_nav escapes hrefs", "&amp;b=2" in nav)
    check("sub_nav marks the active item", 'aria-current="page"' in nav)


def check_redesign_navigation(app_source):
    """Phase 4/5 of the 2026-07-26 redesign."""
    import app as app_module

    # Grouped, and short. The count is a creep guard, not a magic number:
    # the redesign cut fifteen flat items to twelve grouped ones, and every
    # later increment should be a deliberate new top-level feature rather
    # than something that could have lived in the palette.
    # 15 = 12 + the Invoices/AR workspace + Inventory + Contacts. Inventory
    # earns a top-level slot because cost of goods cannot post without it,
    # so it is a place users must find rather than an occasional report.
    # Contacts earns one because it is the only screen that answers "who is
    # this customer and what are they worth", and a person is something you
    # go looking for by name rather than remember the location of.
    check("sidebar is grouped", len(ui.NAV_GROUPS) == 5,
          f"{len(ui.NAV_GROUPS)} groups")
    check("sidebar has 15 items", len(ui.NAV_TABS) == 15,
          f"{len(ui.NAV_TABS)} items")
    labels = {label for label, _, _ in ui.NAV_TABS}
    # "Invoices" is deliberately absent from this list. It named the Wave
    # reminder queue when the redesign merged that into Review, and it now
    # names Klearing's own invoicing workspace -- a different feature that
    # has earned a top-level slot. The guard that still matters is that the
    # Wave queue is a Review tab, asserted just below.
    for merged in ("Pending", "Follow-ups", "Connections",
                   "Automation", "Logs"):
        check(f"{merged} merged out of the sidebar", merged not in labels)
    check("Review is in the sidebar", "Review" in labels)
    check("the reminder queue is still a Review tab, not a sidebar item",
          "invoices" in dict(app_module.REVIEW_TABS))
    # The label used to say "Wave" to separate reminders chased in Wave from
    # Klearing's own invoices. They are the same invoices now, so what needs
    # distinguishing is the queue of reminder DRAFTS from the Invoices screen
    # itself -- a tab labelled "Invoices" in Review would read as the latter.
    check("the reminder tab is labelled as reminders, not as invoices",
          dict(app_module.REVIEW_TABS)["invoices"] == "Invoice reminders",
          dict(app_module.REVIEW_TABS)["invoices"])
    check("Invoices is a sidebar item (the new workspace)",
          "Invoices" in labels)

    # Every old path still resolves. This is the promise that a redesign
    # does not turn a URL somebody has used for months into a 404.
    routes = {getattr(r, "path", None) for r in app_module.app.routes}
    # /invoices is no longer in this list: it is a real page now rather than
    # a redirect. It still resolves (checked below), which is what the
    # no-404 promise actually requires.
    for old in ("/pending", "/followups", "/connections",
                "/automation", "/logs", "/tools"):
        check(f"{old} still has a route (redirect)", old in routes)
    check("/invoices resolves as its own page, not a redirect",
          "/invoices" in routes
          and sum(1 for r in app_module.app.routes
                  if getattr(r, "path", None) == "/invoices") == 1,
          "duplicate /invoices routes would make matching order-dependent")
    for new in ("/review", "/settings", "/review/approve", "/review/deny"):
        check(f"{new} exists", new in routes)

    # The palette is the other half of a short sidebar: if a view is not
    # in the rail it must be findable here.
    hrefs = {href for _, href, _, _, _ in ui.PALETTE_ENTRIES}
    for must in ("/review?tab=followups", "/settings?tab=logs",
                 "/tools/audit", "/ledger?tab=pnl", "/uncategorized"):
        check(f"palette reaches {must}", must in hrefs)
    check("palette payload is valid JSON",
          isinstance(json.loads(ui.palette_payload()), list))
    check("palette entries carry search aliases",
          all(kw for *_, kw in ui.PALETTE_ENTRIES))

    # Bulk review: the reason Phase 5 exists.
    check("review table is selectable", 'data-select="#review-bulk"' in app_source)
    check("rows carry per-row bulk field names", "data-bulk-name=" in app_source)
    # Approval writes the Ledger entry and the local bookkeeping. Both sit
    # behind one entry point so single and bulk can never record differently.
    check("one shared approval posting path",
          app_source.count("def _post_one_approved") == 1)
    check("one shared local-bookkeeping path",
          app_source.count("def _record_approval") == 1)
    check("bulk approve reuses it",
          "_post_one_approved(\n                item, chosen," in app_source)
    check("nothing in the approval path calls Wave",
          "wave_client.create_transaction" not in app_source)

    # _make_redirect must cope with bases that already have a query.
    r = app_module._make_redirect("/review?tab=followups")
    check("redirect helper uses & on a base with a query",
          "tab=followups&msg=" in r(msg="x").headers["location"])


def check_redesign_finish(app_source):
    """Phase 6/7/8 of the 2026-07-26 redesign."""
    # The briefing, and the serif finally doing a job.
    check("briefing component exists", ".briefing-lead {" in ui.STYLE)
    check("briefing uses the document register",
          "font-family: var(--font-doc)" in ui.STYLE)
    check("_briefing is on Home", "{_briefing()}" in app_source)
    for gone in ("{render_daily_insight_banner()}", "{render_uncat_nudge()}"):
        check(f"banner folded into the briefing: {gone}",
              gone not in app_source)

    # Progressive disclosure instead of always-on explanation.
    check("disclosure component exists", "details.disclosure {" in ui.STYLE)
    check("ui.disclosure is used", "ui.disclosure(" in app_source)
    check("reconcile no longer renders every section unconditionally",
          "{_reconcile_history_section(signed)}" not in app_source)

    # No raw JSON shown to a user.
    check("shopify order page does not dump line_items_json",
          "<pre>{items}</pre>" not in app_source)

    # Chart/table duplication resolved.
    check("shopify product bar chart removed", "product_chart" not in app_source)

    # Two breakpoints, fluid containers.
    check("second breakpoint exists", "@media (max-width: 1100px)" in ui.STYLE)
    check("containers are fluid", "max-width: min(900px, 100%)" in ui.STYLE)
    check("wide containers are fluid", "min(1160px, 100%)" in ui.STYLE)
    # The grid overflow this exposed.
    check("grid children can shrink below content width",
          ".dash-grid > * { min-width: 0; }" in ui.STYLE)

    # Docs regenerated rather than left stale.
    inventory = (Path(ui.__file__).parent / "skills" / "klearing-section"
                 / "references" / "section-inventory.md").read_text(encoding="utf-8")
    check("section inventory mentions Review", "/review?tab=transactions" in inventory)
    check("section inventory mentions Ledger", "/ledger?tab=" in inventory)
    check("section inventory documents the moved routes", "/pending      ->" in inventory)
    check("section inventory is not the stale version",
          "render_pending_page" not in inventory)


def check_shape_brief(app_source):
    """The Home + Review shape brief (2026-07-26 design audit)."""
    import app as app_module

    # Home: one dominant Today region, not nine equal cards.
    check("Today region exists", ".today-grid {" in ui.STYLE)
    check("Today is rendered on Home", "{_today_region()}" in app_source)
    check("Today has a completion state", "today-clear" in ui.STYLE
          and "all caught up" in app_source)
    check("exactly one Today card is emphasised",
          app_source.count('today-card is-primary') == 1)
    check("ambient cards moved into disclosures",
          '{ui.disclosure("This month"' in app_source
          and '{ui.disclosure("Store and traffic"' in app_source)
    check("health score is on demand, not auto-loaded",
          "var healthLoaded = false;" in app_source
          and "fillCash();\n    \"\"\"" in app_source)
    check("async failures offer a retry", "function retryButton(" in app_source)
    check("async fills stamp freshness", "function stamp(" in app_source)

    # Review: one primary row action, decisions in a drawer.
    check("rows carry a single Review action", "data-review=\"#detail-" in app_source)
    check("detail panels are rendered", "_review_detail_panel" in app_source)
    check("drawer markup exists", 'id="review-drawer"' in app_source)
    check("drawer is a labelled dialog", 'role="dialog"' in app_source
          and 'aria-modal="true"' in app_source)
    check("drawer styling exists", ".drawer {" in ui.STYLE)
    check("drawer shows/hides via the hidden attribute, not transitioned "
          "visibility", ".drawer[hidden] { display: none; }" in ui.STYLE
          and "visibility var(--t-overlay)" not in ui.STYLE)
    check("Deny is spatially separated from Approve",
          ".drawer-foot .deny-slot" in ui.STYLE)
    check("rows no longer carry per-row selects",
          "_review_transactions_content" in app_source
          and 'class="review-cat"' in app_source)

    # Bulk must survive the drawer: the field lookup follows a pointer,
    # not closest('tr'), or a row whose drawer is open contributes
    # nothing to a bulk submit.
    table_js = (Path(ui.__file__).parent / "frontend" / "app"
                / "table.js").read_text(encoding="utf-8")
    check("bulk field lookup follows data-detail",
          'b.getAttribute("data-detail")' in table_js)
    check("checkboxes point at their detail panel",
          'data-detail="#detail-' in app_source)
    check("detail panels stay in the document while parked",
          ".review-panels { display: none; }" in ui.STYLE)

    # Approve stays irreversible-by-design: no single-key approve.
    check("no single-key approve shortcut",
          'key === "a"' not in table_js and "key === 'a'" not in table_js)
    check("keyboard queue traversal exists",
          'event.key === "j"' in table_js)

    # A failed post returns to the item, not to a flat table.
    check("failed post reopens the drawer",
          "def _redirect_review_item" in app_source)
    check("review route accepts the reopen id", "review: int = None" in app_source)
    r = app_module._redirect_review_item(7, "boom")
    check("reopen redirect carries item and error",
          "review=7" in r.headers["location"]
          and "err=" in r.headers["location"])

    # Terminology, Home and Review only.
    check("Reconcile relabelled in nav",
          any(label == "Bank vs books" for label, _, _ in ui.NAV_TABS))
    check("reconcile still findable in the palette",
          any("reconcile" in (kw or "") for *_, kw in ui.PALETTE_ENTRIES))
    check("approval copy avoids bare Wave",
          "recorded in your native books" in app_source)


def main():
    ui_source = Path(ui.__file__).read_text(encoding="utf-8")
    app_source = (Path(ui.__file__).parent / "app.py").read_text(encoding="utf-8")

    print("Pass 1 — panel selector consolidation")
    check_panel_selectors_share_base_rule()
    print("\nShared classes are not parent-scoped")
    check_hint_is_not_parent_scoped()
    print("\nNav groups are announced, not just drawn")
    check_nav_groups_are_announced()
    print("\nCard reflow is opt-in and breakpoint-scoped")
    check_card_reflow_css()
    print("\nPass 1 — accent color single source")
    check_accent_hex_single_source(ui_source)
    print("\nPass 2 — type scale tokens")
    check_fs_tokens_defined_and_referenced()
    print("\nPass 3 — setup wizard ARIA")
    check_wizard_aria()
    print("\nPolish pass — remaining known-gap colors tokenized")
    check_polish_pass_tokens()
    print("\nRedesign Phase 2/3 — component consolidation")
    check_redesign_consolidation(app_source)
    print("\nRedesign Phase 4/5 — navigation, palette, bulk review")
    check_redesign_navigation(app_source)
    print("\nRedesign Phase 6/7/8 — briefing, disclosure, responsive, docs")
    check_redesign_finish(app_source)
    print("\nShape brief — Home Today region and Review drawer")
    check_shape_brief(app_source)

    total = len(_failures)
    print()
    if total:
        print(f"FAIL: {total} check(s) failed:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("PASS: all UI consolidation regression checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
