"""Versioned state-income-tax profiles for conservative projections.

Only profiles marked populated are allowed to produce a state number.  Every
other state fails closed so a missing or stale rule can never look like zero.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class LocalTaxProfile:
    mode: str  # none | optional | required | not_populated
    data_status: str  # populated | not_populated
    jurisdictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class StateTaxProfile:
    code: str
    name: str
    data_tier: int
    populated: bool
    rate_model: str  # zero | flat | graduated
    base_model: str  # none | federal_agi | federal_taxable_income
    federal_conformity: str  # full | partial | none | not_populated
    flat_rate: Decimal | None
    standard_deductions_cents: dict
    exemptions_cents: dict
    self_employment_add_backs: bool
    local_tax: LocalTaxProfile
    last_verified_tax_year: int | None
    source_url: str


STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

_NONE = LocalTaxProfile("none", "populated")
_NOT_POPULATED_LOCAL = LocalTaxProfile("not_populated", "not_populated")


def _profile(code, tier, rate_model="graduated", base_model="federal_agi",
             conformity="not_populated", rate=None, deductions=None,
             exemptions=None, local=_NOT_POPULATED_LOCAL, populated=False,
             verified=None, source=""):
    return StateTaxProfile(code, STATE_NAMES[code], tier, populated, rate_model,
                           base_model, conformity, rate, deductions or {},
                           exemptions or {}, False, local, verified, source)


_SOURCE = {
    "AK": "https://tax.alaska.gov/programs/individuals/",
    "FL": "https://floridarevenue.com/TaxLaw/Pages/TaxLaw.aspx",
    "NV": "https://tax.nv.gov/",
    "SD": "https://dor.sd.gov/individuals/",
    "TN": "https://www.tn.gov/revenue/taxes.html",
    "TX": "https://comptroller.texas.gov/taxes/publications/98-490.php",
    "WY": "https://revenue.wyo.gov/",
    "AZ": "https://azdor.gov/individuals/income-tax-filing-assistance/tax-rates",
    "CO": "https://tax.colorado.gov/individual-income-tax",
    "GA": "https://dor.georgia.gov/taxes/individual-income-tax",
    "ID": "https://tax.idaho.gov/individual-income-tax/",
    "IL": "https://tax.illinois.gov/individuals.html",
    "IA": "https://revenue.iowa.gov/taxes/tax-guidance/individual-income-tax/individual-income-tax-rates",
    "MS": "https://www.dor.ms.gov/general-information",
    "NC": "https://www.ncdor.gov/taxes-forms/individual-income-tax",
    "UT": "https://tax.utah.gov/forms-pubs-revisions/",
}


_POPULATED = {
    # Tier 1: no personal income tax, therefore an explicit zero.
    code: _profile(code, 1, "zero", "none", "none", Decimal("0"), local=_NONE,
                   populated=True, verified=2026, source=_SOURCE[code])
    for code in ("AK", "FL", "NV", "SD", "TN", "TX", "WY")
}
_POPULATED.update({
    # Tier 2 flat-rate profiles. Values are versioned for 2026 and sourced above.
    "AZ": _profile("AZ", 2, "flat", "federal_agi", "partial", Decimal("0.025"), local=_NONE, populated=True, verified=2026, source=_SOURCE["AZ"]),
    "CO": _profile("CO", 2, "flat", "federal_taxable_income", "partial", Decimal("0.044"), local=_NONE, populated=True, verified=2026, source=_SOURCE["CO"]),
    "GA": _profile("GA", 2, "flat", "federal_agi", "partial", Decimal("0.0499"), {"single": 1_500_000, "married_filing_jointly": 3_000_000, "married_filing_separately": 1_500_000, "head_of_household": 1_500_000}, {}, _NONE, True, 2026, _SOURCE["GA"]),
    "ID": _profile("ID", 2, "flat", "federal_taxable_income", "partial", Decimal("0.053"), local=_NONE, populated=True, verified=2026, source=_SOURCE["ID"]),
    "IL": _profile("IL", 2, "flat", "federal_agi", "partial", Decimal("0.0495"), exemptions={"single": 285_000, "married_filing_jointly": 570_000, "married_filing_separately": 285_000, "head_of_household": 285_000}, local=_NONE, populated=True, verified=2026, source=_SOURCE["IL"]),
    "IA": _profile("IA", 2, "flat", "federal_taxable_income", "partial", Decimal("0.038"), local=_NONE, populated=True, verified=2026, source=_SOURCE["IA"]),
    "MS": _profile("MS", 2, "flat", "federal_agi", "partial", Decimal("0.04"), exemptions={"single": 1_000_000, "married_filing_jointly": 1_000_000, "married_filing_separately": 1_000_000, "head_of_household": 1_000_000}, local=_NONE, populated=True, verified=2026, source=_SOURCE["MS"]),
    "NC": _profile("NC", 2, "flat", "federal_agi", "partial", Decimal("0.0399"), {"single": 1_275_000, "married_filing_jointly": 2_550_000, "married_filing_separately": 1_275_000, "head_of_household": 1_912_500}, local=_NONE, populated=True, verified=2026, source=_SOURCE["NC"]),
    "UT": _profile("UT", 2, "flat", "federal_taxable_income", "partial", Decimal("0.0445"), local=_NONE, populated=True, verified=2026, source=_SOURCE["UT"]),
})

# Explicitly Tier 3: NH interest/dividend tax and WA capital-gains tax cannot
# be resolved from the single undifferentiated estimated_other_income field.
_TIER3 = {code: _profile(code, 3) for code in STATE_NAMES if code not in _POPULATED}


def profile_for(state):
    return _POPULATED.get(state, _TIER3.get(state, _profile(state, 3)))


def _cents(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate(profile, filing_status, adjusted_gross_income_cents,
              federal_taxable_income_cents, tax_year):
    if not profile.populated or profile.rate_model == "graduated":
        return None, f"{profile.name} state tax is not available for this release."
    if profile.last_verified_tax_year != tax_year:
        return None, f"{profile.name} state tax rules have not been verified for {tax_year}."
    if profile.local_tax.data_status != "populated" or profile.local_tax.mode != "none":
        return None, f"{profile.name} local tax rules are not populated for this release."
    if profile.self_employment_add_backs:
        return None, f"{profile.name} self-employment state add-backs are not populated for this release."
    if profile.rate_model == "zero":
        return 0, None
    base = federal_taxable_income_cents if profile.base_model == "federal_taxable_income" else adjusted_gross_income_cents
    base -= profile.standard_deductions_cents.get(filing_status, 0)
    base -= profile.exemptions_cents.get(filing_status, 0)
    return _cents(Decimal(max(0, base)) * profile.flat_rate), None
