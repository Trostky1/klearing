"""
Local AI assistant client — Ollama over HTTP, localhost only.

Klearing talks to Ollama's HTTP server at http://localhost:11434 (the
CLI executable is NOT on PATH and is never invoked from code). Nothing
here ever leaves the machine: the model runs locally, and the context
sent to it is built exclusively from already-computed aggregates —
never raw transaction rows.

The app must work identically when Ollama is offline: is_available()
never raises, and the UI degrades to an "AI offline" state. This is a
convenience layer, not a dependency.

Model: deepseek-r1:8b by default; when it isn't installed, the best
available model is auto-detected from /api/tags (+ /api/show
capabilities). A manually set "klearing_ai_model" settings value
always wins and is never overwritten (no UI for it — a deliberate
escape hatch, not a feature).

Reasoning models (deepseek-r1 and friends) externalize their chain of
thought as <think>...</think> — that is stripped before anything
reaches the UI; users only see the final answer.

Instead of a pre-built context dump, the model gets six read-only
tools (Ollama native tool calling via /api/chat) and decides what to
query per question. Every tool returns DB aggregates only — never raw
transaction rows.
"""

import json
import time
from datetime import date, timedelta
from decimal import Decimal

import requests

import db

BASE_URL = "http://localhost:11434"
MODEL_SETTING_KEY = "klearing_ai_model"
DEFAULT_MODEL = "deepseek-r1:8b"

# Auto-detection preference, best first — prefix-matched against what
# /api/tags reports. Capability data (tools/thinking) breaks ties for
# models not on this list.
PREFERRED_MODELS = [
    "deepseek-r1:8b", "deepseek-r1", "qwen3.5", "qwen3", "gemma4",
    "llama3.1", "mistral-nemo", "llama3.2", "mistral", "gemma",
]

# Multi-step planning: the model may run up to this many tool rounds
# before its final answer — enough for plan → gather → cross-check.
MAX_TOOL_ROUNDS = 5

# Hardcoded and never user-editable — the rules are the product here.
SYSTEM_PROMPT = """You are Klearing's financial assistant. Klearing is a local personal finance
and bookkeeping app. Your job is to help the user understand the financial data
currently visible in the app.

You have read-only tools that query Klearing's own database. Questions about
the user's vendors, spending, cash, reconciliation, monthly summary,
subscriptions, or flags ARE in scope: for those, ALWAYS call the matching tool
first and answer from its result - never refuse them, never invent or estimate
a figure, and never tell the user to go look it up themselves. When you
discover a recurring pattern or an important durable finding, save it with the
write_memory tool so future sessions start knowing it.

Rules you must follow without exception:
- Only state numbers that a tool returned
- Never give general financial advice
- Never discuss anything outside of Klearing's data
- Never speculate about data the tools don't return (say "I don't have that data" instead)
- Never recommend external products, services, or advisors
- Keep responses concise - 3 sentences max unless the user asks to elaborate
- Only when a question is about something other than the user's Klearing data
  (markets, news, other apps, life advice), refuse with:
  "I can only help with data inside Klearing"

"""


def is_available():
    """True when the local Ollama server answers. Never raises — the
    app must behave identically without Ollama."""
    try:
        response = requests.get(f"{BASE_URL}/api/tags", timeout=1.5)
        return response.status_code == 200
    except Exception:
        return False


# ------------------------------------------------------- model choice

_DETECT_CACHE = {"expires": 0.0, "model": None}
_DETECT_TTL = 300  # seconds — model installs are rare, restarts common


def _installed_models():
    response = requests.get(f"{BASE_URL}/api/tags", timeout=3)
    if response.status_code != 200:
        return []
    return [m.get("name", "") for m in response.json().get("models", [])]


def _capabilities(model):
    try:
        response = requests.post(f"{BASE_URL}/api/show",
                                 json={"model": model}, timeout=5)
        if response.status_code == 200:
            return set(response.json().get("capabilities", []))
    except Exception:
        pass
    return set()


def detect_model():
    """The best installed model: the preference list first, then any
    tools-capable model, then whatever is installed, then the default
    (which /api/chat will reject with a clear error if truly absent).
    Cached briefly; never raises and never writes to settings."""
    now = time.time()
    if _DETECT_CACHE["model"] and _DETECT_CACHE["expires"] > now:
        return _DETECT_CACHE["model"]
    chosen = DEFAULT_MODEL
    try:
        installed = _installed_models()
        if installed:
            chosen = None
            for preferred in PREFERRED_MODELS:
                matches = [m for m in installed
                           if m == preferred
                           or m.startswith(preferred + ":")
                           or m.split(":")[0] == preferred]
                if matches:
                    chosen = matches[0]
                    break
            if chosen is None:
                with_tools = [m for m in installed
                              if "tools" in _capabilities(m)]
                chosen = (with_tools or installed)[0]
    except Exception:
        chosen = DEFAULT_MODEL
    _DETECT_CACHE.update(expires=now + _DETECT_TTL, model=chosen)
    return chosen


def reset_model_cache():
    _DETECT_CACHE.update(expires=0.0, model=None)


def model_name():
    """The model to run — a manually set klearing_ai_model value always
    wins (user preference, never overwritten); otherwise auto-detect."""
    manual = (db.get_setting(MODEL_SETTING_KEY) or "").strip()
    if manual:
        return manual
    return detect_model()


# --------------------------------------------------- think stripping

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def strip_think_stream(chunks):
    """Filter <think>...</think> spans out of a streamed chunk
    sequence — tags can arrive split across chunk boundaries, so a
    small tail is held back until it's provably not a partial tag.
    Leading whitespace after a stripped block is dropped so answers
    don't start with the blank lines reasoning models leave behind."""
    in_think = False
    pending = ""
    emitted_anything = False
    for chunk in chunks:
        pending += chunk or ""
        out = []
        while True:
            if in_think:
                idx = pending.find(_THINK_CLOSE)
                if idx == -1:
                    # Keep only a tail that could still be a partial
                    # closing tag; everything else is reasoning.
                    pending = pending[-(len(_THINK_CLOSE) - 1):]
                    break
                pending = pending[idx + len(_THINK_CLOSE):]
                in_think = False
            else:
                idx = pending.find(_THINK_OPEN)
                if idx == -1:
                    hold = 0
                    for k in range(min(len(_THINK_OPEN) - 1,
                                       len(pending)), 0, -1):
                        if pending.endswith(_THINK_OPEN[:k]):
                            hold = k
                            break
                    cut = len(pending) - hold
                    if cut:
                        out.append(pending[:cut])
                        pending = pending[cut:]
                    break
                out.append(pending[:idx])
                pending = pending[idx + len(_THINK_OPEN):]
                in_think = True
        text = "".join(out)
        if not emitted_anything:
            text = text.lstrip()
        if text:
            emitted_anything = True
            yield text
    if pending and not in_think:
        text = pending if emitted_anything else pending.lstrip()
        if text:
            yield text


def strip_think(text):
    """One-shot version for non-streamed strings."""
    return "".join(strip_think_stream([text]))


def stream_response(prompt, system=None, model=None):
    """Yield response text chunk by chunk from /api/generate, think
    blocks stripped. Kept as the no-tools fallback path."""
    def raw():
        with requests.post(
            f"{BASE_URL}/api/generate",
            json={"model": model or model_name(), "prompt": prompt,
                  "system": system or SYSTEM_PROMPT, "stream": True},
            stream=True,
            timeout=120,
        ) as response:
            for line in response.iter_lines():
                if line:
                    chunk = json.loads(line)
                    yield chunk.get("response", "")
                    if chunk.get("done"):
                        break
    return strip_think_stream(raw())


def generate_category(prompt, categories=None):
    """Run the short categorization prompt with a hard ten-second cap.

    Returns ``(text, status)`` where status is one of ``ok``, ``timeout``,
    ``offline``, or ``error``. The caller owns category validation.
    """
    if not is_available():
        return "", "offline"
    system = (
        "You are a business expense categorizer. Given a transaction "
        "vendor, amount, and direction, output a single category from "
        "the provided list. Output ONLY the category name - no explanation, "
        "no punctuation, nothing else."
    )
    try:
        response = requests.post(
            f"{BASE_URL}/api/generate",
            json={"model": model_name(), "prompt": prompt,
                  "system": system, "stream": False},
            timeout=10,
        )
        response.raise_for_status()
        return strip_think(response.json().get("response", "")), "ok"
    except requests.Timeout:
        return "", "timeout"
    except Exception:
        return "", "error"


# ------------------------------------------------------------- context


def _money(value):
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _month_bounds(months_back=0):
    today = date.today()
    year, month = today.year, today.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _sums(items, start, end):
    """(money_in, money_out) over approved items in a window —
    transfers excluded, matching Summary's convention."""
    total_in = total_out = Decimal(0)
    for item in items:
        if not (start <= item["date"] <= end):
            continue
        if item["direction"] == "income":
            total_in += item["amount"]
        elif item["direction"] == "expense":
            total_out += item["amount"]
    return total_in, total_out


def _latest_reconcile():
    """The newest reconciliation snapshot dict, or None. Feature-
    detected: the history table ships on a separate branch."""
    if not hasattr(db, "list_reconcile_history"):
        return None
    try:
        native_since = db.get_setting("native_reconciliation_since")
        if not native_since:
            return None
        rows = db.list_reconcile_history(days=30)
        rows = [row for row in rows if row["date"] >= native_since]
        return rows[-1] if rows else None
    except Exception:
        return None


def _snapshot_lines():
    """The financial snapshot every section gets — DB aggregates only,
    no live bank calls (a chat message must never block on an
    external API)."""
    approved = db.list_items(status="approved")
    pending = db.list_items(status="pending")

    this_start, this_end = _month_bounds(0)
    last_start, last_end = _month_bounds(1)
    this_in, this_out = _sums(approved, this_start, this_end)
    last_in, last_out = _sums(approved, last_start, last_end)

    recon = _latest_reconcile()
    pending_out = sum(i["amount"] for i in pending
                      if i["direction"] == "expense")
    if recon is None:
        cash_line = "- Cash balance: not available (no reconcile check stored yet)"
        recon_line = "- Reconciliation: not checked recently"
        runway_line = None
        safe_line = "- Safe to spend: not available (no reconcile check stored yet)"
    else:
        cash_line = f'- Cash balance: {_money(recon["bank_balance"])} (as of {recon["date"]})'
        recon_line = ("- Reconciliation: flagged "
                      f'({_money(recon["residual"])} residual)'
                      if recon["flagged"] else "- Reconciliation: in sync")
        if pending_out > 0:
            safe_to_spend = max(recon["bank_balance"] - pending_out, 0)
            safe_line = (f"- Safe to spend: {_money(safe_to_spend)} "
                         "after pending expenses")
        else:
            safe_line = "- Safe to spend: same as cash balance (no pending expenses)"
        runway_line = None
        try:
            import cash_alert
            burn = cash_alert.average_daily_burn()
            if burn and burn > 0:
                runway_line = (f'- Runway: {recon["bank_balance"] / burn:.0f} '
                               f"days at the recent spending rate")
        except Exception:
            pass

    pending_total = sum(i["amount"] for i in pending)
    lines = [
        cash_line,
        *( [runway_line] if runway_line else [] ),
        recon_line,
        safe_line,
        f"- Pending items: {len(pending)} ({_money(pending_total)} total)",
        f"- This month net: {_money(this_in - this_out)} "
        f"(vs {_money(last_in - last_out)} last month)",
    ]
    return lines


def _vendors_block():
    approved = db.list_items(status="approved")
    this_start, this_end = _month_bounds(0)
    last_start, last_end = _month_bounds(1)
    this_month, last_month = {}, {}
    for item in approved:
        if item["direction"] != "expense":
            continue
        if this_start <= item["date"] <= this_end:
            this_month[item["vendor"]] = (
                this_month.get(item["vendor"], Decimal(0))
                                          + item["amount"])
        elif last_start <= item["date"] <= last_end:
            last_month[item["vendor"]] = (
                last_month.get(item["vendor"], Decimal(0))
                                          + item["amount"])
    top = sorted(this_month.items(), key=lambda kv: -kv[1])[:5]
    lines = ["Top vendors this month:"]
    if not top:
        lines.append("- (no vendor spend this month)")
    for vendor, total in top:
        prior = last_month.get(vendor, Decimal(0))
        jump = ""
        if prior > 0 and total > prior:
            jump = f" (+{(total / prior - 1) * 100:.0f}% vs last month)"
        lines.append(f"- {vendor}: {_money(total)}{jump}")
    try:
        import recurring
        subs = recurring.detect_subscriptions()
        monthly = sum(s.get("monthly_cost", s.get("typical_amount", 0))
                      for s in subs)
        lines.append(f"Subscriptions detected: {len(subs)} "
                     f"(~{_money(monthly)}/month total)")
    except Exception:
        pass
    return lines


def _reconcile_block():
    recon = _latest_reconcile()
    pending = db.list_items(status="pending")
    out_items = [i for i in pending if i["direction"] == "expense"]
    in_items = [i for i in pending if i["direction"] != "expense"]
    lines = []
    if recon is None:
        lines.append("No reconcile check stored yet — open the Reconcile "
                     "page to run one.")
    else:
        lines += [
            f'Native ledger cash: {_money(recon["wave_balance"])}',
            f'Bank balance: {_money(recon["bank_balance"])}',
            f'Difference: {_money(recon["wave_balance"] - recon["bank_balance"])}',
            f'Unexplained residual: {_money(recon["residual"])}',
        ]
        try:
            flagged_dates = [r["date"] for r in
                             db.list_reconcile_history(days=30)
                             if r["flagged"]]
            if flagged_dates:
                days = (date.today()
                        - date.fromisoformat(flagged_dates[-1])).days
                lines.append(f"Last flagged: {days} days ago")
            else:
                lines.append("Last flagged: no flags in the last 30 days")
        except Exception:
            pass
    lines.append(f"Pending out: "
                 f"{_money(sum(i['amount'] for i in out_items))} "
                 f"({len(out_items)} items)")
    lines.append(f"Pending in: "
                 f"{_money(sum(i['amount'] for i in in_items))} "
                 f"({len(in_items)} items)")
    return lines


def _summary_block(months_back=0):
    approved = db.list_items(status="approved")
    this_start, this_end = _month_bounds(months_back)
    # Match Summary's T2b comparison: the prior window is immediately
    # before the selected range and has the same inclusive length. This
    # matters when the selected month and the preceding calendar month
    # have different numbers of days.
    selected_start = date.fromisoformat(this_start)
    selected_end = date.fromisoformat(this_end)
    prior_end = selected_start - timedelta(days=1)
    prior_start = prior_end - (selected_end - selected_start)
    last_start, last_end = prior_start.isoformat(), prior_end.isoformat()
    this_in, this_out = _sums(approved, this_start, this_end)
    last_in, last_out = _sums(approved, last_start, last_end)
    categories = {}
    for item in approved:
        if (item["direction"] == "expense"
                and this_start <= item["date"] <= this_end):
            categories[item["suggested_category"]] = (
                categories.get(item["suggested_category"], Decimal(0))
                + item["amount"])
    when = "current month" if months_back == 0 else "previous month"
    lines = [
        f"Period: {this_start} to {this_end} ({when})",
        f"Money in: {_money(this_in)} (vs {_money(last_in)} prior)",
        f"Money out: {_money(this_out)} (vs {_money(last_out)} prior)",
        f"Net: {_money(this_in - this_out)} "
        f"(vs {_money(last_in - last_out)} prior)",
    ]
    if categories:
        top_cat, top_total = max(categories.items(), key=lambda kv: kv[1])
        lines.append(f"Top expense category: {top_cat} ({_money(top_total)})")
    return lines


def _growth_block():
    approved = db.list_items(status="approved")
    this_start, this_end = _month_bounds(0)
    this_in, this_out = _sums(approved, this_start, this_end)
    margin = ((this_in - this_out) / this_in * 100) if this_in > 0 else None
    lines = [
        f"This month revenue: {_money(this_in)}",
        f"This month expenses: {_money(this_out)}",
        f"Margin: {margin:.0f}%" if margin is not None
        else "Margin: not computable (no revenue this month)",
    ]
    # Optional integrations, feature-detected — they ship on a separate
    # branch and may not be present or connected.
    try:
        import stripe_client
        if stripe_client.is_configured():
            series = stripe_client.monthly_revenue(1)
            if series:
                lines.append(f"Stripe revenue: {_money(series[-1][1])} "
                             "this month")
    except Exception:
        pass
    try:
        import gusto_client
        if gusto_client.is_configured():
            series, _headcount = gusto_client.monthly_payroll(1)
            if series:
                lines.append(f"Labor cost: {_money(series[-1][1])} "
                             "this month")
    except Exception:
        pass
    return lines


SECTION_BLOCKS = {
    "Vendors": _vendors_block,
    "Reconcile": _reconcile_block,
    "Summary": _summary_block,
    "Growth": _growth_block,
}


def build_context(section):
    """The plain-text context block sent with every message — rebuilt
    fresh per send, aggregates only, never raw transaction rows."""
    db.init_db()
    section = (section or "").strip() or "Home"
    lines = [
        "=== Klearing Context ===",
        f"Section: {section}",
        f"Date: {date.today().isoformat()}",
        "",
        "Financial snapshot:",
    ]
    try:
        lines += _snapshot_lines()
    except Exception:
        lines.append("- (snapshot unavailable)")
    block = SECTION_BLOCKS.get(section)
    if block:
        lines.append("")
        try:
            lines += block()
        except Exception:
            lines.append("(section detail unavailable)")
    lines.append("=== End Context ===")
    return "\n".join(lines)


def build_full_context(section):
    """Everything the tools can answer, as one block — the grounding
    for models that can't call tools (they can't fetch more on demand,
    so they get it all up front). Still aggregates only."""
    lines = [build_context(section)
             .replace("=== End Context ===", "").rstrip()]
    current = SECTION_BLOCKS.get(section)
    for name, func in SECTION_BLOCKS.items():
        if func is current:
            continue
        lines.append("")
        lines.append(f"[{name}]")
        try:
            lines += func()
        except Exception:
            lines.append("(unavailable)")
    lines.append("=== End Context ===")
    return "\n".join(lines)


# ------------------------------------------------------------- tools
#
# Six read-only lookups the model calls on demand (Ollama native tool
# calling, /api/chat). Every one returns a plain-text aggregate view —
# sums, counts, top-N — never raw transaction rows. A tool that hits
# an error returns an honest "unavailable" string so the model can say
# so instead of inventing numbers.


def _tool_reconciliation():
    """Latest reconciliation snapshot + pending queue totals."""
    return "\n".join(_reconcile_block())


def _tool_vendor_summary():
    """Top vendors this month with jump flags."""
    return "\n".join(_vendors_block())


def _tool_cash_position():
    """Cash balance, runway, pending totals, this-month net."""
    return "\n".join(_snapshot_lines())


def _tool_summary(period="this_month"):
    """In/out/net vs prior + top expense category for a month."""
    months_back = 1 if str(period).strip() == "last_month" else 0
    return "\n".join(_summary_block(months_back))


def _tool_subscriptions():
    """Detected recurring vendors: cadence + typical amount."""
    import recurring
    subs = recurring.detect_subscriptions()
    if not subs:
        return ("No subscriptions detected yet — it takes a few months "
                "of approved history to spot a pattern.")
    lines = [f"{len(subs)} subscription(s), about "
             f"{_money(sum(s.get('monthly_cost', s.get('typical_amount', 0)) for s in subs))}"
             f"/month total:"]
    for sub in subs:
        impact = sub.get("runway_impact_days")
        impact_text = (f", about {impact:.1f} runway days"
                       if impact is not None else ", runway impact unknown")
        lines.append(f"- {sub['vendor']}: ~{_money(sub['typical_amount'])} "
                     f"{sub.get('cadence', '')}, ~{_money(sub.get('monthly_cost', sub['typical_amount']))}/month"
                     f", first detected {sub.get('first_detected_date', '?')}, "
                     f"next expected {sub.get('next_expected', '?')}{impact_text}")
    return "\n".join(lines)


def _tool_shopify_summary(period="this_month"):
    """Storefront aggregates from locally synced Shopify data only."""
    import shopify_client
    if not shopify_client.is_configured():
        return "Shopify is not connected."
    today = date.today()
    if period == "last_month":
        end = today.replace(day=1); start = (end - timedelta(days=1)).replace(day=1)
    else:
        start, end = today.replace(day=1), today + timedelta(days=1)
    orders = db.list_shopify_orders(start.isoformat(), end.isoformat())
    gross = sum(row["total_price"] for row in orders)
    refunds = sum(row["refund_total"] for row in orders)
    products = {}
    for row in orders:
        try: items = json.loads(row["line_items_json"])
        except (ValueError, TypeError): items = []
        for item in items:
            title = item.get("title") or "Unknown product"
            try:
                price = Decimal(str(item.get("price") or 0))
            except (ArithmeticError, TypeError, ValueError):
                price = Decimal(0)
            products[title] = (products.get(title, Decimal(0))
                               + price * int(item.get("quantity") or 0))
    top = max(products, key=products.get) if products else "None"
    unfulfilled = [row for row in orders if row["fulfillment_status"] in ("", "unfulfilled", "partial")]
    return json.dumps({
        "gross_revenue": round(gross, 2),
        "net_revenue": round(gross - refunds, 2),
        "order_count": len(orders),
        "average_order_value": round(gross / len(orders), 2) if orders else 0,
        "refund_total": round(refunds, 2),
        "top_product": top,
        "unfulfilled_count": len(unfulfilled),
        "unfulfilled_total": round(
            sum(row["total_price"] for row in unfulfilled), 2),
    }, default=str)


def _tool_flagged_items():
    """Everything currently flagged, across sections."""
    lines = []
    # Vendor spend jumps (same math the Vendors tab flags with).
    approved = db.list_items(status="approved")
    this_start, this_end = _month_bounds(0)
    last_start, last_end = _month_bounds(1)
    this_month, last_month = {}, {}
    for item in approved:
        if item["direction"] != "expense":
            continue
        if this_start <= item["date"] <= this_end:
            this_month[item["vendor"]] = (
                this_month.get(item["vendor"], Decimal(0))
                                          + item["amount"])
        elif last_start <= item["date"] <= last_end:
            last_month[item["vendor"]] = (
                last_month.get(item["vendor"], Decimal(0))
                                          + item["amount"])
    for vendor, total in sorted(this_month.items(), key=lambda kv: -kv[1]):
        prior = last_month.get(vendor, Decimal(0))
        if prior > 0 and total >= prior * Decimal("1.2"):
            lines.append(f"- Vendor spend jump: {vendor} {_money(total)} "
                         f"this month (+{(total / prior - 1) * 100:.0f}% "
                         f"vs last month)")
    # Reconciliation flag from the latest stored check.
    recon = _latest_reconcile()
    if recon and recon["flagged"]:
        lines.append(f"- Reconciliation flagged: "
                     f"{_money(recon['residual'])} unexplained residual "
                     f"(as of {recon['date']})")
    # Pending backlog.
    pending = db.list_items(status="pending")
    if pending:
        lines.append(f"- Pending review: {len(pending)} item(s), "
                     f"{_money(sum(i['amount'] for i in pending))} total")
    # Sales-tax nexus thresholds (user-entered, tracking aid).
    try:
        for row in db.list_nexus_states():
            threshold = row["threshold"]
            if not threshold:
                continue
            share = row["revenue"] / threshold
            if share >= 1:
                lines.append(f"- Sales-tax nexus REACHED: {row['state']} "
                             f"({_money(row['revenue'])} of "
                             f"{_money(threshold)} threshold)")
            elif share >= Decimal("0.8"):
                lines.append(f"- Sales-tax nexus approaching: "
                             f"{row['state']} ({share * 100:.0f}% of "
                             f"threshold)")
    except Exception:
        pass

    # Queue counts mirror the Invoices and Follow-ups tabs. Only drafts
    # still waiting for review are actionable flags; sent, paid, and
    # skipped history should not make the assistant report a backlog.
    today = date.today().isoformat()
    overdue_invoices = sum(
        1 for item in db.list_invoice_reminders(status="pending_review")
        if item.get("due_date", "") < today
    )
    pending_followups = len(
        db.list_quote_followups(status="pending_review")
    )
    lines.append(f"- overdue_invoices: {overdue_invoices}")
    lines.append(f"- pending_followups: {pending_followups}")

    # Reconcile uses this same Stripe balance endpoint for its in-transit
    # context. Stripe is optional, so an unavailable account degrades to a
    # transparent zero rather than making the AI tool fail.
    stripe_in_transit = 0.0
    try:
        import stripe_client
        if stripe_client.is_configured():
            stripe_in_transit = stripe_client.balance()["in_transit"]
    except Exception:
        pass
    lines.append(f"- stripe_in_transit: {_money(stripe_in_transit)}")
    try:
        orders = db.list_shopify_orders()
        overdue = [row for row in orders if row["fulfillment_status"] in ("", "unfulfilled", "partial") and row.get("created_at", "")[:10] and (date.today() - date.fromisoformat(row["created_at"][:10])).days > 3]
        lines.append(f"- shopify_unfulfilled_over_3_days: {len(overdue)} ({_money(sum(row['total_price'] for row in overdue))})")
    except Exception:
        pass
    return "\n".join(lines) if lines else "Nothing is flagged right now."


def _tool_write_memory(key="", value=""):
    """Persist one durable observation. Length-capped; the value is
    the model's own prose about aggregates, never raw data."""
    key = str(key).strip()[:100]
    value = str(value).strip()[:2000]
    if not key or not value:
        return "write_memory needs both a key and a value."
    db.set_agent_memory(key, value)
    return f'Remembered under "{key}".'


def _memory_block():
    """Stored memories as background context, newest first, capped —
    injected at the start of every session."""
    try:
        rows = db.list_agent_memory()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["", "Notes you saved in earlier sessions (background; "
             "may be stale — re-check with tools before relying on a "
             "number):"]
    for row in rows[:20]:
        lines.append(f'- [{row["key"]}] {row["value"]} '
                     f'(noted {row["updated_at"][:10]})')
    return "\n".join(lines) + "\n"


TOOL_FUNCS = {
    "get_reconciliation": _tool_reconciliation,
    "get_vendor_summary": _tool_vendor_summary,
    "get_cash_position": _tool_cash_position,
    "get_summary": _tool_summary,
    "get_subscriptions": _tool_subscriptions,
    "get_shopify_summary": _tool_shopify_summary,
    "get_flagged_items": _tool_flagged_items,
    "write_memory": _tool_write_memory,
}

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "get_reconciliation",
        "description": "Latest native-books-vs-bank reconciliation: balances, "
                       "difference, unexplained residual, last flagged, "
                       "and pending in/out totals.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_vendor_summary",
        "description": "Top vendors by spend this month, with "
                       "month-over-month jump flags and the detected "
                       "subscription total.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_cash_position",
        "description": "Cash balance, runway in days, pending item "
                       "totals, and this month's net vs last month.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_summary",
        "description": "Money in / money out / net vs the prior period "
                       "and the top expense category, for one month.",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string",
                       "enum": ["this_month", "last_month"],
                       "description": "Which month to summarize."}}}}},
    {"type": "function", "function": {
        "name": "get_shopify_summary",
        "description": "Locally synced Shopify gross/net revenue, orders, refunds, top product, and unfulfilled order totals.",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["this_month", "last_month"]}}}}},
    {"type": "function", "function": {
        "name": "get_subscriptions",
        "description": "Detected recurring vendors: cadence, typical "
                       "and monthly cost, first detected date, next "
                       "expected charge, runway impact, monthly total.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_flagged_items",
        "description": "Everything currently flagged across the app: "
                       "vendor spend jumps, reconciliation residual, "
                       "pending backlog, sales-tax nexus thresholds.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "write_memory",
        "description": "Save a durable note for future sessions — use "
                       "when you discover a recurring pattern or an "
                       "important finding worth remembering. Overwrites "
                       "any note with the same key.",
        "parameters": {"type": "object",
                       "required": ["key", "value"],
                       "properties": {
                           "key": {"type": "string",
                                   "description": "Short stable slug, "
                                                  "e.g. vendor-pattern-"
                                                  "bambu"},
                           "value": {"type": "string",
                                     "description": "The finding, one "
                                                    "or two sentences."},
                       }}}},
]


def run_tool(name, arguments=None):
    """Execute one tool by name. Unknown names and errors both return
    honest text — the model should report, not crash or invent."""
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"Unknown tool: {name}"
    try:
        db.init_db()
        return func(**(arguments or {}))
    except TypeError:
        try:
            return func()
        except Exception as exc:
            return f"Tool {name} unavailable: {exc}"
    except Exception as exc:
        return f"Tool {name} unavailable: {exc}"


# ---------------------------------------------------------- chat loop


class _ToolsUnsupported(RuntimeError):
    pass


_TOOL_SUPPORT_CACHE = {}


def _supports_tools(model):
    """Empirical, cached per process: Ollama's capability flags lie —
    deepseek-r1 advertises "tools" but never emits a tool call (verified
    live, with and without thinking). One small probe per model: a
    direct instruction to call a specific tool. Models that can, do;
    models that can't get the static-context path instead, which is
    grounded and can't hallucinate figures."""
    if model in _TOOL_SUPPORT_CACHE:
        return _TOOL_SUPPORT_CACHE[model]
    supported = False
    try:
        body = {"model": model,
                "messages": [{"role": "user", "content":
                              "Call the get_cash_position tool now."}],
                "tools": TOOLS_SPEC, "stream": False}
        if "thinking" in _capabilities(model):
            body["think"] = False  # keep the probe fast
        response = requests.post(f"{BASE_URL}/api/chat", json=body,
                                 timeout=120)
        if response.status_code == 200:
            msg = response.json().get("message") or {}
            supported = bool(msg.get("tool_calls"))
    except Exception:
        supported = False
    _TOOL_SUPPORT_CACHE[model] = supported
    return supported


def chat_stream(message, section=None, model=None):
    """Answer one user message, yielding think-stripped text chunks.

    Tool-capable models get native tool calling via /api/chat: they
    decide which lookups to run, up to MAX_TOOL_ROUNDS rounds. Models
    that can't call tools (empirically probed — deepseek-r1 among
    them) get the static-context /api/generate path instead, which
    grounds every answer in a pre-built aggregate snapshot."""
    db.init_db()
    model = model or model_name()
    section = (section or "").strip() or "Home"
    if not _supports_tools(model):
        yield from stream_response(
            message, system=(SYSTEM_PROMPT + build_full_context(section)
                             + _memory_block()),
            model=model)
        return
    system = (SYSTEM_PROMPT
              + f"Today's date: {date.today().isoformat()}. "
              + f"The user is currently on the {section} screen.\n"
              + _memory_block())
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": message}]

    for _round in range(MAX_TOOL_ROUNDS + 1):
        tool_calls = []
        content_parts = []

        def rounds_chunks():
            with requests.post(
                f"{BASE_URL}/api/chat",
                json={"model": model, "messages": messages,
                      "tools": TOOLS_SPEC, "stream": True},
                stream=True,
                timeout=120,
            ) as response:
                if response.status_code == 400 and "tool" in response.text:
                    raise _ToolsUnsupported(response.text)
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    for call in msg.get("tool_calls") or []:
                        tool_calls.append(call)
                    piece = msg.get("content", "")
                    if piece:
                        content_parts.append(piece)
                        yield piece
                    if chunk.get("done"):
                        break

        try:
            for text in strip_think_stream(rounds_chunks()):
                yield text
        except _ToolsUnsupported:
            # Model has no tool support — static-context fallback.
            yield from stream_response(
                message, system=SYSTEM_PROMPT + build_context(section),
                model=model)
            return

        if not tool_calls:
            return
        messages.append({"role": "assistant",
                         "content": "".join(content_parts),
                         "tool_calls": tool_calls})
        for call in tool_calls:
            func_info = (call.get("function") or {})
            name = func_info.get("name", "")
            arguments = func_info.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            messages.append({"role": "tool", "tool_name": name,
                             "content": run_tool(name, arguments)})

    yield ("\n[Stopped after too many lookups — try asking a narrower "
           "question.]")


# ------------------------------------------------------ daily insight

INSIGHT_KEY = "daily_insight"
INSIGHT_PROMPT = ("Based on today's financial data, what is the single "
                  "most important thing this business owner should know "
                  "today?")


def generate_daily_insight():
    """One agent run after the daily check: the day's most important
    observation, stored under agent_memory["daily_insight"] for the
    Home banner. Returns the text, or None when Ollama is offline or
    the run produced nothing. Never raises — the daily check must not
    fail over an optional nicety."""
    try:
        if not is_available():
            return None
        text = "".join(chat_stream(INSIGHT_PROMPT, section="Home")).strip()
        if not text or text.startswith("[Stopped after"):
            return None
        db.set_agent_memory(INSIGHT_KEY, text[:1500])
        return text
    except Exception:
        return None
