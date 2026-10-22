"""
AI provider abstraction — Ollama (local, default) plus two optional
cloud providers, OpenAI and Anthropic.

This module does NOT rebuild the assistant. The system prompt, the six
read-only tools, the aggregates-only context pattern, and the memory
block all come straight from ollama_client — the cloud providers are
alternate transports for the exact same brain. Ollama stays the
default and the only provider that runs with zero configuration.

Privacy boundary (see anti-patterns.md #17, amended for this feature):
- Ollama: localhost only, nothing leaves the machine. No warning.
- OpenAI / Anthropic: the same aggregates-only context (sums, counts,
  top-N — never raw transaction rows) is sent to the provider's API.
  Selecting one requires explicit confirmation in Settings, and the
  chat panel shows a persistent "Cloud AI active" pill while one is
  active. The choice is stored, never assumed.

API keys live in the encrypted credential store (secrets_store — the
same OS-keychain-backed path every other credential uses) under
"klearing_openai_key" and "klearing_anthropic_key". A selected cloud
provider with no key is a calm degraded state, never a crash: the
status endpoint reports it and the chat button explains where to add
the key.
"""

import json
from datetime import date

import requests

import connectivity
import db
import ollama_client
import secrets_store

PROVIDER_SETTING_KEY = "klearing_ai_provider"

OPENAI_KEY_NAME = "klearing_openai_key"
ANTHROPIC_KEY_NAME = "klearing_anthropic_key"

OPENAI_DEFAULT_MODEL = "gpt-4o"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"

OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"

# Same planning budget as the local path — enough rounds for
# plan → gather → cross-check, bounded so a confused model can't loop.
MAX_TOOL_ROUNDS = ollama_client.MAX_TOOL_ROUNDS

REQUEST_TIMEOUT = 120

PROVIDER_LABELS = {
    "ollama": "Ollama (local)",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}


def provider_name():
    """The selected provider: 'ollama' (default), 'openai', or
    'anthropic'. Unknown stored values fall back to ollama — the one
    provider that can never surprise the user."""
    stored = (db.get_setting(PROVIDER_SETTING_KEY) or "").strip().lower()
    return stored if stored in PROVIDER_LABELS else "ollama"


def is_cloud(name=None):
    return (name or provider_name()) != "ollama"


def _api_key(name):
    secret = {"openai": OPENAI_KEY_NAME,
              "anthropic": ANTHROPIC_KEY_NAME}.get(name)
    if not secret:
        return None
    try:
        return (secrets_store.get_secret(secret) or "").strip() or None
    except Exception:
        # A credential-store hiccup reads as "no key" — degraded, calm,
        # never a crash in the chat path.
        return None


def has_api_key(name):
    return _api_key(name) is not None


def status():
    """What the chat UI needs to draw itself: availability, whether the
    active provider is cloud, and the calm hint to show when it isn't
    usable. Never raises."""
    name = provider_name()
    if name == "ollama":
        available = ollama_client.is_available()
        hint = "Start Ollama to use the assistant."
    else:
        available = has_api_key(name)
        hint = "Add your API key in Settings to use this provider."
    return {
        "available": available,
        "provider": name,
        "label": PROVIDER_LABELS[name],
        "cloud": is_cloud(name),
        "hint": hint,
    }


# ------------------------------------------------------ shared plumbing


def _system_prompt(section):
    """The exact same system prompt + memory block the Ollama path
    builds — one brain, three transports."""
    section = (section or "").strip() or "Home"
    return (ollama_client.SYSTEM_PROMPT
            + f"Today's date: {date.today().isoformat()}. "
            + f"The user is currently on the {section} screen.\n"
            + ollama_client._memory_block())


def _run_tool(name, arguments):
    return ollama_client.run_tool(name, arguments or {})


def _parse_json(text):
    try:
        return json.loads(text) if isinstance(text, str) else (text or {})
    except ValueError:
        return {}


# ------------------------------------------------------------- OpenAI


def _openai_tools():
    """TOOLS_SPEC is already OpenAI-shaped ({'type': 'function',
    'function': {...}}) — Ollama adopted OpenAI's format."""
    return ollama_client.TOOLS_SPEC


def _openai_stream(message, section):
    """One user message through OpenAI chat completions with the same
    tool loop the Ollama path runs. Non-streamed per round (tool-call
    turnaround dominates latency); the final answer is yielded as one
    chunk, which the chat UI's reader handles fine."""
    key = _api_key("openai")
    if key is None:
        yield "Add your API key in Settings to use this provider."
        return
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    messages = [{"role": "system", "content": _system_prompt(section)},
                {"role": "user", "content": message}]

    for _round in range(MAX_TOOL_ROUNDS + 1):
        response = requests.post(
            OPENAI_ENDPOINT, headers=headers,
            json={"model": OPENAI_DEFAULT_MODEL, "messages": messages,
                  "tools": _openai_tools()},
            timeout=REQUEST_TIMEOUT)
        if response.status_code == 429:
            yield connectivity.rate_limit_message("OpenAI")
            return
        if response.status_code == 401:
            yield ("OpenAI rejected the API key — check it in "
                   "Settings.")
            return
        response.raise_for_status()
        choice = (response.json().get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            yield msg.get("content") or ""
            return
        messages.append(msg)
        for call in tool_calls:
            func = call.get("function") or {}
            result = _run_tool(func.get("name", ""),
                               _parse_json(func.get("arguments")))
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", ""),
                             "content": result})

    yield ("\n[Stopped after too many lookups — try asking a narrower "
           "question.]")


# ----------------------------------------------------------- Anthropic


def _anthropic_tools():
    """Anthropic's tool shape: name/description/input_schema at the
    top level, converted from the shared TOOLS_SPEC."""
    tools = []
    for spec in ollama_client.TOOLS_SPEC:
        func = spec.get("function") or {}
        tools.append({"name": func.get("name", ""),
                      "description": func.get("description", ""),
                      "input_schema": func.get("parameters")
                      or {"type": "object", "properties": {}}})
    return tools


def _anthropic_stream(message, section):
    """The same tool loop against Anthropic's Messages API. tool_use
    content blocks map onto the shared tool table; results go back as
    tool_result blocks."""
    key = _api_key("anthropic")
    if key is None:
        yield "Add your API key in Settings to use this provider."
        return
    headers = {"x-api-key": key,
               "anthropic-version": "2023-06-01",
               "Content-Type": "application/json"}
    messages = [{"role": "user", "content": message}]

    for _round in range(MAX_TOOL_ROUNDS + 1):
        response = requests.post(
            ANTHROPIC_ENDPOINT, headers=headers,
            json={"model": ANTHROPIC_DEFAULT_MODEL,
                  "max_tokens": 1024,
                  "system": _system_prompt(section),
                  "messages": messages,
                  "tools": _anthropic_tools()},
            timeout=REQUEST_TIMEOUT)
        if response.status_code == 429:
            yield connectivity.rate_limit_message("Anthropic")
            return
        if response.status_code == 401:
            yield ("Anthropic rejected the API key — check it in "
                   "Settings.")
            return
        response.raise_for_status()
        body = response.json()
        content = body.get("content") or []
        if body.get("stop_reason") != "tool_use":
            yield "".join(block.get("text", "") for block in content
                          if block.get("type") == "text")
            return
        messages.append({"role": "assistant", "content": content})
        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            results.append({"type": "tool_result",
                            "tool_use_id": block.get("id", ""),
                            "content": _run_tool(block.get("name", ""),
                                                 block.get("input"))})
        messages.append({"role": "user", "content": results})

    yield ("\n[Stopped after too many lookups — try asking a narrower "
           "question.]")


# ------------------------------------------------------------ dispatch


def chat_stream(message, section=None):
    """Answer one message through whichever provider is selected.
    The Ollama path is untouched — this only adds routes around it."""
    db.init_db()
    name = provider_name()
    if name == "openai":
        yield from _openai_stream(message, section)
    elif name == "anthropic":
        yield from _anthropic_stream(message, section)
    else:
        yield from ollama_client.chat_stream(message, section=section)


def is_available():
    return status()["available"]
