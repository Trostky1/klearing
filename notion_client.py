"""Opt-in Notion export adapter for close packages and running notes."""

from datetime import date

import requests

import connectivity
import db
import secrets_store

API_BASE = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"
SECRET_NAME = "klearing_notion_token"
PARENT_PAGE_ID_KEY = "klearing_notion_page_id"
SNAPSHOT_PAGE_ID_KEY = "notion_snapshot_page_id"
SNAPSHOT_TITLE = "Klearing \u2014 Business Snapshot"


class NotionHTTPError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        super().__init__(f"Notion returned HTTP {status}: {body[:300]}")


def get_token():
    return secrets_store.get_secret(SECRET_NAME)


def get_parent_page_id():
    return db.get_setting(PARENT_PAGE_ID_KEY) or ""


def is_configured():
    try:
        return bool(get_token()) and bool(get_parent_page_id())
    except Exception:
        return False


def validate_config(token, parent_page_id):
    if not (token or "").strip():
        return "Notion integration token is empty. Paste the token."
    if len(token.strip()) < 20:
        return "The Notion token looks too short. Paste the complete token."
    if not (parent_page_id or "").strip():
        return "Notion parent page ID is empty. Paste the page ID."
    return None


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": API_VERSION,
        "Content-Type": "application/json",
    }


def _request(token, method, path, **kwargs):
    response = requests.request(
        method, f"{API_BASE}{path}", headers=_headers(token), timeout=25,
        **kwargs)
    connectivity.raise_for_rate_limit("Notion", response.status_code)
    if response.status_code < 200 or response.status_code >= 300:
        raise NotionHTTPError(response.status_code, response.text)
    return response.json() if response.content else {}


def _title_from_page(page):
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") != "title":
            continue
        pieces = prop.get("title") or []
        return "".join(piece.get("plain_text") or
                        (piece.get("text") or {}).get("content", "")
                        for piece in pieces)
    return "Notion page"


def test_connection(token, parent_page_id):
    format_error = validate_config(token, parent_page_id)
    if format_error:
        return connectivity.fail(format_error)
    try:
        page = _request(token.strip(), "GET", f"/pages/{parent_page_id.strip()}")
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except NotionHTTPError as exc:
        if exc.status in (401, 403):
            return connectivity.fail(
                "Notion rejected the token or page access. Share the parent "
                "page with the integration, then try again.")
        return connectivity.fail(
            "Notion could not verify that page. Check the page ID and try again.")
    except Exception as exc:
        return connectivity.from_exception("Notion", exc)
    return connectivity.ok(f'Notion connected: "{_title_from_page(page)}".')


def _rt(value):
    return [{"type": "text", "text": {"content": str(value)[:2000]}}]


def _paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rt(text)}}


def _heading(text, level=2):
    block_type = f"heading_{min(max(level, 1), 3)}"
    return {"object": "block", "type": block_type,
            block_type: {"rich_text": _rt(text)}}


def _table(headers, rows):
    rows = rows or [["No entries"] + [""] * (len(headers) - 1)]
    table_rows = []
    for row in rows[:100]:
        cells = list(row)[:len(headers)]
        cells += [""] * (len(headers) - len(cells))
        table_rows.append({
            "object": "block", "type": "table_row",
            "table_row": {"cells": [_rt(cell) for cell in cells]},
        })
    return {"object": "block", "type": "table", "table": {
        "table_width": len(headers),
        "has_column_header": True,
        "has_row_header": False,
        "children": table_rows,
    }}


def _money(value):
    if value is None:
        return "Not available"
    return f"${abs(value):,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


def _section(blocks, title, headers, rows, empty=None):
    blocks.append(_heading(title))
    if rows:
        blocks.append(_table(headers, rows))
    else:
        blocks.append(_paragraph(empty or "No entries."))


def build_close_blocks(data):
    recon = data["reconciliation"]
    opening = recon["opening_balance"]
    closing = recon["closing_balance"]
    blocks = []
    _section(blocks, "Native Ledger Summary", ["Line", "Amount"], [
        ["Opening balance", _money(opening)],
        ["Revenue", _money(data["total_in"])],
        ["Expenses", f"-{_money(data['total_out'])}"],
        ["Net income", _money(data["net"])],
        ["Closing balance", _money(closing)],
    ])
    _section(blocks, "Reconciliation Status", ["Line", "Value"],
             [["Native ledger cash", _money(recon["ledger_balance"])],
              ["Recorded bank balance", _money(recon["bank_balance"])],
              ["Unexplained residual", _money(recon["residual"])],
              ["Status", "FLAGGED" if recon["flagged"] else
               ("Clean" if recon["checked"] else "Not checked")]])
    _section(blocks, "Revenue by Account", ["Account", "Total"], [
        [category, _money(entry["total"])]
        for category, entry in data["income_by_category"]],
        "No income transactions this period.")
    _section(blocks, "Expenses by Account", ["Account", "Total"], [
        [category, _money(entry["total"])]
        for category, entry in data["expense_by_category"]],
        "No expense transactions this period.")
    _section(blocks, "Top Vendors", ["Vendor", "Total Spent", "Flag"], [
        [row["vendor"], _money(row["total"]),
         f"+{row['jump_pct']:.0f}% vs prior period"
         if row["jump_pct"] is not None else ""]
        for row in data["top_vendors"]],
        "No vendor expense activity this period.")
    _section(blocks, "Pending Items (at close)",
             ["Date", "Vendor", "Amount", "Suggested category"], [
                 [item["date"], item["vendor"], _money(item["amount"]),
                  item.get("suggested_category") or "No suggestion"]
                 for item in data["pending"]],
             "Nothing was waiting for review at close.")
    _section(blocks, "Subscriptions", ["Vendor", "Cadence", "Typical Amount"], [
        [row["vendor"], row["cadence"], _money(row["typical_amount"])]
        for row in data["subscriptions"]],
        "No recurring charges detected yet.")
    _section(blocks, "Invoice Aging",
             ["Customer", "Invoice #", "Amount Due", "Days Outstanding"], [
                 [row["customer_name"], row["invoice_number"],
                  row["amount_due"], row["days_outstanding"]]
                 for row in data["invoice_aging"]],
             "No overdue invoices.")

    exceptions = []
    for row in data["top_vendors"]:
        if row["jump_pct"] is not None:
            exceptions.append(
                f"Vendor spend jump: {row['vendor']} up "
                f"{row['jump_pct']:.0f}% vs the prior period.")
    if recon["checked"] and recon["flagged"]:
        exceptions.append(
            f"Reconciliation flagged: {_money(recon['residual'])} "
            f"unexplained residual as of {recon['closing_date']}.")
    if data["no_documentation"]:
        exceptions.append(
            f"{len(data['no_documentation'])} approved transaction(s) have "
            "no note or evidence on file.")
    blocks.append(_heading("Exceptions Log"))
    blocks.extend(_paragraph(item) for item in exceptions) if exceptions else \
        blocks.append(_paragraph("Nothing flagged and no missing documentation."))
    return blocks


def _page_payload(parent_page_id, title, children):
    return {
        "parent": {"page_id": parent_page_id},
        "properties": {"title": {"title": [{
            "type": "text", "text": {"content": title[:2000]}
        }]}},
        "children": children[:100],
    }


def create_close_page(period):
    import accountant_export
    token = get_token()
    parent = get_parent_page_id()
    if not is_configured():
        raise RuntimeError("Notion isn't connected or no parent page is set.")
    data = accountant_export.gather_month_data(period)
    title = date.fromisoformat(data["start_date"]).strftime("%B %Y")
    page = _request(token, "POST", "/pages",
                    json=_page_payload(parent, f"Klearing Close \u2014 {title}",
                                       build_close_blocks(data)))
    return {"title": f"Klearing Close \u2014 {title}",
            "url": page.get("url") or
            f"https://www.notion.so/{page.get('id', '').replace('-', '')}"}


def _snapshot_blocks():
    import ollama_client
    lines = ollama_client._snapshot_lines()
    insight = db.get_agent_memory(ollama_client.INSIGHT_KEY)
    blocks = [_heading(SNAPSHOT_TITLE),
              _paragraph("Updated daily from Klearing's local financial snapshot.")]
    blocks.extend(_paragraph(line.lstrip("- ")) for line in lines)
    if insight and insight.get("value"):
        blocks.extend([_heading("Daily AI Insight", level=3),
                       _paragraph(insight["value"])])
    return blocks


def _append(token, page_id, children):
    for start in range(0, len(children), 100):
        _request(token, "PATCH", f"/blocks/{page_id}/children",
                 json={"children": children[start:start + 100]})


def update_running_snapshot():
    if not is_configured():
        return False
    token = get_token()
    page_id = db.get_setting(SNAPSHOT_PAGE_ID_KEY)
    children = _snapshot_blocks()
    if not page_id:
        page = _request(token, "POST", "/pages",
                        json=_page_payload(get_parent_page_id(),
                                           SNAPSHOT_TITLE, children))
        db.set_setting(SNAPSHOT_PAGE_ID_KEY, page.get("id", ""))
        return True

    existing = _request(token, "GET", f"/blocks/{page_id}/children")
    for block in existing.get("results", []):
        _request(token, "PATCH", f"/blocks/{block['id']}",
                 json={"archived": True})
    _append(token, page_id, children)
    return True
