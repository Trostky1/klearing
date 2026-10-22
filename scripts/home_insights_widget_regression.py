"""Offline render checks for Feature B's configurable Home widget."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-home-insights-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import insight_metrics  # noqa: E402
import insight_widget  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return 0 if condition else 1


def metric(metric_id, label, value, delta, source, unit):
    return insight_metrics.make_insight_metric(
        metric_id, label, value, delta, source,
        f"https://example.com/{metric_id}", unit,
        ([{"period_start": "2026-06-01", "period_end": "2026-06-30",
           "value": value or 0}] if value is not None else []),
    )


def render(pinned_ids, metrics=None, error=None):
    original_pins = insight_widget.get_pinned_metric_ids
    original_metrics = insight_metrics.get_insight_metrics
    insight_widget.get_pinned_metric_ids = lambda: list(pinned_ids)
    if error is None:
        insight_metrics.get_insight_metrics = lambda: list(metrics or [])
    else:
        def fail():
            raise error
        insight_metrics.get_insight_metrics = fail
    try:
        return app._home_insights_widget()
    finally:
        insight_widget.get_pinned_metric_ids = original_pins
        insight_metrics.get_insight_metrics = original_metrics


def run():
    failures = 0
    metrics = [
        metric("shopify_revenue", "Shopify revenue", 125034, -2550,
               "shopify", "currency_cents"),
        metric("inventory_gross_margin", "Inventory-backed gross margin",
               32.5, 1.25, "inventory", "percent"),
        metric("inventory_value", "Inventory at cost", None, None,
               "inventory", "currency_cents"),
    ]

    subset = render(
        ["inventory_gross_margin", "shopify_revenue"], metrics)
    failures += check(
        "only the pinned available subset renders in persisted order",
        subset.index("Inventory-backed gross margin")
        < subset.index("Shopify revenue")
        and "Inventory at cost" not in subset)
    failures += check(
        "money, percentages, and deltas keep numeric typography",
        '<span class="insight-metric-value mono">32.5%</span>' in subset
        and '<span class="mono">1.25 pts</span>' in subset
        and '<span class="insight-metric-value mono">$1,250.34</span>' in subset
        and '<span class="mono">$25.50</span>' in subset)
    failures += check(
        "each metric is one isolated external source link",
        subset.count('target="_blank" rel="noopener noreferrer"') == 2
        and 'href="https://example.com/inventory_gross_margin"' in subset
        and 'href="https://example.com/shopify_revenue"' in subset)
    failures += check(
        "Phase 4 renders no chart markup or inline styles",
        all(token not in subset.lower()
            for token in ("<svg", "<canvas", "style=")))

    no_value = render(["inventory_value"], metrics)
    failures += check(
        "a metric without enough data remains honest and actionable",
        '<span class="insight-metric-value mono">\u2014</span>' in no_value
        and "Not enough stored data yet" in no_value
        and 'href="https://example.com/inventory_value"' in no_value)

    failures += check(
        "an intentional empty selection hides the Home widget",
        render([], metrics) == "")
    unavailable = render(["shopify_revenue"], [])
    failures += check(
        "pinned sources without data get a recovery-oriented empty state",
        "Your pinned metrics are not ready yet." in unavailable
        and 'href="/settings?tab=connections"' in unavailable)
    failed = render(["shopify_revenue"], error=RuntimeError("offline"))
    failures += check(
        "a registry failure cannot take down Home",
        "Insights could not load from stored data." in failed
        and 'href="/"' in failed)

    home_source = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    failures += check(
        "the widget is a sibling after Feature A and before disclosures",
        "{_connection_briefing()}\n    {_home_insights_widget()}\n    {ui.disclosure"
        in home_source)

    if failures:
        print(f"FAIL: {failures} Home Insights widget checks failed.")
        return 1
    print("PASS: Feature B Home Insights widget checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
