"""Validated Schedule C metadata for native Ledger accounts.

The mapping is intentionally account-level: journal activity keeps its
double-entry meaning, while tax reports can group each revenue or expense
account without reclassifying individual entries.  ``None`` always means
unresolved; it never means "safe to omit."
"""


CATEGORY_LABELS = {
    "gross_receipts": "Gross receipts or sales",
    "returns_and_allowances": "Returns and allowances",
    "advertising": "Advertising",
    "car_and_truck": "Car and truck expenses",
    "commissions_and_fees": "Commissions and fees",
    "contract_labor": "Contract labor",
    "depreciation": "Depreciation and Section 179",
    "employee_benefits": "Employee benefit programs",
    "insurance": "Insurance",
    "interest_mortgage": "Mortgage interest",
    "interest_other": "Other interest",
    "legal_and_professional": "Legal and professional services",
    "office_expense": "Office expense",
    "rent_vehicles_machinery": "Rent or lease — vehicles and machinery",
    "rent_other": "Rent or lease — other business property",
    "repairs_and_maintenance": "Repairs and maintenance",
    "supplies": "Supplies",
    "taxes_and_licenses": "Taxes and licenses",
    "travel": "Travel",
    "meals": "Meals",
    "utilities": "Utilities",
    "wages": "Wages",
    "other_expense": "Other expenses",
}

REVENUE_CATEGORIES = (
    "gross_receipts",
    "returns_and_allowances",
)
EXPENSE_CATEGORIES = tuple(
    category for category in CATEGORY_LABELS if category not in REVENUE_CATEGORIES
)
ACCOUNT_CATEGORIES = {
    "revenue": REVENUE_CATEGORIES,
    "expense": EXPENSE_CATEGORIES,
}

# Seed only names whose tax treatment is inherent in the account itself.
# Generic COGS, payment fees, software, payroll/contractors, rent/utilities,
# refunds, interest, and every uncategorized account deliberately stay NULL.
DEFAULT_ACCOUNT_CATEGORIES = {
    "4000": "gross_receipts",
    "4100": "gross_receipts",
    "4900": "returns_and_allowances",
    "6010": "advertising",
    "6040": "office_expense",
    "6050": "legal_and_professional",
}


def choices_for_account_type(account_type):
    """Return the allowed stored categories for one Ledger account type."""
    return ACCOUNT_CATEGORIES.get(account_type, ())


def normalize_account_category(account_type, value):
    """Validate a user-provided mapping and normalize blank to unresolved."""
    category = (value or "").strip() or None
    if category is None:
        return None
    choices = choices_for_account_type(account_type)
    if not choices:
        raise ValueError("Only revenue and expense accounts can have a tax category.")
    if category not in choices:
        kind = "revenue" if account_type == "revenue" else "expense"
        raise ValueError(f"That is not a valid Schedule C {kind} category.")
    return category
