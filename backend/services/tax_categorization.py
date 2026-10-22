"""Retired Wave Tax-queue categorization retained for historical reference.

No production route imports or calls this module. Native Ledger accounts now
carry explicit human-managed tax categories; archived Wave rows are read-only.
"""

import asyncio
import json
import os

import requests

import ai_providers
import connectivity
import db
import error_alerts
import ollama_client
import secrets_store


SCHEDULE_C_LINES = (
    "gross_receipts", "returns_and_allowances", "advertising",
    "car_and_truck", "commissions_and_fees", "contract_labor",
    "depreciation", "employee_benefits", "insurance",
    "interest_mortgage", "interest_other", "legal_and_professional",
    "office_expense", "rent_vehicles_machinery", "rent_other",
    "repairs_and_maintenance", "supplies", "taxes_and_licenses",
    "travel", "meals", "utilities", "wages", "other_expense",
    "owner_draw", "transfer",
)


def _cloud_key(provider):
    """Use the existing cloud-provider secret names, with env overrides."""
    value = (os.environ.get(f"{provider.upper()}_API_KEY") or "").strip()
    if value:
        return value
    secret_name = {
        "openai": ai_providers.OPENAI_KEY_NAME,
        "anthropic": ai_providers.ANTHROPIC_KEY_NAME,
    }[provider]
    try:
        return (secrets_store.get_secret(secret_name) or "").strip() or None
    except Exception:
        return None


def _available_backend():
    """Follow Klearing's fixed local-first AI priority order."""
    try:
        if ollama_client.is_available():
            return "ollama", None
    except Exception:
        pass
    for provider in ("openai", "anthropic"):
        key = _cloud_key(provider)
        if key:
            return provider, key
    return "none", None


def _value(value):
    return str(value) if value not in (None, "") else "unknown"


def _prompt(row):
    cents = int(row["amount_cents"])
    direction = "income" if cents > 0 else "expense"
    amount = f"${abs(cents) / 100:.2f}"
    allowed_lines = "\n".join(f'"{line}"' for line in SCHEDULE_C_LINES)
    return f'''You are a US small business tax categorization assistant.

Given a transaction, return ONLY a valid JSON object with no preamble, no markdown, no backticks. Return exactly this shape:
{{
"schedule_c_line": "<line label>",
"confidence": <float 0.0-1.0>,
"reasoning": "<one sentence max>"
}}

Use ONLY these schedule_c_line values:

{allowed_lines}

Transaction:
Description: {_value(row["description"])}
Merchant: {_value(row["merchant"])}
Amount: {amount} ({direction})
Wave Category: {_value(row["raw_wave_category"])}
Date: {_value(row["date"])}
'''


def _parse_result(content):
    try:
        result = json.loads(content)
        category = result["schedule_c_line"]
        confidence = float(result["confidence"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if category not in SCHEDULE_C_LINES or not 0.0 <= confidence <= 1.0:
        return None
    reasoning = str(result.get("reasoning") or "")[:1000]
    return category, confidence, reasoning


def _ollama(prompt):
    response = requests.post(
        f"{ollama_client.BASE_URL}/api/generate",
        json={"model": ollama_client.DEFAULT_MODEL, "prompt": prompt,
              "stream": False},
        timeout=20,
    )
    connectivity.raise_for_rate_limit("Ollama", response.status_code)
    response.raise_for_status()
    return ollama_client.strip_think(response.json().get("response", ""))


def _openai(prompt, key):
    response = requests.post(
        ai_providers.OPENAI_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": ai_providers.OPENAI_DEFAULT_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "response_format": {"type": "json_object"}},
        timeout=20,
    )
    connectivity.raise_for_rate_limit("OpenAI", response.status_code)
    response.raise_for_status()
    return ((response.json().get("choices") or [{}])[0]
            .get("message", {}).get("content", ""))


def _anthropic(prompt, key):
    response = requests.post(
        ai_providers.ANTHROPIC_ENDPOINT,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={"model": ai_providers.ANTHROPIC_DEFAULT_MODEL,
              "max_tokens": 300,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=20,
    )
    connectivity.raise_for_rate_limit("Anthropic", response.status_code)
    response.raise_for_status()
    return "".join(block.get("text", "") for block in
                   response.json().get("content", [])
                   if block.get("type") == "text")


def _response_for(backend, prompt, key):
    if backend == "ollama":
        return _ollama(prompt)
    if backend == "openai":
        return _openai(prompt, key)
    return _anthropic(prompt, key)


def _rows_to_categorize(transaction_ids):
    with db.get_connection() as conn:
        if transaction_ids is None:
            query = ("SELECT id, date, description, merchant, amount_cents, "
                     "raw_wave_category FROM kl_transactions "
                     "WHERE user_approved = 0 AND ai_category IS NULL")
            return [dict(row) for row in conn.execute(query).fetchall()]
        ids = [item for item in transaction_ids if isinstance(item, str) and item]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        query = ("SELECT id, date, description, merchant, amount_cents, "
                 "raw_wave_category FROM kl_transactions "
                 f"WHERE user_approved = 0 AND id IN ({placeholders})")
        return [dict(row) for row in conn.execute(query, ids).fetchall()]


def _store_result(row_id, category, confidence, reasoning):
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE kl_transactions SET ai_category = ?, ai_confidence = ?, "
            "ai_reasoning = ? WHERE id = ?",
            (category, confidence, reasoning, row_id),
        )


async def categorize_transactions(transaction_ids=None):
    """Categorize selected pending rows in bounded, rate-limit-friendly batches."""
    db.init_db()
    rows = _rows_to_categorize(transaction_ids)
    backend, key = _available_backend()
    categorized = 0
    failed = 0
    rate_limited = False

    for start in range(0, len(rows), 10):
        for row in rows[start:start + 10]:
            if backend == "none":
                _store_result(row["id"], "uncategorized", 0.0,
                              "No AI backend configured")
                categorized += 1
                continue
            if rate_limited:
                _store_result(row["id"], "uncategorized", 0.0,
                              "AI categorization failed")
                categorized += 1
                failed += 1
                continue
            try:
                result = _parse_result(_response_for(backend, _prompt(row), key))
                if result is None:
                    result = ("uncategorized", 0.0, "Failed to parse AI response")
                _store_result(row["id"], *result)
                categorized += 1
            except Exception as exc:
                if isinstance(exc, connectivity.RateLimitError):
                    # The shared helper already recorded the next-run message.
                    rate_limited = True
                else:
                    error_alerts.record_failure("Tax AI categorization", exc)
                _store_result(row["id"], "uncategorized", 0.0,
                              "AI categorization failed")
                categorized += 1
                failed += 1
        if start + 10 < len(rows):
            await asyncio.sleep(0.5)

    return {"categorized": categorized, "failed": failed,
            "backend_used": backend}
