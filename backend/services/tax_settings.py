"""Persisted assumptions used by the live tax projection.

The projection API and Settings UI share this module so defaults, validation
choices, and the single ``kl_tax_settings`` row cannot drift between surfaces.
Money is stored as integer cents.
"""

import db
from backend.services import tax_projection


DEFAULTS = {
    "filing_status": "single",
    "state": "IA",
    "estimated_other_income": 0,
    "tax_year": tax_projection.TAX_YEAR,
    "business_type": "sole_prop",
}

FILING_STATUS_LABELS = {
    "single": "Single",
    "married_filing_jointly": "Married filing jointly",
    "married_filing_separately": "Married filing separately",
    "head_of_household": "Head of household",
}

# These are the entity types the current projection handles explicitly. Sole
# proprietors and single-member LLCs receive the supported Schedule SE
# estimate; S corporations and partnerships fail closed for professional
# review instead of borrowing that calculation.
BUSINESS_TYPE_LABELS = {
    "sole_prop": "Sole proprietorship",
    "single_member_llc": "Single-member LLC",
    "s_corp": "S corporation",
    "partnership": "Partnership",
}

STATE_LABELS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


def get():
    """Return the live projection assumptions, or its explicit defaults."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT filing_status, state, estimated_other_income, tax_year, "
            "business_type FROM kl_tax_settings WHERE id = 1"
        ).fetchone()
    return dict(row) if row is not None else dict(DEFAULTS)


def validate(filing_status, state, estimated_other_income, business_type):
    """Reject inputs the projection cannot interpret truthfully."""
    if filing_status not in FILING_STATUS_LABELS:
        raise ValueError("Choose a supported filing status.")
    if state not in STATE_LABELS:
        raise ValueError("Choose a valid U.S. state.")
    if business_type not in BUSINESS_TYPE_LABELS:
        raise ValueError("Choose a supported business type.")
    if not isinstance(estimated_other_income, int) or estimated_other_income < 0:
        raise ValueError("Estimated other income must be zero or more.")


def save(filing_status, state, estimated_other_income, business_type):
    """Upsert validated settings while locking the supported tax year."""
    validate(filing_status, state, estimated_other_income, business_type)

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO kl_tax_settings "
            "(id, filing_status, state, estimated_other_income, tax_year, business_type) "
            "VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "filing_status = excluded.filing_status, state = excluded.state, "
            "estimated_other_income = excluded.estimated_other_income, "
            "tax_year = excluded.tax_year, business_type = excluded.business_type",
            (filing_status, state, estimated_other_income,
             tax_projection.TAX_YEAR, business_type),
        )
