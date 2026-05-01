"""
llm.py — minimal LLM client.

DESIGN NOTE:
Two functions only:
  - chat(messages, tools=None, json_mode=False) -> response dict
  - extract_text(response) / extract_tool_calls(response) -> typed accessors

Why not use openai-python or litellm?
  We support ANY OpenAI-compatible endpoint, including weird local ones
  whose pydantic schemas don't quite match. A 50-line raw-requests client
  is more portable than a fat SDK that breaks on minor protocol drift.
  When the project matures we can swap in litellm; for now, simple wins.

This is the executor's bridge to the world. Tool calling format follows the
OpenAI spec (Anthropic, OpenRouter, Mistral, DeepSeek all match it).
"""

from __future__ import annotations
import json
from typing import Any

import requests

from . import config


def apply_cache_markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Phase 20: wrap the LAST system message in Anthropic-style content
    blocks with `cache_control: ephemeral`. OpenRouter honors this and
    forwards to Anthropic's prompt cache; OpenAI ignores extra fields.

    No-op when:
      - JANUS_PROMPT_CACHE is off (default)
      - the message list has no system message
      - the system content is already a list (caller built blocks itself)
    """
    if not config.PROMPT_CACHE_MARKERS:
        return messages
    out: list[dict[str, Any]] = []
    last_system_idx = -1
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            last_system_idx = i
    for i, m in enumerate(messages):
        if i == last_system_idx and isinstance(m.get("content"), str):
            out.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": m["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        else:
            out.append(m)
    return out


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
) -> dict:
    """Single chat completion. Returns the raw 'message' object from the response."""
    url = f"{config.API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": config.MODEL,
        "messages": apply_cache_markers(messages),
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    r = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    # Phase 13: feed token usage to the cost tracker. Non-fatal — providers
    # that omit `usage` (some local backends) are silently treated as zero.
    try:
        from . import cost
        cost.record(payload.get("model") or config.MODEL, body.get("usage"))
    except Exception:
        pass
    return body["choices"][0]["message"]


def chat_stream(messages, tools=None, temperature=0.7):
    """Re-export of streaming.chat_stream so callers don't need to import
    a second module just to switch modes. See streaming.py for shape.
    """
    from . import streaming
    return streaming.chat_stream(messages, tools=tools, temperature=temperature)


def parse_json_loose(raw: str) -> Any:
    """Accept JSON even if the model wrapped it in markdown fences.

    Some smaller models ignore response_format=json_object and emit fenced
    blocks anyway. We strip the fences rather than fail.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw.strip())
