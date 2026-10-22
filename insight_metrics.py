"""Shared contract and provider registry for cross-source insight metrics.

Every provider returns raw numeric values from data already stored on this
device.  ``value`` and ``delta`` use the same unit, while ``time_series``
keeps the underlying dated values needed by a later charting phase.  This
module renders nothing and performs no network calls.
"""

import math
import re
from datetime import date, timedelta
from typing import Literal, TypedDict, cast
from urllib.parse import urlparse


MetricNumber = int | float
InsightSource = Literal["shopify", "plaid", "inventory"]
InsightUnit = Literal["currency_cents", "count", "percent", "days"]


class InsightMetricPoint(TypedDict):
    period_start: str
    period_end: str
    value: MetricNumber


class InsightMetric(TypedDict):
    id: str
    label: str
    value: MetricNumber | None
    delta: MetricNumber | None
    source: InsightSource
    deep_link: str
    unit: InsightUnit
    time_series: list[InsightMetricPoint]


class InsightMetricDefinition(TypedDict):
    id: str
    label: str
    description: str
    source: InsightSource


# Stable catalog order is also the widget order in this phase. The stored
# configuration keeps an ordered list now so a later reordering UI needs no
# persistence migration.
METRIC_CATALOG: tuple[InsightMetricDefinition, ...] = (
    {"id": "shopify_revenue", "label": "Shopify revenue",
     "description": "Net Shopify revenue over the latest 30-day period.",
     "source": "shopify"},
    {"id": "plaid_cash_position", "label": "Cash position",
     "description": "The latest stored Plaid-backed business cash snapshot.",
     "source": "plaid"},
    {"id": "plaid_top_expense_category", "label": "Top expense category",
     "description": "The largest settled Plaid expense category this period.",
     "source": "plaid"},
    {"id": "inventory_value", "label": "Inventory at cost",
     "description": "Stock on hand valued from exact FIFO acquisition layers.",
     "source": "inventory"},
    {"id": "shopify_payouts_matched", "label": "Payouts matched to bank",
     "description": "Shopify payouts with one exact settled bank match.",
     "source": "shopify"},
    {"id": "inventory_gross_margin", "label": "Inventory-backed gross margin",
     "description": "Margin where order revenue and complete FIFO COGS both posted.",
     "source": "inventory"},
    {"id": "expense_growth_vs_revenue", "label": "Expense growth vs revenue",
     "description": "How the top expense category is moving against Shopify revenue.",
     "source": "plaid"},
)

DEFAULT_PINNED_METRIC_IDS = (
    "shopify_revenue",
    "plaid_cash_position",
    "plaid_top_expense_category",
)


_METRIC_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_SOURCES = {"shopify", "plaid", "inventory"}
_UNITS = {"currency_cents", "count", "percent", "days"}


def _number(value, field_name, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number.")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite.")
    return value


def make_insight_metric(
        metric_id: str,
        label: str,
        value: MetricNumber | None,
        delta: MetricNumber | None,
        source: InsightSource,
        deep_link: str,
        unit: InsightUnit,
        time_series: list[InsightMetricPoint]) -> InsightMetric:
    """Build one metric while enforcing the shared provider contract."""
    metric_id = (metric_id or "").strip()
    if not _METRIC_ID_RE.fullmatch(metric_id):
        raise ValueError("An insight metric id must be a lowercase identifier.")

    label = " ".join((label or "").split())
    if not label:
        raise ValueError("An insight metric needs a label.")
    if source not in _SOURCES:
        raise ValueError(f"Unsupported insight source: {source}")
    if unit not in _UNITS:
        raise ValueError(f"Unsupported insight unit: {unit}")

    parsed = urlparse(deep_link)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("An insight metric deep-link must be an HTTPS URL.")

    clean_points: list[InsightMetricPoint] = []
    previous_start = None
    for point in time_series:
        try:
            start = date.fromisoformat(point["period_start"])
            end = date.fromisoformat(point["period_end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Insight time-series points need ISO date bounds.")
        if end < start:
            raise ValueError("An insight time-series period cannot end before it starts.")
        if previous_start is not None and start < previous_start:
            raise ValueError("Insight time-series points must be oldest first.")
        previous_start = start
        clean_points.append({
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "value": _number(point["value"], "time-series value"),
        })

    return {
        "id": metric_id,
        "label": label,
        "value": _number(value, "metric value", optional=True),
        "delta": _number(delta, "metric delta", optional=True),
        "source": cast(InsightSource, source),
        "deep_link": deep_link,
        "unit": cast(InsightUnit, unit),
        "time_series": clean_points,
    }


def rolling_periods(as_of=None, periods=6, days=30):
    """Equal trailing windows, oldest first, for comparable deltas."""
    as_of = as_of or date.today()
    if periods <= 0 or days <= 0:
        raise ValueError("Insight periods and days must be positive.")
    windows = []
    for offset in reversed(range(periods)):
        end = as_of - timedelta(days=offset * days)
        start = end - timedelta(days=days - 1)
        windows.append((start, end))
    return windows


def value_delta(time_series):
    """Current value and same-unit change from the preceding period."""
    if not time_series:
        return None, None
    value = time_series[-1]["value"]
    delta = (value - time_series[-2]["value"]
             if len(time_series) >= 2 else None)
    return value, delta


def get_insight_metrics() -> list[InsightMetric]:
    """Collect every connected provider's metrics from one registry."""
    # Lazy imports avoid cycles: each provider imports only this contract.
    import inventory
    import plaid_client
    import shopify_client

    base_metrics = [
        *shopify_client.get_insight_metrics(),
        *plaid_client.get_insight_metrics(),
        *inventory.get_insight_metrics(),
    ]
    import insight_pairings
    return [*base_metrics, *insight_pairings.get_insight_metrics(base_metrics)]
