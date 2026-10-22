"""
Regression: every non-POST entry in ui.PALETTE_ENTRIES must be reachable
from real navigation — either a direct sidebar destination (ui.NAV_TABS)
or a sub-nav link built by one of app.py's own tab-builder functions,
whose parent route is itself a sidebar destination. PALETTE_ENTRIES is a
hand-maintained literal (ui.py says so at its definition), so nothing
previously caught a palette-only destination nothing else points to —
this is that check.

Calls the real builder functions (_insights_nav, _ledger_nav, _tax_nav,
_tools_sibling_nav, _settings_nav, _invoice_nav, _inventory_nav,
_contacts_nav) rather
than re-typing their source literals, so it tracks what those functions
actually render, not a second copy of
LEDGER_TABS/TAX_TABS/TOOLS_SUBPAGES that could drift from them.

Static/import-level check, no server, no DB, no network — same spirit as
scripts/ui_consolidation_regression.py. Importing app.py touches db.py at
module scope, so this points KLEARING_DATA_DIR at a throwaway directory
first, same isolation scripts/app_render_regression.py uses.

Run it:

    venv/Scripts/python scripts/nav_palette_regression.py   # Windows
    venv/bin/python scripts/nav_palette_regression.py       # macOS/Linux

Exits 0 if every check passes, 1 if any fails (prints which).
"""
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DATA_DIR = tempfile.TemporaryDirectory(prefix="klearing-nav-")
os.environ["KLEARING_DATA_DIR"] = _DATA_DIR.name

import app  # noqa: E402
import ui  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


_HREF_RE = re.compile(r'href="([^"]+)"')


def _hrefs_from(html):
    return set(_HREF_RE.findall(html))


# Destinations reachable by a visible, clickable link that is not a nav or
# sub-nav item. These are exempt from the tab-reachability check because
# they genuinely have a click path -- the check just cannot see it.
#
#   /uncategorized  surfaces as a conditional content link (a "needs
#                   review" card/digest line rendered when the count is
#                   nonzero), matching the app's "a zero badge on an empty
#                   queue is not a nav item" philosophy.
#   /invoices/new   is the primary header action on /invoices, i.e. the
#                   most prominent button on the page it belongs to.
#
# Keep this list short. Anything added here is a destination a user can
# only find if they already know it exists, so it needs a real reason.
_CONTENT_LINK_ONLY = {"/uncategorized", "/invoices/new"}


def reachable_hrefs():
    """Every href with a real click path to it: the sidebar's own
    destinations, plus whatever each sub-nav builder actually renders,
    called live rather than re-derived from their source."""
    hrefs = {href for _, href, _ in ui.NAV_TABS}
    # The active value passed to each builder doesn't matter for
    # reachability — it only selects which link gets .active, not which
    # links exist — so any real key/path works to exercise the function.
    hrefs |= _hrefs_from(app._insights_nav("financial"))
    hrefs |= _hrefs_from(app._ledger_nav("sync"))
    hrefs |= _hrefs_from(app._tax_nav("transactions"))
    hrefs |= _hrefs_from(app._tools_sibling_nav("/tools/data-health"))
    hrefs |= _hrefs_from(app._settings_nav("connections"))
    hrefs |= _hrefs_from(app._invoice_nav("all"))
    hrefs |= _hrefs_from(app._inventory_nav("stock"))
    hrefs |= _hrefs_from(app._contacts_nav("all"))
    # Review's tab nav is built inline inside a DB-touching render
    # function rather than a standalone helper like the other five, so
    # this reconstructs it from the same REVIEW_TABS literal that inline
    # code builds from, instead of calling a function that needs live
    # data (items/counts/categories) to run at all.
    hrefs |= {f"/review?tab={key}" for key, _ in app.REVIEW_TABS}
    return hrefs


def main():
    print("Palette click-path regression\n")
    reachable = reachable_hrefs()
    checked = 0
    for label, href, group, method, keywords in ui.PALETTE_ENTRIES:
        if method == "post":
            # A verb, not a place — /check, /ledger/sync, /lock each run
            # from a button already visible wherever they're relevant;
            # there's no "destination" for them to be reachable from.
            continue
        if href in _CONTENT_LINK_ONLY:
            continue
        checked += 1
        check(f'"{label}" ({href}) has a nav path', href in reachable,
              "not in NAV_TABS or any sub-nav builder's output — add it "
              "to the sidebar/sub-nav, or it's reachable only by typing "
              "into the palette")

    print(f"\n{checked} navigational entries checked, {len(_failures)} failed")
    if _failures:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
