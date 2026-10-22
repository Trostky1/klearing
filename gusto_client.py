"""
Gusto API client (fixed, built-in integration — optional to connect).

Connecting Gusto adds: an automatic payroll row on Summary's expense
table, Gusto contractor payments in the 1099 tracker (cross-referenced
against Wave history for double-counts), and a labor-cost trend on
Growth. Read-only against Gusto — Klearing never runs or edits payroll.

The API token lives in the OS-keychain-backed secrets store under
"klearing_gusto_api_key". Company id/name and pay-schedule facts are
cached in settings at connect/re-test time so page renders don't need
a company lookup first.
"""

import time
from datetime import date

import requests

import connectivity
import db
import secrets_store

API_BASE = "https://api.gusto.com/v1"
SECRET_NAME = "klearing_gusto_api_key"

COMPANY_ID_KEY = "gusto_company_id"
COMPANY_NAME_KEY = "gusto_company_name"

_CACHE = {}
_CACHE_TTL = 600  # render-path calls only; connect/test always go live


def _cached(name, fn):
    now = time.time()
    hit = _CACHE.get(name)
    if hit and hit[0] > now:
        return hit[1]
    value = fn()
    _CACHE[name] = (now + _CACHE_TTL, value)
    return value


def reset_cache():
    _CACHE.clear()


class GustoHTTPError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"Gusto returned HTTP {status}: {body[:300]}")
        self.status = status


def get_key():
    return secrets_store.get_secret(SECRET_NAME)


def is_configured():
    return bool(get_key()) and bool(db.get_setting(COMPANY_ID_KEY))


def validate_key_format(key):
    """None if plausible, else a specific error. No network call."""
    key = (key or "").strip()
    if not key:
        return "Gusto API token is empty. Paste it in (don't retype it)."
    if len(key) < 20:
        return (f"The token looks too short ({len(key)} characters). Part "
                "of it was probably cut off in the paste — copy it again "
                "in one go.")
    return None


def _get(key, path, params=None):
    response = requests.get(
        f"{API_BASE}{path}", params=params or {},
        headers={"Authorization": f"Bearer {key}",
                 "Accept": "application/json"},
        timeout=20)
    connectivity.raise_for_rate_limit("Gusto", response.status_code)
    if response.status_code != 200:
        raise GustoHTTPError(response.status_code, response.text)
    return response.json()


def _first_company(key):
    """The token's first company — Klearing is a one-business tool."""
    companies = _get(key, "/companies")
    if isinstance(companies, dict):
        companies = companies.get("companies") or []
    if not companies:
        raise RuntimeError("The Gusto token works but has no company "
                           "attached to it.")
    return companies[0]


def test_connection(key):
    """Live check. On success the company id/name are cached in
    settings so is_configured() and page renders can answer without a
    lookup. Returns connectivity.TestResult."""
    format_error = validate_key_format(key)
    if format_error:
        return connectivity.fail(format_error)
    try:
        company = _first_company(key)
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except GustoHTTPError as exc:
        if exc.status in (401, 403):
            return connectivity.fail(
                "Gusto rejected the token. It may have expired or been "
                "revoked — create a new API token in Gusto and paste it "
                "again.")
        return connectivity.fail(str(exc))
    except RuntimeError as exc:
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("Gusto", exc)
    company_id = str(company.get("id") or company.get("uuid") or "")
    name = (company.get("name") or company.get("trade_name")
            or "Gusto company")
    db.set_setting(COMPANY_ID_KEY, company_id)
    db.set_setting(COMPANY_NAME_KEY, name)
    return connectivity.ok(f'Gusto connected: "{name}".')


def _company_id():
    return db.get_setting(COMPANY_ID_KEY) or ""


def payrolls_in_range(start_iso, end_iso):
    """Processed payroll runs in a date range:
    [{'date', 'gross', 'employer_taxes', 'net'}], oldest first."""
    raw = _get(get_key(), f"/companies/{_company_id()}/payrolls",
               {"start_date": start_iso, "end_date": end_iso,
                "processed": "true", "include": "totals"})
    if isinstance(raw, dict):
        raw = raw.get("payrolls") or []
    runs = []
    for p in raw:
        totals = p.get("totals") or {}
        check_date = (p.get("check_date")
                      or (p.get("pay_period") or {}).get("end_date") or "")
        runs.append({
            "date": check_date[:10],
            "gross": float(totals.get("gross_pay") or 0),
            "employer_taxes": float(totals.get("employer_taxes") or 0),
            "net": float(totals.get("net_pay") or 0),
        })
    runs.sort(key=lambda r: r["date"])
    return runs


def contractor_payments_this_year():
    """Contractor payments year-to-date, grouped:
    [{'vendor', 'total', 'count'}]. Cached (render path)."""
    return _cached("contractor_payments", _contractor_payments_live)


def _contractor_payments_live():
    year = date.today().year
    raw = _get(get_key(),
               f"/companies/{_company_id()}/contractor_payments",
               {"start_date": f"{year}-01-01",
                "end_date": date.today().isoformat()})
    if isinstance(raw, dict):
        raw = (raw.get("contractor_payments") or raw.get("payments")
               or [])
    agg = {}
    for p in raw:
        name = (p.get("contractor_name") or p.get("name")
                or str(p.get("contractor_id") or "Contractor"))
        entry = agg.setdefault(name, {"total": 0.0, "count": 0})
        entry["total"] += float(p.get("wage") or p.get("amount") or 0)
        entry["count"] += 1
    return [{"vendor": vendor, **entry}
            for vendor, entry in sorted(agg.items(),
                                        key=lambda kv: -kv[1]["total"])]


def employee_count():
    """Active employee headcount, or None if unavailable."""
    try:
        raw = _get(get_key(), f"/companies/{_company_id()}/employees",
                   {"terminated": "false"})
        if isinstance(raw, dict):
            raw = raw.get("employees") or []
        return len(raw)
    except Exception:
        return None


def monthly_payroll(months=6):
    """([(month_label, gross+employer_taxes $)] oldest first, headcount
    or None) — Growth's labor-cost trend. Cached (render path)."""
    return _cached(f"monthly_payroll:{months}",
                   lambda: _monthly_payroll_live(months))


def _monthly_payroll_live(months):
    today = date.today()
    year, month = today.year, today.month
    start_year, start_month = year, month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    runs = payrolls_in_range(f"{start_year:04d}-{start_month:02d}-01",
                             today.isoformat())
    by_month = {}
    for run in runs:
        key = run["date"][:7]
        by_month[key] = (by_month.get(key, 0.0)
                         + run["gross"] + run["employer_taxes"])
    series = []
    seq = []
    for k in range(months):
        mm, yy = month - k, year
        while mm <= 0:
            mm += 12
            yy -= 1
        seq.append((yy, mm))
    for yy, mm in reversed(seq):
        series.append((date(yy, mm, 1).strftime("%b"),
                       round(by_month.get(f"{yy:04d}-{mm:02d}", 0.0), 2)))
    return series, employee_count()


def company_facts():
    """{'name', 'pay_schedule', 'last_payroll'} for the Connections
    card, best-effort live. Cached."""
    return _cached("company_facts", _company_facts_live)


def _company_facts_live():
    key = get_key()
    company_id = _company_id()
    facts = {"name": db.get_setting(COMPANY_NAME_KEY) or "Gusto company",
             "pay_schedule": None, "last_payroll": None}
    try:
        schedules = _get(key, f"/companies/{company_id}/pay_schedules")
        if isinstance(schedules, dict):
            schedules = schedules.get("pay_schedules") or []
        if schedules:
            facts["pay_schedule"] = (schedules[0].get("frequency")
                                     or schedules[0].get("name"))
    except Exception:
        pass
    try:
        today = date.today()
        runs = payrolls_in_range(f"{today.year - 1:04d}-01-01",
                                 today.isoformat())
        if runs:
            facts["last_payroll"] = runs[-1]["date"]
    except Exception:
        pass
    return facts
