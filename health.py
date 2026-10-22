"""
Business health score — deterministic, no AI, no network.

Same discipline as every flag in the app: plain thresholds and
comparisons on real numbers. This module holds only the *scoring* math
as pure functions; the caller (app.py) gathers the raw inputs (some of
which need a live bank call) and passes them in. That keeps the
scoring trivially testable and side-effect free.

Each factor returns one of:
  available=True  -> a 0-100 sub-score, a short status word, and a
                     plain-language "why" line
  available=False -> excluded from the composite entirely, with the
                     reason shown. Missing data is NEVER scored as good
                     or bad — it is simply left out and the remaining
                     weights are renormalized.

The composite is the weighted average of the available factors only.
Weights are configurable in Settings; a factor's weight is ignored when
that factor is unavailable.
"""

from ui import money

# Factor keys, labels, and the settings key that stores each weight.
FACTORS = ["cash", "reconcile", "revenue", "vendors", "traffic"]
FACTOR_LABELS = {
    "cash": "Cash & runway",
    "reconcile": "Reconcile match",
    "revenue": "Revenue trend",
    "vendors": "Vendor stability",
    "traffic": "Website traffic",
}
WEIGHT_KEY = {key: f"health_weight_{key}" for key in FACTORS}
DEFAULT_WEIGHT = 20  # five factors, sums to 100 by default


def _available(key, score, status, detail):
    return {"key": key, "label": FACTOR_LABELS[key], "available": True,
            "score": max(0, min(100, round(score))), "status": status,
            "detail": detail}


def _unavailable(key, reason):
    return {"key": key, "label": FACTOR_LABELS[key], "available": False,
            "score": None, "status": "no data yet", "detail": reason}


def get_weights():
    """Per-factor weights from Settings, defaulting to 20 each. Negative
    or unparseable values fall back to the default so the score can't be
    broken by a bad input."""
    import db
    weights = {}
    for key in FACTORS:
        raw = db.get_setting(WEIGHT_KEY[key])
        if raw is None or str(raw).strip() == "":
            weights[key] = DEFAULT_WEIGHT
            continue
        try:
            value = float(raw)
            weights[key] = value if value >= 0 else DEFAULT_WEIGHT
        except (ValueError, TypeError):
            weights[key] = DEFAULT_WEIGHT
    return weights


# ------------------------------------------------------------ per factor


def score_cash(configured, reachable, runway_days, runway_threshold,
               balance, balance_threshold):
    if not configured:
        return _unavailable("cash", "Pick your business-cash accounts and set "
                                    "a runway or balance alert in Settings.")
    if not reachable:
        return _unavailable("cash", "Couldn't reach the bank to read the "
                                    "balance just now.")
    if runway_threshold:
        if runway_days is None:  # no burn in the window
            return _available("cash", 100, "strong",
                              "No spend in the last 30 days — runway is "
                              "effectively unlimited.")
        ratio = runway_days / runway_threshold
        note = (f"Runway about {runway_days:.0f} days vs your "
                f"{runway_threshold:.0f}-day alert.")
        if ratio >= 1.5:
            return _available("cash", 100, "strong", note)
        if ratio >= 1.0:
            return _available("cash", 80, "healthy", note)
        if ratio >= 0.5:
            return _available("cash", 45, "watch", note)
        return _available("cash", 20, "at risk", note)
    if balance_threshold:
        ratio = (balance / balance_threshold) if balance_threshold else 1.0
        note = (f"Balance {money(balance)} vs your "
                f"{money(balance_threshold)} alert.")
        if ratio >= 1.5:
            return _available("cash", 100, "strong", note)
        if ratio >= 1.0:
            return _available("cash", 80, "healthy", note)
        if ratio >= 0.5:
            return _available("cash", 45, "watch", note)
        return _available("cash", 20, "at risk", note)
    return _unavailable("cash", "Set a runway or balance alert in Settings.")


def score_reconcile(available, flagged, residual):
    if not available:
        return _unavailable("reconcile", "Connect a bank and select business-cash "
                                         "accounts to compare the two balances.")
    if flagged:
        return _available("reconcile", 30, "discrepancy",
                          f"Unexplained difference of {money(abs(residual))} "
                          f"after the pending queue.")
    return _available("reconcile", 100, "in sync",
                      "The native books and bank agree once pending is accounted for.")


def score_revenue(recent_income, prior_income):
    if recent_income <= 0 and prior_income <= 0:
        return _unavailable("revenue", "No income recorded yet to trend.")
    if prior_income <= 0:
        return _available("revenue", 100, "growing",
                          f"Income {money(recent_income)} this period; none in "
                          f"the prior period.")
    change = (recent_income - prior_income) / prior_income * 100
    note = (f"Income {money(recent_income)} vs {money(prior_income)} prior "
            f"({change:+.0f}%).")
    if change >= 10:
        return _available("revenue", 100, "growing", note)
    if change > -10:
        return _available("revenue", 70, "flat", note)
    return _available("revenue", 35, "declining", note)


def score_vendors(has_history, flagged_count):
    if not has_history:
        return _unavailable("vendors", "No vendor spend history yet.")
    if flagged_count == 0:
        return _available("vendors", 100, "stable",
                          "No vendor spend jumps this month.")
    if flagged_count == 1:
        return _available("vendors", 65, "1 spend jump",
                          "One vendor is over your spend-jump threshold "
                          "this month.")
    return _available("vendors", 40, f"{flagged_count} spend jumps",
                      f"{flagged_count} vendors are over your spend-jump "
                      f"threshold this month.")


def score_traffic(configured, recent_sessions, prior_sessions):
    if not configured:
        return _unavailable("traffic", "GA4 isn't connected (optional).")
    if recent_sessions == 0 and prior_sessions == 0:
        return _unavailable("traffic", "No website traffic recorded yet.")
    if prior_sessions == 0:
        return _available("traffic", 100, "growing",
                          f"{recent_sessions:,} sessions this week; none the "
                          f"week before.")
    change = (recent_sessions - prior_sessions) / prior_sessions * 100
    note = (f"{recent_sessions:,} sessions this week vs "
            f"{prior_sessions:,} last ({change:+.0f}%).")
    if change >= 10:
        return _available("traffic", 100, "growing", note)
    if change > -10:
        return _available("traffic", 70, "flat", note)
    return _available("traffic", 40, "declining", note)


# --------------------------------------------------------------- combine


def grade_for(score):
    """A-F from a 0-100 score."""
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def combine(factors, weights):
    """Weighted average over AVAILABLE factors only. Returns the score
    (None when nothing is available or all available weights are 0), the
    grade, the weakest available factor, and the include/exclude split."""
    available = [f for f in factors if f["available"]]
    unavailable = [f for f in factors if not f["available"]]
    total_weight = sum(weights.get(f["key"], 0) for f in available)

    if not available or total_weight <= 0:
        return {"score": None, "grade": None, "factors": factors,
                "available": available, "unavailable": unavailable,
                "weakest": None}

    composite = sum(f["score"] * weights.get(f["key"], 0)
                    for f in available) / total_weight
    # Weakest = lowest sub-score among available; ties break to the
    # higher-weighted factor (it drags the composite more).
    weakest = min(available,
                  key=lambda f: (f["score"], -weights.get(f["key"], 0)))
    return {"score": round(composite), "grade": grade_for(composite),
            "factors": factors, "available": available,
            "unavailable": unavailable, "weakest": weakest}
