"""Tax exports from Klearing's canonical native double-entry Ledger."""

import csv
import io
from collections import defaultdict
from decimal import Decimal
from xml.sax.saxutils import escape

import accountant_export
import db
import tax_categories
from backend.services import tax_financials


class ExportError(RuntimeError):
    """A calm, user-facing Tax export failure."""


class ExportCategorizationError(ExportError):
    """An export was refused because tax-sensitive accounts are unresolved."""

    def __init__(self, detail):
        super().__init__(detail["message"])
        self.detail = detail


def _require_pdf():
    if not accountant_export.PDF_AVAILABLE:
        raise ExportError(accountant_export.REPORTLAB_INSTALL_HINT)


def _approved_transactions(start_date, end_date):
    """Legacy Wave rows retained only for the explicit archive export."""
    query = """
        SELECT id, date, description, merchant, amount_cents, currency,
               raw_wave_category, ai_category, ai_confidence, ai_reasoning,
               user_override_category, synced_at,
               COALESCE(NULLIF(user_override_category, ''), ai_category,
                        'uncategorized') AS effective_category
        FROM kl_transactions
        WHERE user_approved = 1 AND date >= ? AND date <= ?
        ORDER BY date ASC, id ASC
    """
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(query, (start_date, end_date))]


def _money(cents):
    return accountant_export._money(Decimal(cents) / Decimal(100))


def _expense_money(cents):
    return f"-{_money(cents)}" if cents else "$0.00"


def _require_tax_complete(statement, output_label):
    if statement["is_tax_complete"]:
        return
    raise ExportCategorizationError(
        tax_financials.categorization_required_detail(
            statement, output_label=output_label,
            consequence="No file was generated.",
        )
    )


def _pdf_document(title, subtitle, sections):
    _require_pdf()
    styles = accountant_export._styles()
    story = [
        accountant_export.Paragraph(escape(title), styles["KLTitle"]),
        accountant_export.Paragraph(escape(subtitle), styles["KLCoverMeta"]),
        accountant_export.Spacer(1, 12),
    ]
    for heading, content in sections:
        story.append(accountant_export.Paragraph(heading, styles["KLSection"]))
        if isinstance(content, tuple):
            headers, rows, widths, money_columns = content
            story.append(accountant_export._section_table(
                headers, rows, col_widths=widths, money_cols=money_columns))
        else:
            story.append(accountant_export.Paragraph(escape(content), styles["KLBody"]))
    buffer = io.BytesIO()
    document = accountant_export.SimpleDocTemplate(
        buffer, pagesize=accountant_export.letter,
        topMargin=0.75 * accountant_export.inch,
        bottomMargin=0.9 * accountant_export.inch,
        leftMargin=0.75 * accountant_export.inch,
        rightMargin=0.75 * accountant_export.inch,
        title=title,
    )
    document.build(story, onFirstPage=accountant_export._footer,
                   onLaterPages=accountant_export._footer)
    return buffer.getvalue()


def build_income_statement_pdf(start_date, end_date):
    """Render the canonical native-Ledger income statement as a PDF."""
    statement = tax_financials.income_statement(start_date, end_date)
    income = statement["income"]
    cogs = statement["cost_of_goods_sold"]
    operating = statement["operating_expenses"]
    overview_rows = [
        ["Gross receipts", _money(income["gross_receipts_cents"])],
        ["Returns and allowances", _expense_money(income["returns_and_allowances_cents"])],
        ["Net revenue", _money(income["net_revenue_cents"])],
        ["Cost of goods sold", _expense_money(cogs["total_cents"])],
        ["Gross profit", _money(statement["gross_profit_cents"])],
        ["Gross margin", f"{statement['gross_margin']:.1%}"],
        ["Operating expenses", _expense_money(operating["total_cents"])],
        ["Net income", _money(statement["net_income_cents"])],
        ["Net margin", f"{statement['net_margin']:.1%}"],
    ]
    cogs_rows = [[row["label"], _money(row["amount_cents"])]
                 for row in cogs["categories"]]
    operating_rows = [[row["label"], _money(row["amount_cents"])]
                      for row in operating["categories"]]
    sections = [
        ("Statement", (["Line", "Amount"], overview_rows,
                       [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Cost of Goods Sold", (["Category", "Amount"], cogs_rows or [["None", "$0.00"]],
                                  [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Operating Expenses", (["Category", "Amount"], operating_rows or [["None", "$0.00"]],
                                  [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Basis", "Native double-entry Ledger entries in the selected period."),
    ]
    if not statement["is_tax_complete"]:
        count = statement["unresolved_account_count"]
        noun = "account" if count == 1 else "accounts"
        sections.append((
            "Tax categorization status",
            f"{count} {noun} still need Schedule C categorization. All Ledger "
            "amounts remain included in this financial statement, but tax exports "
            "are blocked until the mappings are complete.",
        ))
    return _pdf_document(
        "Klearing Income Statement", f"{start_date} to {end_date}", sections)


def schedule_c_summary(start_date, end_date):
    """Aggregate a complete native-Ledger P&L into Schedule C categories."""
    statement = tax_financials.income_statement(start_date, end_date)
    _require_tax_complete(statement, "Schedule C export")

    def grouped(rows):
        amounts = defaultdict(int)
        for row in rows:
            amounts[row["category"]] += int(row["amount_cents"])
        return [
            {
                "category": category,
                "label": tax_categories.CATEGORY_LABELS[category],
                "amount_cents": amount,
            }
            for category, amount in sorted(
                amounts.items(), key=lambda item: tax_categories.CATEGORY_LABELS[item[0]])
            if amount != 0
        ]

    income = statement["income"]
    cogs_rows = grouped(statement["cost_of_goods_sold"]["categories"])
    operating_rows = grouped(statement["operating_expenses"]["categories"])
    total_cogs = sum(row["amount_cents"] for row in cogs_rows)
    total_operating = sum(row["amount_cents"] for row in operating_rows)
    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "basis": tax_financials.BASIS,
        "source_entry_count": statement["source_entry_count"],
        "gross_receipts_cents": income["gross_receipts_cents"],
        "returns_and_allowances_cents": income["returns_and_allowances_cents"],
        "net_receipts_cents": income["net_revenue_cents"],
        "cost_of_goods_sold": cogs_rows,
        "total_cost_of_goods_sold_cents": total_cogs,
        "gross_profit_cents": statement["gross_profit_cents"],
        "operating_expenses": operating_rows,
        "total_operating_expenses_cents": total_operating,
        "total_expenses_cents": total_cogs + total_operating,
        "tentative_profit_cents": statement["net_income_cents"],
    }


def build_schedule_c_pdf(start_date, end_date):
    """Render a labeled planning summary, not an IRS filing form."""
    summary = schedule_c_summary(start_date, end_date)
    income_rows = [
        ["Gross receipts or sales", _money(summary["gross_receipts_cents"])],
        ["Returns and allowances", _expense_money(summary["returns_and_allowances_cents"])],
        ["Net receipts", _money(summary["net_receipts_cents"])],
    ]
    cogs_rows = [[row["label"], _money(row["amount_cents"])]
                 for row in summary["cost_of_goods_sold"]]
    cogs_rows.append([
        "Total cost of goods sold",
        _money(summary["total_cost_of_goods_sold_cents"]),
    ])
    expense_rows = [[row["label"], _money(row["amount_cents"])]
                    for row in summary["operating_expenses"]]
    expense_rows.append([
        "Total operating expenses",
        _money(summary["total_operating_expenses_cents"]),
    ])
    sections = [
        ("Income", (["Schedule C summary line", "Amount"], income_rows,
                     [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Cost of Goods Sold", (["Schedule C category", "Amount"], cogs_rows,
                                 [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Operating Expenses", (["Schedule C category", "Amount"], expense_rows,
                       [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Tentative Profit", (["Line", "Amount"], [["Net profit or loss", _money(summary["tentative_profit_cents"])]],
                               [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Important", "Planning summary only - review eligibility, deductions, and the final IRS form before filing."),
    ]
    return _pdf_document(
        "Klearing Schedule C Summary", f"{start_date} to {end_date}", sections)


def _journal_lines(start_date, end_date):
    with db.get_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT je.id AS journal_entry_id, jl.id AS journal_line_id, "
            "je.date, je.memo, je.created_by, je.source_reference, "
            "a.code AS account_code, a.name AS account_name, a.type AS account_type, "
            "a.tax_category, jl.debit_amount, jl.credit_amount "
            "FROM journal_entries je "
            "JOIN journal_lines jl ON jl.journal_entry_id = je.id "
            "JOIN accounts a ON a.id = jl.account_id "
            "WHERE je.date >= ? AND je.date <= ? "
            "ORDER BY je.date, je.id, jl.id",
            (start_date, end_date),
        ).fetchall()]


def _decimal_cents(cents):
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def build_transactions_csv(start_date, end_date):
    """Return complete native double-entry journal lines in portable CSV."""
    statement = tax_financials.income_statement(start_date, end_date)
    _require_tax_complete(statement, "Ledger tax CSV export")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "journal_entry_id", "journal_line_id", "date", "memo", "created_by",
        "source_reference", "account_code", "account_name", "account_type",
        "schedule_c_category", "schedule_c_category_label", "debit", "credit",
    ])
    for row in _journal_lines(start_date, end_date):
        category = row["tax_category"]
        writer.writerow([
            row["journal_entry_id"], row["journal_line_id"], row["date"],
            row["memo"], row["created_by"], row["source_reference"] or "",
            row["account_code"], row["account_name"], row["account_type"],
            category or "not_applicable",
            tax_categories.CATEGORY_LABELS[category] if category else "Not applicable",
            _decimal_cents(row["debit_amount"]),
            _decimal_cents(row["credit_amount"]),
        ])
    return output.getvalue()


def build_legacy_transactions_csv(start_date, end_date):
    """Preserve approved Wave Tax queue rows for the read-only legacy archive."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "record_source", "legacy_approval_status", "transaction_id", "date",
        "description", "merchant", "amount", "currency", "wave_category",
        "schedule_c_category", "ai_confidence", "ai_reasoning", "synced_at",
    ])
    for row in _approved_transactions(start_date, end_date):
        writer.writerow([
            "legacy_wave_tax_archive", "approved", row["id"], row["date"],
            row["description"] or "", row["merchant"] or "",
            _decimal_cents(row["amount_cents"]), row["currency"] or "USD",
            row["raw_wave_category"] or "", row["effective_category"],
            "" if row["ai_confidence"] is None else row["ai_confidence"],
            row["ai_reasoning"] or "", row["synced_at"] or "",
        ])
    return output.getvalue()


def build_projection_pdf(projection):
    """Render caller-validated canonical projection data without recalculating."""
    income = projection["income"]
    deductions = projection["deductions"]
    taxes = projection["taxes"]
    state = projection.get("state", {
        "name": "Iowa", "available": taxes.get("iowa_income_tax_cents") is not None,
    })
    state_cents = taxes.get("state_income_tax_cents", taxes.get("iowa_income_tax_cents"))
    tax_rows = [
        ["Federal income tax", _money(taxes["federal_income_tax_cents"])],
        ["Self-employment tax", _money(taxes["self_employment_tax_cents"])],
        [f"{state['name']} income tax", "Not available" if not state["available"]
         else _money(state_cents)],
        ["Total estimated tax" if state["available"] else "Federal + self-employment only",
         _money(taxes["total_estimated_tax_cents"])],
    ]
    deduction_rows = [
        ["Section 179 deduction", _money(deductions["section_179_allowed_cents"])],
        ["Home office deduction", _money(deductions["home_office_deduction_cents"])],
        ["QBI deduction", _money(deductions["qbi_deduction_cents"])],
        ["Half SE tax deduction", _money(deductions["half_self_employment_tax_deduction_cents"])],
    ]
    quarterly_rows = [[item["due_date"], _money(item["amount_cents"])]
                      for item in projection["quarterly_estimates"]]
    sections = [
        ("Income Basis", (["Line", "Amount"], [
            ["YTD net income", _money(income["ytd_net_income_cents"])],
            ["Annualized net income", _money(income["annualized_net_income_cents"])],
            ["Estimated other income", _money(income["estimated_other_income_cents"])],
        ], [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Deductions", (["Line", "Amount"], deduction_rows,
                          [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Estimated Taxes", (["Tax", "Amount"], tax_rows,
                              [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Quarterly Estimates", (["Due date", "Estimated payment"], quarterly_rows,
                                  [3.6 * accountant_export.inch, 2.1 * accountant_export.inch], [1])),
        ("Important", " ".join(projection["warnings"])),
    ]
    return _pdf_document(
        "Klearing Tax Projection Summary", f"As of {projection['as_of']}", sections)
