"""Historical Shopify analytics, imported from Admin CSV exports.

DELIBERATELY NOT PART OF THE LEDGER.

This module reads aggregate reporting figures — total sales by day,
sales by product — that Shopify Admin exports as CSV, and stores them for
trend display only. It imports `db` and nothing else. It does not import
`ledger`, it does not import `ledger_sync`, and it never writes a journal
entry. The ledger starts fresh going forward; this exists so the
dashboard can still show what happened before that line, without
pretending the two are the same kind of number.

Nothing here reconciles against profit_and_loss(). The two describe
different scopes — aggregate/historical versus transactional/ongoing —
and cross-checking them would manufacture a disagreement rather than
find one.

CSV rather than the Bulk Operations API on purpose: this is a one-time
seed, and Bulk Operations is a submit/poll/download-JSONL flow whose
implementation surface is out of proportion to something that runs once.
"""

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import db

_CENT = Decimal("0.01")

# metric -> (is_money, needs_its_own_period_column)
METRICS = {
    "total_sales": (True, True),
    "order_count": (False, True),
    "sales_by_product": (True, False),
}
PERIODS = ("day", "week", "month")

# Shopify's export headers vary by report and by locale-ish naming. Match
# on a normalised header rather than an exact string so a "Net sales"
# export and a "Total sales" export both land.
_DATE_HEADERS = ("day", "date", "week", "month", "period", "week starting",
                 "month starting", "day starting")
_VALUE_HEADERS = ("total sales", "net sales", "gross sales", "sales",
                  "orders", "order count", "total orders", "value")
_PRODUCT_TITLE_HEADERS = ("product title", "product", "product name", "title")
_PRODUCT_ID_HEADERS = ("product id", "product_id", "id")


def _normalise(name):
    return (name or "").strip().lower().lstrip("﻿")


def _pick(headers, candidates):
    lookup = {_normalise(h): h for h in headers}
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return None


def _to_cents(value):
    """Money text to integer cents, or None if it is not money.

    Strips currency symbols and thousands separators, and accepts
    parenthesised negatives the way accounting exports write them.
    """
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    for junk in ("$", ",", " ", " "):
        text = text.replace(junk, "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    cents = int(amount.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)
    return -cents if negative else cents


def _to_count(value):
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _to_date(value):
    """Shopify writes several date shapes depending on the report."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y",
                "%Y-%m", "%B %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def import_csv(text, metric_type, period, period_start=None,
               source="csv_export"):
    """Import one Shopify report.

    Validates per row and keeps going: a malformed row is reported with
    its line number and the rest of the file still lands. Failing an
    entire import because row 47 has a stray character would be worse
    than useless on a hand-exported file.

    Returns {"imported": int, "errors": [str], "skipped": int}.
    """
    if metric_type not in METRICS:
        raise ValueError(f"Unknown metric type: {metric_type}")
    if period not in PERIODS:
        raise ValueError(f"Unknown period: {period}")

    is_money, has_own_period = METRICS[metric_type]
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        return {"imported": 0, "errors": ["The file has no header row."],
                "skipped": 0}

    value_col = _pick(headers, _VALUE_HEADERS)
    date_col = _pick(headers, _DATE_HEADERS)
    title_col = _pick(headers, _PRODUCT_TITLE_HEADERS)
    id_col = _pick(headers, _PRODUCT_ID_HEADERS)

    errors = []
    if value_col is None:
        errors.append("No value column found. Expected one of: "
                      + ", ".join(_VALUE_HEADERS) + ".")
    if has_own_period and date_col is None:
        errors.append("No date column found. Expected one of: "
                      + ", ".join(_DATE_HEADERS) + ".")
    if not has_own_period and period_start is None:
        errors.append("This report has no date column, so a period start "
                      "date is required.")
    if errors:
        return {"imported": 0, "errors": errors, "skipped": 0}

    imported_at = datetime.now(timezone.utc).isoformat()
    imported, skipped = 0, 0

    for line_number, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            skipped += 1
            continue

        if has_own_period:
            period_date = _to_date(row.get(date_col))
            if period_date is None:
                errors.append(f"Row {line_number}: "
                              f"could not read a date from "
                              f'"{row.get(date_col, "")}".')
                continue
        else:
            period_date = _to_date(period_start)
            if period_date is None:
                errors.append(f"Row {line_number}: invalid period start date.")
                continue

        raw = row.get(value_col)
        value = _to_cents(raw) if is_money else _to_count(raw)
        if value is None:
            errors.append(f"Row {line_number}: "
                          f'could not read a number from "{raw or ""}".')
            continue

        product_ref, product_title = None, None
        if metric_type == "sales_by_product":
            product_title = (row.get(title_col) or "").strip() or None
            product_ref = ((row.get(id_col) or "").strip() or None
                           if id_col else None) or product_title
            if not product_ref:
                errors.append(f"Row {line_number}: no product name or id.")
                continue

        db.upsert_shopify_analytics_row({
            "metric_type": metric_type,
            "period": period,
            "period_start_date": period_date,
            "value": value,
            "product_ref": product_ref,
            "product_title": product_title,
            "source": source,
            "imported_at": imported_at,
        })
        imported += 1

    return {"imported": imported, "errors": errors, "skipped": skipped}


def trend(metric_type="total_sales", period="day", limit=None):
    """Stored history for display, oldest first.

    Money metrics come back as Decimal; counts as int. The caller renders
    it — this returns no HTML, same as the ledger reports.
    """
    is_money = METRICS.get(metric_type, (False, True))[0]
    rows = db.list_shopify_analytics(metric_type=metric_type, period=period,
                                     limit=limit)
    return [{
        "period_start_date": row["period_start_date"],
        "value": (Decimal(row["value"]) / 100 if is_money else row["value"]),
        "product_ref": row["product_ref"],
        "product_title": row["product_title"],
    } for row in rows]


def summary():
    """What has been imported so far, for the page header."""
    return db.shopify_analytics_summary()
