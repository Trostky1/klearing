"""
Weekly financial digest for Klearing.

Runs weekly (scheduled Monday 8:00 AM) and emails a summary of the
just-completed week (Monday through Sunday) compared with the week
before it. Uses approved transactions only — the same data the
Summary tab shows.

Transfers (owner contributions) are excluded from income and expense
totals, exactly as on the Summary tab, so they never inflate revenue
or spend.
"""

import sys
from datetime import date, timedelta
from decimal import Decimal

import db
import business_registry
import email_utils
from config import APP_NAME, APP_URL

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TOP_CATEGORIES = 3


def week_bounds(today=None):
    """
    Return (this_start, this_end, prior_start, prior_end) for the most
    recently completed Mon-Sun week and the one before it.

    On the Monday this is scheduled for, "this week" is the week that
    just ended yesterday (Sunday).
    """
    today = today or date.today()
    # Most recent Sunday on or before yesterday. isoweekday(): Mon=1..Sun=7,
    # so Monday -> back 1 day to Sunday; other days -> back to prior Sunday.
    this_end = today - timedelta(days=today.isoweekday() % 7 or 7)
    this_start = this_end - timedelta(days=6)
    prior_end = this_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=6)
    return this_start, this_end, prior_start, prior_end


def summarize_week(start, end):
    """Income, expenses, net, and expense-by-category for approved
    transactions dated within [start, end] (inclusive)."""
    lo, hi = start.isoformat(), end.isoformat()
    items = [
        i for i in db.list_items(status="approved")
        if lo <= i["date"] <= hi
    ]
    income = sum(i["amount"] for i in items if i["direction"] == "income")
    expenses = sum(i["amount"] for i in items if i["direction"] == "expense")

    by_category = {}
    for i in items:
        if i["direction"] == "expense":
            by_category[i["suggested_category"]] = (
                by_category.get(i["suggested_category"], Decimal(0))
                + i["amount"]
            )
    top = sorted(by_category.items(), key=lambda kv: -kv[1])[:TOP_CATEGORIES]

    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "top_expenses": top,
        "expenses_by_category": by_category,
    }


BASELINE_WEEKS = 8


def rolling_expense_baseline(this_start):
    """Average weekly approved expenses over the up-to-8 completed weeks
    before `this_start`.

    Returns (average, weeks_used). weeks_used counts only weeks at or
    after the first week with any approved expense history, so a new
    user is never handed a false 8-week baseline padded with zero-spend
    weeks. weeks_used is 0 when there's no prior history, which the
    caller reads as "no baseline to show yet".
    """
    approved = db.list_items(status="approved")
    expense_dates = [i["date"] for i in approved if i["direction"] == "expense"]
    if not expense_dates:
        return Decimal(0), 0
    earliest = min(expense_dates)

    weekly_totals = []
    for k in range(1, BASELINE_WEEKS + 1):
        wk_end = this_start - timedelta(days=1 + 7 * (k - 1))
        wk_start = wk_end - timedelta(days=6)
        # Skip weeks entirely before any history began — they'd only
        # dilute the average toward zero.
        if wk_end.isoformat() < earliest:
            continue
        lo, hi = wk_start.isoformat(), wk_end.isoformat()
        total = sum(
            i["amount"] for i in approved
            if i["direction"] == "expense" and lo <= i["date"] <= hi
        )
        weekly_totals.append(total)

    if not weekly_totals:
        return Decimal(0), 0
    return sum(weekly_totals) / len(weekly_totals), len(weekly_totals)


def baseline_line(this_expenses, average, weeks_used):
    """One-line comparison of this week's spend to the rolling average,
    or None when there's no prior history to compare against."""
    if weeks_used <= 0:
        return None
    if weeks_used >= BASELINE_WEEKS:
        label, note = f"your {BASELINE_WEEKS}-week average", ""
    else:
        plural = "s" if weeks_used != 1 else ""
        label = "your average"
        note = f" (based on {weeks_used} week{plural} of data)"
    if average <= 0:
        return (f"This week's spend is {money(this_expenses)}; {label} is "
                f"{money(average)}.{note}")
    pct = (this_expenses - average) / average * 100
    word = "above" if pct >= 0 else "below"
    return (f"This week's spend is {abs(pct):.0f}% {word} {label} "
            f"({money(average)}).{note}")


def change_indicator(current, previous):
    """A small up/down string comparing current to previous."""
    if previous == 0:
        if current == 0:
            return "no change"
        return "New"
    pct = (current - previous) / previous * 100
    return f"{pct:+.0f}%"


def money(amount):
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def format_date_range(start, end):
    return f"{start.strftime('%b %d')} to {end.strftime('%b %d, %Y')}"


def build_digest(today=None):
    """Return (subject, body) for the weekly digest email."""
    this_start, this_end, prior_start, prior_end = week_bounds(today)
    this_week = summarize_week(this_start, this_end)
    prior_week = summarize_week(prior_start, prior_end)

    subject = f"{APP_NAME} Weekly Digest (Week of {this_start.isoformat()})"

    lines = [
        f"Week of {format_date_range(this_start, this_end)}",
        "",
        "THIS WEEK'S NUMBERS",
        "-------------------",
        f"  Income:   {money(this_week['income'])}",
        f"  Expenses: {money(this_week['expenses'])}",
        f"  Net:      {money(this_week['net'])}",
        "",
        f"VS. LAST WEEK ({format_date_range(prior_start, prior_end)})",
        "-------------------------------------",
        f"  Income:   {money(this_week['income'])}  "
        f"({change_indicator(this_week['income'], prior_week['income'])})"
        f"   [last week: {money(prior_week['income'])}]",
        f"  Expenses: {money(this_week['expenses'])}  "
        f"({change_indicator(this_week['expenses'], prior_week['expenses'])})"
        f"   [last week: {money(prior_week['expenses'])}]",
        f"  Net:      {money(this_week['net'])}  "
        f"   [last week: {money(prior_week['net'])}]",
    ]

    average, weeks_used = rolling_expense_baseline(this_start)
    line = baseline_line(this_week["expenses"], average, weeks_used)
    if line:
        lines += ["", "VS. YOUR AVERAGE", "----------------", f"  {line}"]

    lines += [
        "",
        "TOP EXPENSE CATEGORIES (this week)",
        "---------------------------------",
    ]

    if this_week["top_expenses"]:
        for rank, (category, amount) in enumerate(this_week["top_expenses"], 1):
            prior_amount = prior_week["expenses_by_category"].get(
                category, Decimal(0))
            lines.append(
                f"  {rank}. {category}: {money(amount)}  "
                f"({change_indicator(amount, prior_amount)})"
            )
    else:
        lines.append("  No expenses this week.")

    # With several local businesses configured, break the week down per
    # business so the combined totals above stay interpretable.
    businesses = business_registry.list_businesses()
    if len(businesses) > 1:
        lines += ["", "BY BUSINESS (this week)", "-----------------------"]
        lo, hi = this_start.isoformat(), this_end.isoformat()
        items = [i for i in db.list_items(status="approved")
                 if lo <= i["date"] <= hi]
        for business in businesses:
            mine = [i for i in items if i.get("business_id") == business["id"]]
            income = sum(i["amount"] for i in mine if i["direction"] == "income")
            expenses = sum(i["amount"] for i in mine if i["direction"] == "expense")
            lines.append(
                f"  {business['name']}: income {money(income)}, "
                f"expenses {money(expenses)}, net {money(income - expenses)}"
            )
        unattributed = [i for i in items if not i.get("business_id")]
        if unattributed:
            lines.append(f"  (plus {len(unattributed)} item(s) with no "
                         f"business stamp, counted in the totals above)")

    lines += [
        "",
        "(Transfers are excluded from income and expense totals.)",
        "",
        f"Review at {APP_URL}",
    ]

    return subject, "\n".join(lines)


def run_weekly() -> int:
    import error_alerts

    def run():
        db.init_db()
        subject, body = build_digest()
        print(subject)
        print()
        print(body)
        print()
        email_utils.send_email(subject, body)

    failures = []
    error_alerts.run_step("Weekly digest", run, failures)
    if failures:
        error_alerts.notify_failures(failures, context="Weekly digest")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_weekly())
