"""Offline render checks for Feature B's full Insights Overview."""

import os
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-insights-overview-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import insight_metrics  # noqa: E402
import insight_widget  # noqa: E402
import ui  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return 0 if condition else 1


def metric(metric_id, label, value, delta, source, unit="currency_cents"):
    return insight_metrics.make_insight_metric(
        metric_id, label, value, delta, source,
        f"https://example.com/{metric_id}", unit,
        [{"period_start": "2026-06-01", "period_end": "2026-06-30",
          "value": value}],
    )


def run():
    failures = 0
    metrics = [
        metric("shopify_revenue", "Shopify revenue", 125034, 2550,
               "shopify"),
        metric("plaid_cash_position", "Cash position", 808000, -1200,
               "plaid"),
        metric("plaid_top_expense_category", "Top expense: Shipping", 93000,
               3000, "plaid"),
        metric("inventory_value", "Inventory at cost", 440000, 5000,
               "inventory"),
        metric("shopify_payouts_matched", "Payouts matched", 118000, None,
               "shopify"),
        metric("inventory_gross_margin", "Inventory gross margin", 31.25,
               1.5, "inventory", "percent"),
        metric("expense_growth_vs_revenue", "Shipping is climbing faster",
               4.75, -0.5, "plaid", "percent"),
    ]

    page = app.render_insights_page(None, metrics=metrics)
    main_html = page.split("<main", 1)[1].split("</main>", 1)[0]
    failures += check(
        "/insights defaults to the cross-source Overview tab",
        "Across your business" in page
        and 'href="/insights" class="active"' in page
        and 'href="/insights?tab=financial"' in page
        and 'href="/insights?tab=marketing"' in page)
    failures += check(
        "every registry metric renders once and in registry order",
        page.count('class="insight-metric"') == len(metrics)
        and all(page.count(f'href="https://example.com/{item["id"]}"') == 1
                for item in metrics)
        and all(page.index(metrics[index]["label"])
                < page.index(metrics[index + 1]["label"])
                for index in range(len(metrics) - 1)))
    failures += check(
        "Overview and Home share the exact metric renderer",
        all(app._insight_metric_link(item) in page for item in metrics))

    original_pins = insight_widget.get_pinned_metric_ids
    original_registry = insight_metrics.get_insight_metrics
    insight_widget.get_pinned_metric_ids = lambda: [
        "shopify_revenue", "plaid_cash_position",
        "plaid_top_expense_category",
    ]
    insight_metrics.get_insight_metrics = lambda: list(metrics)
    try:
        home = app._home_insights_widget()
    finally:
        insight_widget.get_pinned_metric_ids = original_pins
        insight_metrics.get_insight_metrics = original_registry
    failures += check(
        "first-run metric numbers and deltas are byte-identical on both surfaces",
        all(app._insight_metric_link(item) in home
            and app._insight_metric_link(item) in page
            for item in metrics[:3])
        and all(app._insight_metric_link(item) not in home
                for item in metrics[3:]))
    failures += check(
        "all metric destinations are isolated source-platform links",
        page.count('target="_blank" rel="noopener noreferrer"')
        == len(metrics))
    failures += check(
        "the Phase 5 Overview adds no chart rendering",
        "chart-svg" not in main_html
        and "<canvas" not in main_html.lower()
        and "/static/insights/financial.js" not in main_html)

    empty = app._render_cross_source_insights_page([])
    failures += check(
        "the no-source state points to Connections",
        "No cross-source metrics are ready yet." in empty
        and 'href="/settings?tab=connections"' in empty)
    failed = app._render_cross_source_insights_page([], metrics_failed=True)
    failures += check(
        "a registry failure leaves the page usable and explains local behavior",
        "Insights could not read the stored source data." in failed
        and "No source system was contacted." in failed
        and 'href="/insights"' in failed)

    hostile = metric("hostile_metric", "<script>alert(1)</script>", 1, 0,
                     "shopify", "count")
    hostile_page = app._render_cross_source_insights_page([hostile])
    failures += check(
        "source-controlled labels are escaped",
        "<script>alert(1)</script>" not in hostile_page
        and "&lt;script&gt;alert(1)&lt;/script&gt;" in hostile_page)

    overview_palette = [entry for entry in ui.PALETTE_ENTRIES
                        if entry[0] == "Insights — Overview"]
    failures += check(
        "Overview remains findable from navigation and the command palette",
        len(overview_palette) == 1 and overview_palette[0][1] == "/insights")

    if failures:
        print(f"FAIL: {failures} Insights Overview checks failed.")
        return 1
    print("PASS: Feature B Insights Overview checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
