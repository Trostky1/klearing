"""
Accountant Export — a monthly close package built entirely from data
Klearing already tracks locally. Nothing here makes a network call;
everything is read from the SQLite database (db.py) and rendered to a
PDF or CSV on disk. Three deliverables:

  build_close_pdf(period)   a one-file monthly close package (reportlab)
  build_qb_csv(period)      approved transactions, QuickBooks/Xero shape
  build_pending_csv(period) currently-pending items, clearly labeled

This module is deliberately self-contained (no import of app.py) —
same reason ollama_client.py re-derives its own summary/vendor blocks
instead of importing app.py's: app.py is the composition root that
will import THIS module, so the dependency can only run one way.

reportlab is an optional runtime dependency (see requirements.txt). If
it isn't installed, PDF_AVAILABLE is False and build_close_pdf raises
ExportError with a calm install instruction — callers must check
PDF_AVAILABLE (or catch ExportError) rather than assume the import
succeeded.
"""

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

import db
import ledger
import recurring
from config import APP_NAME, APP_VERSION

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
    PDF_AVAILABLE = True
except Exception:
    PDF_AVAILABLE = False

REPORTLAB_INSTALL_HINT = (
    "PDF export needs the reportlab package, which isn't installed. "
    "Run: pip install reportlab"
)


class ExportError(RuntimeError):
    """Clear, user-facing export failure — routes already catch
    RuntimeError and show the message instead of a 500."""


# Mirrors app.py's VENDOR_JUMP_KEY / DEFAULT_VENDOR_JUMP_PCT — kept as a
# separate constant (not imported) to avoid a circular import with
# app.py; if the Settings default ever changes, update both.
VENDOR_JUMP_KEY = "vendor_jump_threshold"
DEFAULT_VENDOR_JUMP_PCT = 20.0

RECONCILE_FLAG_THRESHOLD = 25.0  # mirrors app.py's RECONCILE_THRESHOLD

EVIDENCE_TYPES = [
    ("receipt", "Receipt"),
    ("invoice", "Invoice"),
    ("bank_statement", "Bank statement"),
    ("contract", "Contract"),
    ("other", "Other"),
]
EVIDENCE_LABELS = dict(EVIDENCE_TYPES)


# --------------------------------------------------------------- periods


def previous_complete_month():
    """The most recent fully-elapsed month, as YYYY-MM — the export
    page's default so a mid-month click doesn't default to a partial
    period."""
    first_of_this_month = date.today().replace(day=1)
    last_of_prior = first_of_this_month - timedelta(days=1)
    return last_of_prior.strftime("%Y-%m")


def month_bounds(period):
    """(start_date, end_date) ISO strings for a YYYY-MM period. Raises
    ExportError on a malformed period rather than silently defaulting —
    a close package for the wrong month is worse than an error."""
    try:
        year, month = (int(part) for part in period.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError):
        raise ExportError(f'"{period}" isn\'t a valid month (expected YYYY-MM).')
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ----------------------------------------------------------- aggregation


def signed_amount(item):
    """Expenses negative, everything else (income, transfer) positive —
    same convention as app.py's signed_amount, reimplemented here to
    avoid importing app.py."""
    return -item["amount"] if item["direction"] == "expense" else item["amount"]


def _statement_rollup(accounts):
    """Non-zero native statement accounts, largest amount first."""
    rows = [(account["name"], {"total": account["amount"]})
            for account in accounts if account["amount"]]
    return sorted(rows, key=lambda row: -row[1]["total"])


def _vendor_jump_threshold():
    raw = db.get_setting(VENDOR_JUMP_KEY)
    if raw:
        try:
            value = Decimal(raw)
            if value > 0:
                return value
        except (ArithmeticError, ValueError):
            pass
    return Decimal(str(DEFAULT_VENDOR_JUMP_PCT))


def _top_vendors(period_items, prior_items, limit=10):
    """Top vendors by expense spend in the period, with a jump flag
    against the PRIOR period's spend for the same vendor (same
    threshold convention as the Vendors tab)."""
    this_totals, prior_totals = {}, {}
    for item in period_items:
        if item["direction"] != "expense":
            continue
        this_totals[item["vendor"]] = (this_totals.get(item["vendor"], Decimal(0))
                                       + item["amount"])
    for item in prior_items:
        if item["direction"] != "expense":
            continue
        prior_totals[item["vendor"]] = (prior_totals.get(item["vendor"], Decimal(0))
                                        + item["amount"])
    threshold = _vendor_jump_threshold()
    rows = []
    for vendor, total in this_totals.items():
        prior = prior_totals.get(vendor, Decimal(0))
        jump_pct = None
        if prior > 0 and total >= prior * (1 + threshold / 100):
            jump_pct = (total / prior - 1) * 100
        rows.append({"vendor": vendor, "total": total, "jump_pct": jump_pct})
    rows.sort(key=lambda r: -r["total"])
    return rows[:limit]


def _nearest_snapshot_on_or_before(target_date):
    """Closest native-era reconciliation snapshot on or before a date.

    Rows from before the native cutover record a different accounting
    source and must never be compared with today's native ledger.
    """
    native_since = db.get_setting("native_reconciliation_since")
    if not native_since:
        return None
    history = db.list_reconcile_history(days=366)
    candidates = [row for row in history
                  if native_since <= row["date"] <= target_date]
    return candidates[-1] if candidates else None


def cash_balance(as_of_date):
    """Cash parent plus its direct children from the native Balance Sheet."""
    report = ledger.balance_sheet(as_of_date)
    cash_parent = ledger.account_by_code("1000")
    cash_parent_id = cash_parent["id"] if cash_parent else None
    return sum((account["amount"] for account in report["assets"]
                if account["code"] == "1000"
                or account["parent_account_id"] == cash_parent_id), Decimal("0"))


def _reconciliation_status(start_date, end_date):
    """Native cash balances plus the closest recorded bank observation.

    Old rows retain a ``wave_balance`` column for historical compatibility,
    but it is deliberately ignored. Any residual is recomputed between the
    native ledger and the bank balance on the same snapshot date.
    """
    opening_date = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
    closing_snap = _nearest_snapshot_on_or_before(end_date)
    ledger_at_check = (cash_balance(closing_snap["date"])
                       if closing_snap else None)
    bank_balance = (Decimal(str(closing_snap["bank_balance"]))
                    if closing_snap else None)
    residual = (ledger_at_check - bank_balance if closing_snap else None)
    return {
        "opening_balance": cash_balance(opening_date),
        "opening_date": opening_date,
        "closing_balance": cash_balance(end_date),
        "closing_date": closing_snap["date"] if closing_snap else None,
        "ledger_balance": ledger_at_check,
        "bank_balance": bank_balance,
        "residual": residual,
        "flagged": bool(residual is not None
                        and abs(residual) > RECONCILE_FLAG_THRESHOLD),
        "checked": closing_snap is not None,
    }


def _invoice_aging():
    """Overdue invoices (still pending_review or sent, not paid) with
    days outstanding from Klearing's native invoice reminder records."""
    today = date.today()
    rows = []
    for inv in db.list_invoice_reminders():
        if inv["status"] == "paid":
            continue
        try:
            due = date.fromisoformat(inv["due_date"])
        except (ValueError, TypeError):
            continue
        days_over = (today - due).days
        if days_over <= 0:
            continue
        rows.append({
            "customer_name": inv["customer_name"],
            "invoice_number": inv.get("invoice_number") or "—",
            "amount_due": inv["amount_due"],
            "days_outstanding": days_over,
        })
    rows.sort(key=lambda r: -r["days_outstanding"])
    return rows


def notes_coverage(period):
    """(with_notes, total) across every approved + pending transaction
    relevant to the period — approved items IN the period, plus every
    item still pending right now (there's no "period" for a pending
    item; it hasn't happened yet as far as the books are concerned).
    Counts a note OR an evidence tag as "covered" — either is
    documentation, not just free text."""
    start_date, end_date = month_bounds(period)
    approved = db.list_items_in_range("approved", start_date, end_date)
    pending = db.list_items(status="pending")
    items = approved + pending
    covered = sum(1 for item in items
                 if (item.get("notes") or "").strip()
                 or item.get("evidence_type"))
    return covered, len(items)


def gather_month_data(period):
    """Everything the PDF/CSV builders need for one period, gathered
    once so both can share it. Statement figures come from the native
    ledger reports; queue rows remain transaction-detail and pending-review
    evidence rather than a second source of financial totals.
    """
    start_date, end_date = month_bounds(period)
    prior_end = date.fromisoformat(start_date) - timedelta(days=1)
    prior_start = prior_end.replace(day=1)

    approved = db.list_items_in_range("approved", start_date, end_date)
    prior_approved = db.list_items_in_range(
        "approved", prior_start.isoformat(), prior_end.isoformat())
    pending = db.list_items(status="pending")

    pnl = ledger.profit_and_loss(start_date, end_date)
    balance_sheet = ledger.balance_sheet(end_date)
    total_in = pnl["revenue_total"]
    total_out = pnl["cogs_total"] + pnl["operating_expenses_total"]

    reconciliation = _reconciliation_status(start_date, end_date)
    top_vendors = _top_vendors(approved, prior_approved)
    subscriptions = recurring.detect_subscriptions()
    invoice_aging = _invoice_aging()
    no_documentation = [
        item for item in approved
        if not (item.get("notes") or "").strip() and not item.get("evidence_type")
    ]

    return {
        "period": period,
        "business_name": db.get_setting("business_name") or "your business",
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "approved": approved,
        "pending": pending,
        "profit_and_loss": pnl,
        "balance_sheet": balance_sheet,
        "total_in": total_in,
        "total_out": total_out,
        "net": pnl["net_income"],
        "income_by_category": _statement_rollup(pnl["revenue"]),
        "expense_by_category": _statement_rollup(
            pnl["cogs"] + pnl["operating_expenses"]),
        "reconciliation": reconciliation,
        "top_vendors": top_vendors,
        "subscriptions": subscriptions,
        "subscriptions_monthly_total": sum(
            s.get("monthly_cost", s["typical_amount"])
            for s in subscriptions),
        "invoice_aging": invoice_aging,
        "no_documentation": no_documentation,
    }


# ---------------------------------------------------------------- CSVs


QB_HEADER = ["Date", "Description", "Amount", "Category", "Account",
            "Reference", "Memo", "Cleared", "Evidence"]


def _qb_row(item, cleared_label, account_name):
    date_obj = date.fromisoformat(item["date"])
    return [
        date_obj.strftime("%m/%d/%Y"),
        item["vendor"],
        f"{signed_amount(item):.2f}",
        item["suggested_category"],
        account_name,
        item.get("external_transaction_id") or f'KL-{item["id"]}',
        item.get("notes") or "",
        cleared_label,
        EVIDENCE_LABELS.get(item.get("evidence_type"), ""),
    ]


def build_qb_csv(period):
    """Approved transactions for the period, QuickBooks/Xero-shaped:
    MM/DD/YYYY dates, signed amounts (expense negative), Note and
    Evidence carried through as their own columns (an addition beyond
    the 8 standard columns — extra trailing columns don't block a
    QuickBooks/Xero CSV import, which maps columns explicitly)."""
    data = gather_month_data(period)
    checking = ledger.account_by_code("1010")
    account_name = checking["name"] if checking else "Business checking"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(QB_HEADER)
    for item in data["approved"]:
        writer.writerow(_qb_row(item, "Cleared", account_name))
    return buffer.getvalue()


def build_all_transactions_csv(period):
    """Approved transactions for the period PLUS every currently-pending
    item, in one file — the "all transactions" export. Pending rows are
    clearly labeled via the Cleared column ("Pending", not "Cleared"),
    not buried in a banner row that would break a straightforward CSV
    parse. This file is a complete local record, not meant for blind
    QuickBooks import the way build_qb_csv's approved-only file is —
    importing unapproved transactions as real ones would be wrong."""
    data = gather_month_data(period)
    checking = ledger.account_by_code("1010")
    account_name = checking["name"] if checking else "Business checking"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(QB_HEADER)
    for item in data["approved"]:
        writer.writerow(_qb_row(item, "Cleared", account_name))
    for item in data["pending"]:
        writer.writerow(_qb_row(item, "Pending", account_name))
    return buffer.getvalue()


# ----------------------------------------------------------------- PDF


FOOTER_TEXT = f"Generated by {APP_NAME} — local data, not reviewed by a CPA"


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawCentredString(doc.pagesize[0] / 2, 0.5 * inch, FOOTER_TEXT)
    canvas.drawRightString(doc.pagesize[0] - 0.75 * inch, 0.5 * inch,
                           f"Page {doc.page}")
    canvas.restoreState()


def _styles():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(
        "KLTitle", parent=base["Title"], fontSize=22, spaceAfter=6,
    ))
    base.add(ParagraphStyle(
        "KLCoverMeta", parent=base["Normal"], fontSize=11,
        alignment=TA_CENTER, textColor=colors.HexColor("#333333"),
        spaceAfter=4,
    ))
    base.add(ParagraphStyle(
        "KLSection", parent=base["Heading2"], fontSize=13,
        spaceBefore=18, spaceAfter=8, textColor=colors.black,
    ))
    base.add(ParagraphStyle(
        "KLBody", parent=base["Normal"], fontSize=10, leading=14,
    ))
    base.add(ParagraphStyle(
        "KLNote", parent=base["Normal"], fontSize=9, leading=12,
        textColor=colors.HexColor("#555555"),
    ))
    return base


TABLE_STYLE = TableStyle([
    ("FONTSIZE", (0, 0), (-1, -1), 10),
    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
    ("TOPPADDING", (0, 0), (-1, -1), 5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
])


def _money(value):
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _section_table(headers, rows, col_widths=None, money_cols=None):
    """money_cols: column indices holding money/numeric values — those
    columns (header + body) are right-aligned so figures line up on the
    decimal, the accountant-report convention. Left-aligned otherwise."""
    table = Table([headers] + rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TABLE_STYLE)
    for col in (money_cols or []):
        table.setStyle(TableStyle([
            ("ALIGN", (col, 0), (col, -1), "RIGHT"),
        ]))
    return table


def build_close_pdf(period):
    """The one-file monthly close package. Raises ExportError if
    reportlab isn't installed — callers must check PDF_AVAILABLE (or
    catch this) before calling, never let the ImportError surface as a
    500."""
    if not PDF_AVAILABLE:
        raise ExportError(REPORTLAB_INSTALL_HINT)

    data = gather_month_data(period)
    styles = _styles()
    story = []

    # --- Cover page ---------------------------------------------------
    story.append(Spacer(1, 2.2 * inch))
    story.append(Paragraph(f"{APP_NAME} Monthly Close Package",
                           styles["KLTitle"]))
    story.append(Paragraph(escape_amp(data["business_name"]),
                           styles["KLCoverMeta"]))
    story.append(Paragraph(f'Period: {data["start_date"]} to {data["end_date"]}',
                           styles["KLCoverMeta"]))
    story.append(Paragraph(f'Generated: {data["generated_at"]}',
                           styles["KLCoverMeta"]))
    story.append(Paragraph(f"{APP_NAME} version {APP_VERSION}",
                           styles["KLCoverMeta"]))
    story.append(PageBreak())

    # --- Cash summary ---------------------------------------------------
    recon = data["reconciliation"]
    opening = recon["opening_balance"]
    closing = recon["closing_balance"]
    story.append(Paragraph("Native Ledger Summary", styles["KLSection"]))
    cash_rows = [
        ["Opening balance",
         _money(opening) if opening is not None else "Not available"],
        ["Revenue", _money(data["total_in"])],
        ["Expenses", f"-{_money(data['total_out'])}"],
        ["Net income", _money(data["net"])],
        ["Closing balance",
         _money(closing) if closing is not None else "Not available"],
    ]
    story.append(_section_table(["Line", "Amount"], cash_rows,
                                col_widths=[3 * inch, 2 * inch],
                                money_cols=[1]))
    # --- Reconciliation status ------------------------------------------
    story.append(Paragraph("Reconciliation Status", styles["KLSection"]))
    if not recon["checked"]:
        story.append(Paragraph(
            "No reconciliation check was recorded on or before the end "
            "of this period.", styles["KLBody"]))
    else:
        recon_rows = [
            ["Native ledger cash", _money(recon["ledger_balance"])],
            ["Recorded bank balance", _money(recon["bank_balance"])],
            ["Unexplained residual", _money(recon["residual"])],
            ["Status",
             "FLAGGED — residual exceeds threshold" if recon["flagged"]
             else "Clean"],
        ]
        story.append(_section_table(["Line", "Value"], recon_rows,
                                    col_widths=[3 * inch, 2.5 * inch],
                                    money_cols=[1]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f'As of the closest check on or before period end '
            f'({escape_amp(recon["closing_date"])}).', styles["KLNote"]))

    # --- Income / expense breakdowns -------------------------------------
    story.append(Paragraph("Revenue by Account", styles["KLSection"]))
    if data["income_by_category"]:
        rows = [[cat, _money(entry["total"])]
                for cat, entry in data["income_by_category"]]
        story.append(_section_table(
            ["Account", "Total"], rows,
            col_widths=[4 * inch, 2 * inch], money_cols=[1]))
    else:
        story.append(Paragraph("No income transactions this period.",
                               styles["KLBody"]))

    story.append(Paragraph("Expenses by Account", styles["KLSection"]))
    if data["expense_by_category"]:
        rows = [[cat, _money(entry["total"])]
                for cat, entry in data["expense_by_category"]]
        story.append(_section_table(
            ["Account", "Total"], rows,
            col_widths=[4 * inch, 2 * inch], money_cols=[1]))
    else:
        story.append(Paragraph("No expense transactions this period.",
                               styles["KLBody"]))

    # --- Top vendors ------------------------------------------------------
    story.append(Paragraph("Top Vendors", styles["KLSection"]))
    if data["top_vendors"]:
        rows = []
        for v in data["top_vendors"]:
            flag = (f"+{v['jump_pct']:.0f}% vs prior period"
                   if v["jump_pct"] is not None else "")
            rows.append([v["vendor"], _money(v["total"]), flag])
        story.append(_section_table(
            ["Vendor", "Total Spent", "Flag"], rows,
            col_widths=[2.5 * inch, 1.5 * inch, 2 * inch],
            money_cols=[1]))
    else:
        story.append(Paragraph("No vendor expense activity this period.",
                               styles["KLBody"]))

    # --- Pending items -----------------------------------------------------
    story.append(Paragraph("Pending Items (at close)", styles["KLSection"]))
    if data["pending"]:
        rows = [[i["date"], i["vendor"],
                _money(i["amount"]), i.get("suggested_category") or "No suggestion"]
               for i in data["pending"]]
        story.append(_section_table(
            ["Date", "Vendor", "Amount", "Suggested category"], rows,
            col_widths=[1.1 * inch, 1.8 * inch, 1.1 * inch, 2 * inch],
            money_cols=[2]))
    else:
        story.append(Paragraph("Nothing was waiting for review at close.",
                               styles["KLBody"]))

    # --- Subscriptions -----------------------------------------------------
    story.append(Paragraph("Subscriptions", styles["KLSection"]))
    if data["subscriptions"]:
        rows = [[s["vendor"], s["cadence"],
                _money(s["typical_amount"])] for s in data["subscriptions"]]
        story.append(_section_table(
            ["Vendor", "Cadence", "Typical Amount"], rows,
            col_widths=[2.5 * inch, 1.5 * inch, 2 * inch],
            money_cols=[2]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f'{len(data["subscriptions"])} detected, about '
            f'{_money(data["subscriptions_monthly_total"])}/month total.',
            styles["KLNote"]))
    else:
        story.append(Paragraph(
            "No recurring charges detected yet — takes a few months of "
            "approved history to spot a pattern.", styles["KLBody"]))

    # --- Invoice aging -----------------------------------------------------
    story.append(Paragraph("Invoice Aging", styles["KLSection"]))
    if data["invoice_aging"]:
        rows = [[i["customer_name"], i["invoice_number"],
                i["amount_due"], str(i["days_outstanding"])]
               for i in data["invoice_aging"]]
        story.append(_section_table(
            ["Customer", "Invoice #", "Amount Due", "Days Outstanding"],
            rows, col_widths=[1.8 * inch, 1 * inch, 1.3 * inch, 1.5 * inch],
            money_cols=[2, 3]))
    else:
        story.append(Paragraph("No overdue invoices.", styles["KLBody"]))

    # --- Exceptions log -----------------------------------------------------
    story.append(Paragraph("Exceptions Log", styles["KLSection"]))
    exceptions = []
    for v in data["top_vendors"]:
        if v["jump_pct"] is not None:
            exceptions.append(
                f'Vendor spend jump: {v["vendor"]} up '
                f'{v["jump_pct"]:.0f}% vs the prior period.')
    if recon["checked"] and recon["flagged"]:
        exceptions.append(
            f'Reconciliation flagged: {_money(recon["residual"])} '
            f'unexplained residual as of {recon["closing_date"]}.')
    if data["no_documentation"]:
        exceptions.append(
            f'{len(data["no_documentation"])} approved transaction(s) this '
            f'period have no note or evidence on file.')
    if exceptions:
        for line in exceptions:
            story.append(Paragraph(f"&bull; {escape_amp(line)}",
                                   styles["KLBody"]))
    else:
        story.append(Paragraph(
            "Nothing flagged, no unexplained reconciliation residual, and "
            "every approved transaction has a note or evidence tag.",
            styles["KLBody"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Note: Klearing does not currently track whether a category was "
        "manually changed after an automatic suggestion — this log covers "
        "spend jumps, reconciliation flags, and missing documentation "
        "only.", styles["KLNote"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title=f"{APP_NAME} Monthly Close — {period}",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def escape_amp(text):
    """reportlab Paragraphs interpret a minimal HTML-like markup, so
    literal & (common in vendor names like "Smith & Sons") must be
    escaped or the tag parser breaks. Table cells passed as plain
    strings (not Paragraphs) don't need this, but paragraph text does."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_invoice_pdf(invoice, lines, contact, totals, business_name=None,
                      logo_note=None):
    """The invoice a customer receives, as a PDF.

    Pure formatting: every figure is passed in already computed (totals in
    cents from invoice_ledger.invoice_totals, lines as db rows), so this
    file stays free of the invoicing arithmetic and of an import back into
    app.py. Reuses the same _styles / TABLE_STYLE / _section_table / _money
    helpers as the monthly-close PDF, so the two documents read as one set.

    Raises ExportError when reportlab is absent, matching build_close_pdf --
    callers check PDF_AVAILABLE or catch ExportError, never let ImportError
    become a 500.
    """
    if not PDF_AVAILABLE:
        raise ExportError(REPORTLAB_INSTALL_HINT)

    styles = _styles()
    story = []

    number = invoice.get("invoice_number") or "DRAFT"
    story.append(Paragraph(f"Invoice {escape_amp(number)}", styles["KLTitle"]))
    if business_name:
        story.append(Paragraph(escape_amp(business_name), styles["KLBody"]))
    if logo_note:
        story.append(Paragraph(escape_amp(logo_note), styles["KLNote"]))
    story.append(Spacer(1, 12))

    # Bill-to and dates, side by side as a borderless two-column table so
    # they align without hand-tuned spacing.
    bill_to = [f"<b>Bill to</b>", escape_amp(contact.get("display_name") or "")]
    if contact.get("email"):
        bill_to.append(escape_amp(contact["email"]))
    meta = [
        f"<b>Issued</b> {escape_amp(invoice['issue_date'])}",
        f"<b>Due</b> {escape_amp(invoice['due_date'])}",
    ]
    if invoice.get("terms"):
        meta.append(f"<b>Terms</b> {escape_amp(invoice['terms'])}")
    header = Table(
        [[Paragraph("<br/>".join(bill_to), styles["KLBody"]),
          Paragraph("<br/>".join(meta), styles["KLBody"])]],
        colWidths=[3.4 * inch, 3.4 * inch])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 18))

    rows = []
    for line in lines:
        quantity = _decimal(line["quantity_milli"], 1000)
        unit = _decimal(line["unit_price_cents"], 100)
        line_net = (line["quantity_milli"] * line["unit_price_cents"]) // 1000
        line_tax = round(line_net * line["tax_rate_bp"] / 10000)
        tax_label = (f"{_decimal(line['tax_rate_bp'], 100)}%"
                     if line["tax_rate_bp"] else "—")
        rows.append([
            # Plain string cells in a reportlab Table are drawn literally,
            # not parsed as markup, so the description must NOT be escaped
            # here -- escape_amp is only for Paragraph text. Escaping it
            # would render a literal "&amp;" to the customer.
            line["description"],
            f"{quantity:g}",
            _money(unit),
            tax_label,
            _money(_decimal(line_net + line_tax, 100)),
        ])
    story.append(_section_table(
        ["Description", "Qty", "Unit price", "Tax", "Amount"], rows,
        col_widths=[3.2 * inch, 0.7 * inch, 1.1 * inch, 0.7 * inch, 1.1 * inch],
        money_cols=[2, 4]))
    story.append(Spacer(1, 12))

    summary_rows = [
        ["Subtotal", _money(_decimal(totals["net"], 100))],
        ["Tax", _money(_decimal(totals["tax"], 100))],
        ["Total", _money(_decimal(totals["gross"], 100))],
    ]
    summary = _section_table(["", ""], summary_rows,
                             col_widths=[5.7 * inch, 1.1 * inch],
                             money_cols=[1])
    story.append(summary)

    if invoice.get("notes"):
        story.append(Spacer(1, 18))
        story.append(Paragraph("Notes", styles["KLSection"]))
        story.append(Paragraph(escape_amp(invoice["notes"]), styles["KLBody"]))

    if invoice.get("payment_link_url"):
        story.append(Spacer(1, 18))
        story.append(Paragraph(
            f'Pay online: {escape_amp(invoice["payment_link_url"])}',
            styles["KLBody"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title=f"Invoice {number}",
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _decimal(cents_or_units, divisor):
    """A plain float for display formatting only. Never used for posting --
    the ledger math is integer cents in invoice_ledger; this is the PDF."""
    return cents_or_units / divisor
