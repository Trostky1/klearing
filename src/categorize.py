"""Two-tier transaction categorization.

Rules are deterministic and instant. Unknown vendors fall through to the
existing local Ollama client, but a slow or unavailable model never blocks
the pending queue.
"""

import db
import ledger
import ollama_client


VENDOR_PATTERNS = {
    # Software and cloud
    "amazon web services": "Software & Subscriptions",
    "aws": "Software & Subscriptions",
    "google cloud": "Software & Subscriptions",
    "digitalocean": "Software & Subscriptions",
    "vercel": "Software & Subscriptions",
    "github": "Software & Subscriptions",
    "adobe": "Software & Subscriptions",
    "figma": "Software & Subscriptions",
    "notion": "Software & Subscriptions",
    "slack": "Software & Subscriptions",
    "zoom": "Software & Subscriptions",
    "dropbox": "Software & Subscriptions",
    "1password": "Software & Subscriptions",
    # Payment processing
    "stripe": "Bank & Payment Fees",
    "paypal": "Bank & Payment Fees",
    "square": "Bank & Payment Fees",
    # Payroll and contractors
    "gusto": "Payroll & Contractors",
    "quickbooks": "Software & Subscriptions",
    "wave": "Software & Subscriptions",
    # Office and supplies
    "staples": "Office Expense",
    "office depot": "Office Expense",
    "amazon": "Office Expense",
    # Travel
    "uber": "Uncategorized Expense",
    "lyft": "Uncategorized Expense",
    "delta": "Uncategorized Expense",
    "united": "Uncategorized Expense",
    "american airlines": "Uncategorized Expense",
    "southwest": "Uncategorized Expense",
    "marriott": "Uncategorized Expense",
    "hilton": "Uncategorized Expense",
    "airbnb": "Uncategorized Expense",
    # Meals
    "doordash": "Uncategorized Expense",
    "grubhub": "Uncategorized Expense",
    "uber eats": "Uncategorized Expense",
    "wholefds": "Uncategorized Expense",
    "whole foods": "Uncategorized Expense",
    "trader joe": "Uncategorized Expense",
    # Utilities and internet
    "comcast": "Rent & Utilities",
    "at&t": "Rent & Utilities",
    "verizon": "Rent & Utilities",
    "t-mobile": "Rent & Utilities",
    # Marketing
    "google ads": "Advertising & Marketing",
    "facebook ads": "Advertising & Marketing",
    "meta ads": "Advertising & Marketing",
    "mailchimp": "Advertising & Marketing",
    "klaviyo": "Advertising & Marketing",
    "hubspot": "Advertising & Marketing",
    # Professional services
    "fedex": "Uncategorized Expense",
    "ups": "Uncategorized Expense",
    "usps": "Uncategorized Expense",
}

def _normalize(value):
    return " ".join(str(value or "").lower().split())


def _rule_match(vendor_name):
    vendor = _normalize(vendor_name)
    for pattern in sorted(VENDOR_PATTERNS, key=len, reverse=True):
        if pattern in vendor:
            return pattern, VENDOR_PATTERNS[pattern]
    return None, None


def correction_pattern(vendor_name):
    """Return the stable pattern key used when learning a correction."""
    pattern, _category = _rule_match(vendor_name)
    return pattern or _normalize(vendor_name)


def _correction_match(vendor_name, db_module):
    vendor = _normalize(vendor_name)
    if not vendor:
        return None
    matches = []
    for row in db_module.list_category_corrections():
        pattern = _normalize(row.get("vendor_pattern"))
        if pattern and pattern in vendor:
            matches.append((len(pattern), row.get("count", 0),
                            row.get("last_seen", ""),
                            row.get("corrected_category")))
    if not matches:
        return None
    return max(matches)[-1]


def get_native_categories():
    """Return revenue and expense names from the native Chart of Accounts.

    The chart is local and authoritative, so there is no cache, TTL, network
    fallback, or generic category list that can drift away from the accounts
    an approval can actually post to.
    """
    return sorted({account["name"] for account in ledger.list_accounts()
                   if account["type"] in ("expense", "revenue")})


def categorize(vendor_name, amount, direction, db):
    """Return ``(category, confidence, source)`` for one transaction."""
    corrected = _correction_match(vendor_name, db)
    if corrected:
        return corrected, "high", "rule"

    _pattern, category = _rule_match(vendor_name)
    if category:
        return category, "high", "rule"

    if not ollama_client.is_available():
        return None, None, "none"

    categories = get_native_categories()
    if not categories:
        return None, None, "none"
    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Amount: {amount}\n"
        f"Direction: {direction}\n"
        f"Available categories: {', '.join(categories)}\n\n"
        "Category:"
    )
    response, status = ollama_client.generate_category(prompt, categories)
    if status == "timeout":
        return None, None, "timeout"
    if status != "ok":
        return None, None, "none"

    answer = _normalize(response)
    for option in categories:
        if _normalize(option) == answer:
            return option, "medium", "ollama"
    return None, None, "none"
