"""Offline regression coverage for Insights widget configuration."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path


_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-insight-widget-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import insight_widget  # noqa: E402
from insight_metrics import DEFAULT_PINNED_METRIC_IDS, METRIC_CATALOG  # noqa: E402
from migrations.run_migrations import run_migrations  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return not condition


def main():
    failures = 0
    db.init_db()
    first = run_migrations(report=False)
    second = run_migrations(report=False)
    failures += check(
        "widget migration is additive and idempotent",
        first["insight_widget_config"] in {"created", "skipped"}
        and second["insight_widget_config"] == "skipped")

    failures += check(
        "a new profile receives the three locked defaults",
        insight_widget.get_pinned_metric_ids()
        == list(DEFAULT_PINNED_METRIC_IDS))

    saved = insight_widget.save_pinned_metric_ids([
        "inventory_value", "shopify_revenue", "inventory_value", "unknown",
    ])
    failures += check(
        "writes reject unknown IDs, deduplicate, and use catalog order",
        saved == ["shopify_revenue", "inventory_value"]
        and insight_widget.get_pinned_metric_ids() == saved)

    insight_widget.save_pinned_metric_ids([])
    row = db.get_insight_widget_config(insight_widget.LOCAL_PROFILE_ID)
    failures += check(
        "an explicit empty selection stays distinct from first-run defaults",
        insight_widget.get_pinned_metric_ids() == []
        and json.loads(row["metric_ids_json"]) == [])

    with db.get_connection() as conn:
        conn.execute(
            "UPDATE insight_widget_config SET metric_ids_json = ? "
            "WHERE profile_id = ?",
            ("not-json", insight_widget.LOCAL_PROFILE_ID),
        )
    failures += check(
        "a corrupt row falls back safely to defaults",
        insight_widget.get_pinned_metric_ids()
        == list(DEFAULT_PINNED_METRIC_IDS))

    insight_widget.save_pinned_metric_ids(
        ["inventory_value"], profile_id="profile-b")
    failures += check(
        "profile scope keeps selections independent",
        insight_widget.get_pinned_metric_ids("profile-b") == ["inventory_value"]
        and insight_widget.get_pinned_metric_ids("profile-c")
        == list(DEFAULT_PINNED_METRIC_IDS))

    import app  # noqa: E402

    settings_html = app.render_settings_page([])
    widget_html = settings_html.split('id="home-insights-widget"', 1)[1]
    widget_html = widget_html.split('action="/settings/ai-provider"', 1)[0]
    failures += check(
        "General Settings lists the complete catalog in its own form",
        'action="/settings/insights-widget"' in widget_html
        and all(f'value="{definition["id"]}"' in widget_html
                for definition in METRIC_CATALOG)
        and widget_html.count('name="metric_ids"') == len(METRIC_CATALOG))
    failures += check(
        "corrupt or first-run state renders exactly the three defaults",
        widget_html.count('name="metric_ids"') == len(METRIC_CATALOG)
        and widget_html.count(' checked') == len(DEFAULT_PINNED_METRIC_IDS)
        and set(re.findall(
            r'name="metric_ids"\s+value="([^"]+)"[^>]* checked', widget_html))
        == set(DEFAULT_PINNED_METRIC_IDS))

    response = app.save_insights_widget(metric_ids=[])
    failures += check(
        "saving an empty selection uses POST to 303 with a banner",
        response.status_code == 303
        and response.headers.get("location")
        == "/settings?msg=Home%20insights%20widget%20updated."
        and insight_widget.get_pinned_metric_ids() == [])

    empty_html = app.render_settings_page([])
    empty_widget_html = empty_html.split('id="home-insights-widget"', 1)[1]
    empty_widget_html = empty_widget_html.split(
        'action="/settings/ai-provider"', 1)[0]
    failures += check(
        "an intentionally empty Home widget renders with no boxes checked",
        'Leave all unchecked to hide the Home widget.' in empty_widget_html
        and ' checked' not in empty_widget_html)

    if failures:
        print(f"FAIL: {failures} Insights widget checks failed.")
        return 1
    print("PASS: Insights widget checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
