"""Persistence rules for the customizable Home Insights widget."""

import json
from datetime import datetime, timezone

import db
from insight_metrics import DEFAULT_PINNED_METRIC_IDS, METRIC_CATALOG


LOCAL_PROFILE_ID = "local"


def _normalize_metric_ids(metric_ids):
    """Known IDs once each, in the catalog's stable display order."""
    selected = {str(metric_id) for metric_id in (metric_ids or [])}
    return [definition["id"] for definition in METRIC_CATALOG
            if definition["id"] in selected]


def get_pinned_metric_ids(profile_id=LOCAL_PROFILE_ID):
    """Saved selection, or product defaults for a new/corrupt profile."""
    db.init_db()
    row = db.get_insight_widget_config(profile_id)
    if row is None:
        return list(DEFAULT_PINNED_METRIC_IDS)
    try:
        stored = json.loads(row["metric_ids_json"])
    except (KeyError, TypeError, ValueError):
        return list(DEFAULT_PINNED_METRIC_IDS)
    if not isinstance(stored, list):
        return list(DEFAULT_PINNED_METRIC_IDS)
    return _normalize_metric_ids(stored)


def save_pinned_metric_ids(metric_ids, profile_id=LOCAL_PROFILE_ID):
    """Validate and atomically save a profile's explicit selection."""
    db.init_db()
    normalized = _normalize_metric_ids(metric_ids)
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_insight_widget_config(
        profile_id,
        json.dumps(normalized, separators=(",", ":")),
        now,
    )
    return normalized
