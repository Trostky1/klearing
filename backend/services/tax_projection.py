"""Conservative 2026 federal and state tax planning estimates.

This service estimates from a caller-supplied financial statement. It is not a
return-preparation engine: credits, withholding, prior-year safe harbors, and
entity-specific payroll or K-1 treatment are intentionally disclosed instead
of inferred from incomplete bookkeeping data.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from backend.services.state_tax import calculate as calculate_state_tax
from backend.services.state_tax import profile_for

TAX_YEAR = 2026
IOWA_FLAT_RATE = Decimal("0.038")
SE_EARNINGS_RATE = Decimal("0.9235")
SOCIAL_SECURITY_RATE = Decimal("0.124")
MEDICARE_RATE = Decimal("0.029")
ADDITIONAL_MEDICARE_RATE = Decimal("0.009")
SOCIAL_SECURITY_WAGE_BASE_CENTS = 18_450_000
SECTION_179_LIMIT_CENTS = 256_000_000
HOME_OFFICE_RATE_CENTS_PER_SQ_FT = 500
HOME_OFFICE_MAX_SQ_FT = 300

STANDARD_DEDUCTIONS_CENTS = {
    "single": 1_610_000,
    "married_filing_jointly": 3_220_000,
    "married_filing_separately": 1_610_000,
    "head_of_household": 2_415_000,
}

# Upper bounds and marginal rates for 2026 Form 1040-ES rate schedules.
FEDERAL_BRACKETS = {
    "single": ((1_240_000, "0.10"), (5_040_000, "0.12"),
               (10_570_000, "0.22"), (20_177_500, "0.24"),
               (25_622_500, "0.32"), (64_060_000, "0.35"), (None, "0.37")),
    "married_filing_jointly": ((2_480_000, "0.10"), (10_080_000, "0.12"),
                                (21_140_000, "0.22"), (40_355_000, "0.24"),
                                (51_245_000, "0.32"), (76_870_000, "0.35"),
                                (None, "0.37")),
    "married_filing_separately": ((1_240_000, "0.10"), (5_040_000, "0.12"),
                                   (10_570_000, "0.22"), (20_177_500, "0.24"),
                                   (25_622_500, "0.32"), (38_435_000, "0.35"),
                                   (None, "0.37")),
    "head_of_household": ((1_770_000, "0.10"), (6_745_000, "0.12"),
                           (10_570_000, "0.22"), (20_175_000, "0.24"),
                           (25_620_000, "0.32"), (64_060_000, "0.35"),
                           (None, "0.37")),
}

ADDITIONAL_MEDICARE_THRESHOLDS_CENTS = {
    "single": 20_000_000,
    "married_filing_jointly": 25_000_000,
    "married_filing_separately": 12_500_000,
    "head_of_household": 20_000_000,
}

QUARTERLY_DUE_DATES = (
    "2026-04-15", "2026-06-15", "2026-09-15", "2027-01-15",
)


def tax_year_staleness_detail(configured_year, today=None):
    """Describe why an out-of-year projection must not be shown.

    Tax rules are deliberately versioned by ``TAX_YEAR``.  Once the calendar
    moves outside the configured year, a 2026 calculation is historical data,
    not a current estimate.  Keep this check independent of the calculation
    so API callers can fail closed before producing any money values.
    """
    current_date = today or date.today()
    if current_date.year == configured_year:
        return None
    current_year = current_date.year
    return {
        "code": "tax_year_stale",
        "tax_year": configured_year,
        "current_year": current_year,
        "title": f"Projections have not been updated for {current_year} yet",
        "message": (
            f"Klearing's projection rules are configured for {configured_year}. "
            f"No {configured_year} projection is shown as a current "
            f"{current_year} estimate."
        ),
    }


def _cents(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _tax_from_brackets(taxable_income_cents, filing_status):
    remaining = max(0, taxable_income_cents)
    lower = 0
    tax = Decimal("0")
    for upper, rate in FEDERAL_BRACKETS[filing_status]:
        width = remaining - lower if upper is None else min(remaining, upper) - lower
        if width > 0:
            tax += Decimal(width) * Decimal(rate)
        if upper is None or remaining <= upper:
            break
        lower = upper
    return _cents(tax)


def _annualized_net_income(as_of, statement):
    start = date(TAX_YEAR, 1, 1)
    days_elapsed = (as_of - start).days + 1
    ytd_net_income = int(statement["net_income_cents"])
    return ytd_net_income, _cents(
        Decimal(ytd_net_income) * Decimal(365) / Decimal(days_elapsed))


def projection(settings, as_of, statement, section_179_cents=0,
               home_office_square_feet=0):
    """Estimate 2026 taxes from the supplied YTD statement and deductions.

    State output is calculated only by a current, populated state profile;
    unsupported or stale state rules return ``None`` and are disclosed.
    """
    filing_status = settings["filing_status"]
    business_type = settings["business_type"]
    if filing_status not in STANDARD_DEDUCTIONS_CENTS:
        raise ValueError("Unsupported filing status in Tax settings")
    if int(settings["tax_year"]) != TAX_YEAR:
        raise ValueError("Tax projection currently supports tax year 2026 only")
    if as_of.year != TAX_YEAR:
        raise ValueError("as_of must fall within the configured tax year")

    ytd_net_income, annualized_net_income = _annualized_net_income(as_of, statement)
    section_179_requested = max(0, section_179_cents)
    section_179_limit = SECTION_179_LIMIT_CENTS
    # A Section 179 deduction cannot exceed business income; unused amounts may carry over.
    section_179_allowed = min(section_179_requested, section_179_limit,
                              max(0, annualized_net_income))
    home_office_allowed = min(max(0, home_office_square_feet), HOME_OFFICE_MAX_SQ_FT)
    home_office_cents = home_office_allowed * HOME_OFFICE_RATE_CENTS_PER_SQ_FT
    home_office_cents = min(home_office_cents,
                            max(0, annualized_net_income - section_179_allowed))
    business_income = annualized_net_income - section_179_allowed - home_office_cents
    other_income = max(0, int(settings["estimated_other_income"] or 0))

    # Schedule SE starts with 92.35% of self-employment profit. S-corp and
    # partnership K-1/payroll treatment cannot be derived from this ledger.
    se_eligible = business_type in {"sole_prop", "single_member_llc"}
    se_earnings = _cents(Decimal(max(0, business_income)) * SE_EARNINGS_RATE) if se_eligible else 0
    social_security_tax = _cents(Decimal(min(se_earnings, SOCIAL_SECURITY_WAGE_BASE_CENTS))
                                 * SOCIAL_SECURITY_RATE)
    medicare_tax = _cents(Decimal(se_earnings) * MEDICARE_RATE)
    additional_medicare = _cents(Decimal(max(
        0, se_earnings - ADDITIONAL_MEDICARE_THRESHOLDS_CENTS[filing_status]))
        * ADDITIONAL_MEDICARE_RATE)
    self_employment_tax = social_security_tax + medicare_tax + additional_medicare
    half_se_deduction = self_employment_tax // 2

    adjusted_gross_income = business_income + other_income - half_se_deduction
    taxable_before_qbi = max(0, adjusted_gross_income - STANDARD_DEDUCTIONS_CENTS[filing_status])
    qbi_income = max(0, business_income - half_se_deduction)
    qbi_deduction = 0
    if qbi_income >= 100_000:
        qbi_deduction = min(
            taxable_before_qbi,
            max(40_000, min(_cents(Decimal(qbi_income) * Decimal("0.20")),
                            _cents(Decimal(taxable_before_qbi) * Decimal("0.20")))),
        )
    federal_taxable_income = max(0, taxable_before_qbi - qbi_deduction)
    federal_income_tax = _tax_from_brackets(federal_taxable_income, filing_status)

    state_profile = profile_for(settings["state"])
    state_tax, state_warning = calculate_state_tax(
        state_profile, filing_status, adjusted_gross_income, federal_taxable_income,
        TAX_YEAR)
    # Keep the legacy Iowa field stable while exposing a state-generic result.
    iowa_tax = state_tax if settings["state"] == "IA" else None
    total_estimated_tax = federal_income_tax + self_employment_tax + (state_tax or 0)
    requires_review = business_type not in {"sole_prop", "single_member_llc"}
    warnings = [
        "Projection excludes withholding, credits, payments already made, capital gains, "
        "itemized deductions, and prior-year safe-harbor calculations.",
        "Estimated other income is treated as ordinary taxable income, not as wages or "
        "self-employment income.",
        "Section 179 and home-office inputs are planning-only and must be supported by "
        "eligible property or qualified business use.",
        "Section 179's property-cost phaseout is not modeled because total qualifying "
        "property cost is not tracked yet.",
        "QBI is a simplified estimate and does not evaluate wage, property, specified "
        "service-trade, or capital-gain limitations.",
    ]
    if state_warning:
        warnings.append(state_warning)
    if requires_review:
        warnings.append(
            "Partnership and S-corp self-employment, payroll, K-1, and QBI treatment "
            "requires tax-professional review; their SE tax estimate is not calculated.")

    result = {
        "tax_year": TAX_YEAR,
        "as_of": as_of.isoformat(),
        "basis": statement["basis"],
        "is_estimate_complete": not requires_review and state_tax is not None,
        "source_entry_count": statement.get(
            "source_entry_count", statement.get("approved_transaction_count", 0)),
        "income": {
            "ytd_net_income_cents": ytd_net_income,
            "annualized_net_income_cents": annualized_net_income,
            "days_elapsed": (as_of - date(TAX_YEAR, 1, 1)).days + 1,
            "annualization_method": "YTD net income multiplied by 365 / days elapsed",
            "estimated_other_income_cents": other_income,
        },
        "deductions": {
            "section_179_requested_cents": section_179_requested,
            "section_179_allowed_cents": section_179_allowed,
            "section_179_limit_cents": section_179_limit,
            "home_office_square_feet": home_office_allowed,
            "home_office_deduction_cents": home_office_cents,
            "standard_deduction_cents": STANDARD_DEDUCTIONS_CENTS[filing_status],
            "half_self_employment_tax_deduction_cents": half_se_deduction,
            "qbi_deduction_cents": qbi_deduction,
        },
        "taxes": {
            "federal_income_tax_cents": federal_income_tax,
            "self_employment_tax_cents": self_employment_tax,
            "social_security_tax_cents": social_security_tax,
            "medicare_tax_cents": medicare_tax,
            "additional_medicare_tax_cents": additional_medicare,
            "state_income_tax_cents": state_tax,
            "iowa_income_tax_cents": iowa_tax,
            "total_estimated_tax_cents": total_estimated_tax,
        },
        "taxable_income": {
            "adjusted_gross_income_cents": adjusted_gross_income,
            "federal_taxable_before_qbi_cents": taxable_before_qbi,
            "federal_taxable_income_cents": federal_taxable_income,
            "qbi_income_cents": qbi_income,
        },
        "quarterly_estimates": [
            {"due_date": due_date,
             "amount_cents": _cents(Decimal(total_estimated_tax) / Decimal(4)),
             "is_past_due": due_date < date.today().isoformat()}
            for due_date in QUARTERLY_DUE_DATES
        ],
        "assumptions": {
            "filing_status": filing_status,
            "business_type": business_type,
            "state": settings["state"],
            "iowa_flat_rate": float(IOWA_FLAT_RATE) if iowa_tax is not None else None,
            "self_employment_earnings_rate": float(SE_EARNINGS_RATE),
            "social_security_wage_base_cents": SOCIAL_SECURITY_WAGE_BASE_CENTS,
        },
        "warnings": warnings,
        "state": {
            "code": state_profile.code,
            "name": state_profile.name,
            "available": state_tax is not None,
            "tax_cents": state_tax,
            "warning": state_warning,
            "data_tier": state_profile.data_tier,
            "last_verified_tax_year": state_profile.last_verified_tax_year,
            "local_tax_mode": state_profile.local_tax.mode,
        },
    }
    # Preserve the frozen legacy oracle's exact public shape while production
    # native-Ledger statements use the truthful source_entry_count name.
    if "approved_transaction_count" in statement:
        result["approved_transaction_count"] = statement["approved_transaction_count"]
    return result
