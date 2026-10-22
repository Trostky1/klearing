"""
Recurring-transaction detection for the Vendors tab.

Purely informational: scans approved history for the same vendor
charging a similar amount at a roughly regular cadence and surfaces a
"Detected subscriptions" list. It never acts on what it finds — no
auto-approvals, no alerts — because a human deciding "yes, that's my
hosting bill" is exactly the job this app exists to protect.

Detection rules (deliberately conservative — a false "subscription"
label erodes trust faster than a missed one):
- at least MIN_OCCURRENCES charges from one vendor,
- amounts within AMOUNT_TOLERANCE of their median (or within a
  dollar, for small amounts),
- median gap in the weekly (5-9d), biweekly (12-17d), monthly
  (26-36d), or yearly (350-380d) band, with every gap within 40% of
  the median so one-off repeat purchases don't sneak in.
"""

from datetime import date, timedelta
from statistics import median

import db

MIN_OCCURRENCES = 3
AMOUNT_TOLERANCE = 0.15          # ±15% of the median amount
GAP_CONSISTENCY = 0.40           # every gap within ±40% of the median gap

CADENCE_BANDS = [
    ("weekly", 5, 9),
    ("every 2 weeks", 12, 17),
    ("monthly", 26, 36),
    ("yearly", 350, 380),
]

CADENCE_MONTHLY_FACTORS = {
    "weekly": 52 / 12,
    "every 2 weeks": 26 / 12,
    "monthly": 1.0,
    "yearly": 1 / 12,
}


def _parse_date(value):
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _classify_cadence(gaps):
    """A cadence label when the gaps are regular, else None."""
    if not gaps:
        return None
    mid = median(gaps)
    for label, low, high in CADENCE_BANDS:
        if low <= mid <= high:
            tolerance = max(mid * GAP_CONSISTENCY, 2)
            if all(abs(gap - mid) <= tolerance for gap in gaps):
                return label, mid
    return None


def detect_subscriptions(business_id=None):
    """Recurring expense charges in approved history, most recently
    seen first: [{vendor, cadence, typical_amount, monthly_cost, count,
    first_detected_date, last_date, next_expected, runway_impact_days}]."""
    try:
        import cash_alert
        daily_burn = cash_alert.average_daily_burn()
    except Exception:
        daily_burn = 0.0
    by_vendor = {}
    for item in db.list_items(status="approved"):
        if item["direction"] != "expense":
            continue
        if (business_id is not None
                and item.get("business_id") not in (None, business_id)):
            continue
        when = _parse_date(item["date"])
        if when is None:
            continue
        by_vendor.setdefault(item["vendor"], []).append(
            (when, item["amount"]))

    found = []
    for vendor, charges in by_vendor.items():
        if len(charges) < MIN_OCCURRENCES:
            continue
        charges.sort()

        amounts = [amount for _, amount in charges]
        typical = median(amounts)
        allowed = max(typical * AMOUNT_TOLERANCE, 1.00)
        steady = [(when, amount) for when, amount in charges
                  if abs(amount - typical) <= allowed]
        if len(steady) < MIN_OCCURRENCES:
            continue

        dates = [when for when, _ in steady]
        gaps = [(later - earlier).days
                for earlier, later in zip(dates, dates[1:])]
        cadence = _classify_cadence(gaps)
        if cadence is None:
            continue
        label, typical_gap = cadence

        typical_amount = median(a for _, a in steady)
        monthly_cost = typical_amount * CADENCE_MONTHLY_FACTORS[label]
        last = dates[-1]
        found.append({
            "vendor": vendor,
            "cadence": label,
            "typical_amount": typical_amount,
            "monthly_cost": monthly_cost,
            "count": len(steady),
            "first_detected_date": dates[0].isoformat(),
            "last_date": last.isoformat(),
            "next_expected": (last + timedelta(days=round(typical_gap))).isoformat(),
            "runway_impact_days": (monthly_cost / daily_burn
                                    if daily_burn > 0 else None),
        })

    found.sort(key=lambda row: row["last_date"], reverse=True)
    return found
