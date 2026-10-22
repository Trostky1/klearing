"""Tax APIs backed by the native Ledger and preserved legacy archive."""

from datetime import date
from math import ceil

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

import db
from backend.services import (sku_service, tax_exports, tax_financials,
                              tax_projection, tax_settings)


router = APIRouter()

APPROVAL_STATES = {0: "pending", 1: "approved", 2: "rejected"}


def _transaction_view(row):
    transaction = dict(row)
    transaction["approval_status"] = APPROVAL_STATES[transaction["user_approved"]]
    transaction["effective_category"] = (
        transaction["user_override_category"] or transaction["ai_category"])
    return transaction


@router.get("/api/tax/legacy-transactions")
def list_legacy_transactions(status: str = "approved"):
    """List preserved Wave Tax records from the retired read-only archive."""
    db.init_db()
    states = {"pending": 0, "approved": 1, "rejected": 2}
    if status not in {*states, "all"}:
        raise HTTPException(status_code=400, detail="status must be pending, approved, rejected, or all")
    query = "SELECT * FROM kl_transactions"
    params = []
    if status != "all":
        query += " WHERE user_approved = ?"
        params.append(states[status])
    query += " ORDER BY date DESC, id DESC"
    with db.get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        summary_row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN user_approved = 0 THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN user_approved = 1 THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN user_approved = 2 THEN 1 ELSE 0 END) AS rejected
            FROM kl_transactions
        """).fetchone()
    transactions = [_transaction_view(row) for row in rows]
    summary = {key: int(summary_row[key] or 0) for key in summary_row.keys()}
    return {"transactions": transactions, "count": len(transactions),
            "summary": summary, "read_only": True,
            "source": "legacy_wave_tax_archive"}


def _statement_date(value, field, default=None):
    if value is None:
        return default
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from exc


def _today():
    """Keep the app's calendar source explicit and regression-testable."""
    return date.today()


@router.get("/api/tax/statements/income")
def income_statement(start_date: str | None = None, end_date: str | None = None):
    """Income statement from the canonical native double-entry Ledger."""
    db.init_db()
    default_start, default_end = tax_financials.current_month_bounds()
    start = _statement_date(start_date, "start_date", default_start)
    end = _statement_date(end_date, "end_date", default_end)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date")
    return tax_financials.income_statement(start, end)


@router.get("/api/tax/statements/balance-sheet")
def balance_sheet(as_of: str | None = None):
    """Balance sheet from the canonical native double-entry Ledger."""
    db.init_db()
    return tax_financials.balance_sheet(
        _statement_date(as_of, "as_of", date.today().isoformat()))


def _tax_settings():
    return tax_settings.get()


@router.get("/api/tax/projection")
def tax_projection_endpoint(section_179_cents: int = 0,
                            home_office_square_feet: int = 0,
                            as_of: str | None = None):
    """Return a conservative 2026 estimate from categorized native Ledger income."""
    db.init_db()
    if section_179_cents < 0:
        raise HTTPException(status_code=400, detail="section_179_cents must be non-negative")
    if home_office_square_feet < 0:
        raise HTTPException(status_code=400, detail="home_office_square_feet must be non-negative")
    settings = _tax_settings()
    try:
        configured_year = int(settings["tax_year"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Tax settings contain an invalid tax year") from exc
    if configured_year != tax_projection.TAX_YEAR:
        raise HTTPException(status_code=400, detail="Tax projection currently supports tax year 2026 only")
    stale_detail = tax_projection.tax_year_staleness_detail(
        configured_year, _today())
    if stale_detail is not None:
        raise HTTPException(status_code=409, detail=stale_detail)
    projection_as_of = _statement_date(
        as_of, "as_of", min(_today(), date(configured_year, 12, 31)).isoformat())
    try:
        statement = tax_financials.income_statement(
            date(configured_year, 1, 1).isoformat(), projection_as_of)
        if not statement["is_tax_complete"]:
            raise HTTPException(
                status_code=409,
                detail=tax_financials.categorization_required_detail(statement),
            )
        return tax_projection.projection(
            settings, date.fromisoformat(projection_as_of), statement,
            section_179_cents, home_office_square_feet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _tax_export_response(content, media_type, filename):
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _tax_export_error(exc):
    raise HTTPException(status_code=503, detail=str(exc)) from exc


def _tax_export_categorization_error(exc):
    raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.get("/api/tax/exports/income-statement.pdf")
def export_income_statement_pdf(start_date: str | None = None,
                                end_date: str | None = None):
    """Download an approved-ledger income statement PDF."""
    db.init_db()
    default_start, default_end = tax_financials.current_month_bounds()
    start = _statement_date(start_date, "start_date", default_start)
    end = _statement_date(end_date, "end_date", default_end)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date")
    try:
        pdf = tax_exports.build_income_statement_pdf(start, end)
    except tax_exports.ExportCategorizationError as exc:
        _tax_export_categorization_error(exc)
    except tax_exports.ExportError as exc:
        _tax_export_error(exc)
    return _tax_export_response(pdf, "application/pdf",
                                f"Klearing_Income_Statement_{start}_to_{end}.pdf")


@router.get("/api/tax/exports/schedule-c.pdf")
def export_schedule_c_pdf(as_of: str | None = None):
    """Download a Schedule C-oriented planning summary PDF."""
    db.init_db()
    settings = _tax_settings()
    try:
        tax_year = int(settings["tax_year"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Tax settings contain an invalid tax year") from exc
    end = _statement_date(as_of, "as_of", min(date.today(), date(tax_year, 12, 31)).isoformat())
    if end[:4] != str(tax_year):
        raise HTTPException(status_code=400, detail="as_of must fall within the configured tax year")
    start = date(tax_year, 1, 1).isoformat()
    try:
        pdf = tax_exports.build_schedule_c_pdf(start, end)
    except tax_exports.ExportCategorizationError as exc:
        _tax_export_categorization_error(exc)
    except tax_exports.ExportError as exc:
        _tax_export_error(exc)
    return _tax_export_response(pdf, "application/pdf",
                                f"Klearing_Schedule_C_Summary_{tax_year}.pdf")


@router.get("/api/tax/exports/transactions.csv")
def export_tax_transactions_csv(start_date: str | None = None,
                                end_date: str | None = None):
    """Download canonical native-Ledger journal lines as a portable CSV."""
    db.init_db()
    default_start, default_end = tax_financials.current_month_bounds()
    start = _statement_date(start_date, "start_date", default_start)
    end = _statement_date(end_date, "end_date", default_end)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date")
    try:
        content = tax_exports.build_transactions_csv(start, end)
    except tax_exports.ExportCategorizationError as exc:
        _tax_export_categorization_error(exc)
    return _tax_export_response(
        content, "text/csv",
        f"Klearing_Ledger_Tax_Activity_{start}_to_{end}.csv")


@router.get("/api/tax/exports/legacy-transactions.csv")
def export_legacy_tax_transactions_csv(start_date: str | None = None,
                                       end_date: str | None = None):
    """Download approved Wave Tax queue rows from the preserved legacy archive."""
    db.init_db()
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=400,
            detail="Provide both start_date and end_date, or neither for the full archive",
        )
    start = _statement_date(start_date, "start_date", "0001-01-01")
    end = _statement_date(end_date, "end_date", "9999-12-31")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date")
    filename = "Klearing_Legacy_Wave_Tax_Archive.csv"
    if start_date is not None:
        filename = f"Klearing_Legacy_Wave_Tax_Transactions_{start}_to_{end}.csv"
    return _tax_export_response(
        tax_exports.build_legacy_transactions_csv(start, end), "text/csv",
        filename)


@router.get("/api/tax/exports/projection.pdf")
def export_projection_pdf(section_179_cents: int = 0,
                          home_office_square_feet: int = 0,
                          as_of: str | None = None):
    """Download the Phase 6 projection as a PDF without recalculating it."""
    try:
        projection = tax_projection_endpoint(
            section_179_cents, home_office_square_feet, as_of)
        pdf = tax_exports.build_projection_pdf(projection)
    except tax_exports.ExportError as exc:
        _tax_export_error(exc)
    return _tax_export_response(
        pdf, "application/pdf", f"Klearing_Tax_Projection_{projection['as_of']}.pdf")


@router.get("/api/tax/planning")
def planning():
    """Return Shopify/manual unit inputs with period-matched Ledger costs."""
    db.init_db()
    config = sku_service.get_units_inputs()
    sku_metrics = sku_service.parse_sku_metrics(config["date_range_days"])
    costs = sku_service.ledger_costs(config["date_range_days"])
    if not costs["statement"]["is_tax_complete"]:
        raise HTTPException(
            status_code=409,
            detail=tax_financials.categorization_required_detail(
                costs["statement"], output_label="planning result",
                consequence="No planning amount is shown.",
            ),
        )
    units = sku_service.resolve_sp_q_vc(
        config, sku_metrics, costs["variable_cost_cents"])
    fixed_costs = costs["fixed_cost_cents"]
    target_income = int(config["target_income_cents"] or 0)
    margin = units["sp_cents"] - units["vc_per_unit_cents"]
    q = units["q"]
    breakeven = ceil(fixed_costs / margin) if margin > 0 else 0
    target_q = ceil((fixed_costs + target_income) / margin) if margin > 0 else 0
    min_price = ceil((units["vc_per_unit_cents"] * q + fixed_costs + target_income) / q) if q else 0
    return {
        "inputs": {**units, "fc_cents": fixed_costs,
                   "target_income_cents": target_income},
        "outputs": {
            "contribution_margin_cents": margin,
            "contribution_margin_pct": margin / units["sp_cents"]
            if units["sp_cents"] else 0,
            "breakeven_q": breakeven,
            "target_q": target_q,
            "current_profit_cents": (units["sp_cents"] * q)
            - (units["vc_per_unit_cents"] * q) - fixed_costs,
            "units_to_target": max(0, target_q - q),
            "min_sp_for_target_cents": min_price,
        },
        "per_sku": sku_metrics["per_sku"],
        "blended": sku_metrics["blended"],
        "config": config,
        "cost_basis": {
            "source": tax_financials.BASIS,
            "period": costs["period"],
            "variable_cost_cents": costs["variable_cost_cents"],
            "fixed_cost_cents": fixed_costs,
            "variable_cost_categories": list(
                sku_service.VARIABLE_COST_CATEGORIES),
            "fixed_cost_categories": list(sku_service.FIXED_COST_CATEGORIES),
        },
    }


@router.patch("/api/tax/planning/config")
def update_planning_config(body: dict | None = Body(default=None)):
    """Persist only the provided, validated Tax Planning configuration values."""
    db.init_db()
    body = body or {}
    allowed = {"source", "manual_sp_cents", "manual_q", "manual_vc_cents",
               "target_income_cents", "date_range_days"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail="Unknown Planning configuration field")
    if "source" in body and body["source"] not in {"shopify", "manual"}:
        raise HTTPException(status_code=400, detail="source must be shopify or manual")
    for field in allowed - {"source"}:
        if field in body and (type(body[field]) is not int or body[field] < 0):
            raise HTTPException(status_code=400, detail=f"{field} must be a non-negative integer")
    if "date_range_days" in body and body["date_range_days"] not in {7, 30, 90}:
        raise HTTPException(status_code=400, detail="date_range_days must be 7, 30, or 90")
    config = sku_service.get_units_inputs()
    updates = {field: body[field] for field in allowed if field in body}
    if updates:
        assignments = ", ".join(f"{field} = ?" for field in updates)
        with db.get_connection() as conn:
            conn.execute(f"UPDATE kl_units_config SET {assignments} WHERE id = 1",
                         list(updates.values()))
    return sku_service.get_units_inputs()
