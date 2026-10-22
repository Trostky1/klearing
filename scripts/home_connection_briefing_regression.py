"""Offline render checks for Feature A's Home connection briefing."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-home-briefing-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import briefing_signals  # noqa: E402
import db  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return 0 if condition else 1


def signal(severity, sentence, link):
    return briefing_signals.make_briefing_signal(severity, sentence, link)


def render(signals, *, shopify=True, plaid=True):
    original_collect = briefing_signals.get_home_briefing_signals
    original_shopify = app.shopify_client.is_configured
    original_plaid = app.plaid_client.is_configured
    briefing_signals.get_home_briefing_signals = lambda: list(signals)
    app.shopify_client.is_configured = lambda: shopify
    app.plaid_client.is_configured = lambda: plaid
    try:
        return app._connection_briefing()
    finally:
        briefing_signals.get_home_briefing_signals = original_collect
        app.shopify_client.is_configured = original_shopify
        app.plaid_client.is_configured = original_plaid


def run():
    failures = 0
    db.init_db()

    empty = render([])
    failures += check(
        "connected zero state is explicit and calm",
        "Nothing new from your connected sources today." in empty
        and "Shopify + Plaid" in empty
        and "source-signal-context" not in empty)

    disconnected = render([], shopify=False, plaid=False)
    failures += check(
        "disconnected zero state offers a local recovery path",
        "Connect Shopify or Plaid" in disconnected
        and 'href="/settings?tab=connections"' in disconnected)

    informational = [
        signal("informational", "Plaid has 2 pending transactions.",
               "https://my.plaid.com/"),
        signal("informational", "Shopify recorded 3 orders totaling $90.00 today.",
               "https://example.myshopify.com/admin/orders"),
    ]
    info_html = render(informational)
    details_start = info_html.index("<details")
    details_tag = info_html[details_start:info_html.index(">", details_start)]
    failures += check(
        "all-informational state is collapsed by default",
        'class="source-signal-context"' in details_tag
        and " open" not in details_tag
        and info_html.count("source-signal informational") == 2)
    failures += check(
        "signal numbers and money use the numeric type convention",
        '<span class="mono">2</span>' in info_html
        and '<span class="mono">3</span>' in info_html
        and '<span class="mono">$90.00</span>' in info_html)

    mixed = [
        signal("informational", "Shopify recorded 1 order today.",
               "https://example.myshopify.com/admin/orders"),
        signal("notable", "Shopify orders are moving faster than usual.",
               "https://example.myshopify.com/admin/orders"),
        signal("urgent", "2 Shopify orders need fulfillment today.",
               "https://example.myshopify.com/admin/orders"),
    ]
    mixed_html = render(mixed)
    failures += check(
        "mixed signals render in severity order",
        mixed_html.index("need fulfillment")
        < mixed_html.index("moving faster")
        < mixed_html.index("recorded"))
    failures += check(
        "urgent, notable, and informational tiers have distinct classes",
        all(f'source-signal {tier}' in mixed_html
            for tier in ("urgent", "notable", "informational")))
    failures += check(
        "source links are external and isolated",
        mixed_html.count('target="_blank" rel="noopener noreferrer"') == 3)

    home_source = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    failures += check(
        "connection briefing is a sibling immediately after Today",
        "{_today_region()}\n    {_connection_briefing()}" in home_source)
    failures += check(
        "Home signal markup introduces no inline styles",
        "style=" not in mixed_html and "style=" not in empty)

    if failures:
        print(f"FAIL: {failures} Home connection briefing checks failed.")
        return 1
    print("PASS: Feature A Home connection briefing checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
