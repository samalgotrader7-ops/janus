"""
tool_call_recovery.py — recover tool calls leaked into content (v1.17.2).

WHY THIS EXISTS:
Some endpoints (notably self-hosted vLLM without
`--enable-auto-tool-choice --tool-call-parser <parser>`) emit the model's
tool call as JSON in the `content` field instead of the proper OpenAI-
shaped `tool_calls` field. The chat loop sees no tool_calls, treats the
content as a final answer, and the user sees raw JSON dumped to chat.

This module detects that pattern and synthesizes a proper tool_call so
the loop can continue. It's a DEFENSIVE measure — the architecturally
correct fix is to start the endpoint with the right tool-call parser.
But Janus should still work when the endpoint is misconfigured.

Recognized shapes (in priority order):
  1. ```json``` markdown-fenced JSON → strip fences first
  2. Explicit-named: {"name": "X", "arguments": {...}}
                     {"tool": "X", "args": {...}}
                     {"function": {"name": "X", "arguments": {...}}}
  3. Shape-matched: keys exactly cover one tool's parameter properties
     and include all required parameters, with no foreign keys.

The shape-matching is restrictive on purpose — false positives would
mean executing a tool the model didn't ask for. The exact-key-match
rule means {"path": "x.py", "content": "..."} matches fs_write but
{"path": "x", "extra": "stuff"} matches nothing.
"""

from __future__ import annotations
import json
import re
import uuid
from typing import Any


def recover(content: str, schemas: list[dict]) -> dict | None:
    """If content is a JSON-shaped tool call, return an OpenAI-shaped
    tool_call dict. None if no recovery possible.

    Shape returned matches one element of `tool_calls`:
        {"id": "...", "type": "function",
         "function": {"name": "...", "arguments": "<json string>"}}

    Args:
      content: the assistant message content (may be empty / non-JSON).
      schemas: tools.schemas() — list of {"function": {...}} dicts.
    """
    if not content or not schemas:
        return None

    parsed = _try_parse_json(content)
    if not isinstance(parsed, dict):
        return None

    # Priority 1: explicit name/arguments shape.
    explicit = _explicit_name_shape(parsed)
    if explicit:
        name, args = explicit
        if _tool_exists(name, schemas):
            return _build_tool_call(name, args)
        # Explicit name doesn't match any registered tool — refuse rather
        # than fall back to shape matching. The model named a specific
        # tool; substituting another would be wrong.
        return None

    # Priority 2: shape match against tool schemas by key set.
    matched = _shape_match(parsed, schemas)
    if matched:
        name, args = matched
        return _build_tool_call(name, args)

    return None


# ---------- Internal ----------


def _strip_fences(text: str) -> str:
    """Strip markdown code fences. ```json\n{...}\n``` → {...}."""
    text = text.strip()
    # Match ```<lang>\n<body>\n```  with optional language tag.
    m = re.match(
        r"^```(?:json|javascript|js|tool_call|function)?\s*\n?(.*?)\n?```\s*$",
        text, re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return text


def _try_parse_json(text: str) -> Any | None:
    """Strict JSON-only check: text must be a fenced or bare JSON object.

    We don't try to extract JSON embedded in prose — too risky for false
    positives. The model that's leaking tool calls into content emits
    pure JSON (sometimes fenced); that's what we look for.
    """
    text = _strip_fences(text).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _explicit_name_shape(parsed: dict) -> tuple[str, dict] | None:
    """Detect shapes that explicitly name a tool.

    Recognized:
      - {"name": "X", "arguments": {...} or "<json string>"}
      - {"tool": "X", "args": {...}}
      - {"tool_name": "X", "tool_args": {...}}
      - {"function": {"name": "X", "arguments": {...} or "<json>"}}
    """
    name = ""
    args: Any = {}

    # Top-level name + arguments.
    if isinstance(parsed.get("name"), str) and "arguments" in parsed:
        name = parsed["name"]
        args = parsed["arguments"]
    elif isinstance(parsed.get("tool"), str) and "args" in parsed:
        name = parsed["tool"]
        args = parsed["args"]
    elif isinstance(parsed.get("tool_name"), str) and "tool_args" in parsed:
        name = parsed["tool_name"]
        args = parsed["tool_args"]
    elif isinstance(parsed.get("function"), dict):
        fn = parsed["function"]
        if isinstance(fn.get("name"), str):
            name = fn["name"]
            args = fn.get("arguments", {})

    if not name:
        return None

    # Arguments may be a JSON-string (OpenAI's wire shape) — unwrap.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    return name, args


def _shape_match(parsed: dict, schemas: list[dict]) -> tuple[str, dict] | None:
    """Find the tool whose schema best matches the parsed JSON's keys.

    Rules (intentionally restrictive — false positives = executing a
    tool the model didn't ask for):
      1. ALL required parameters must be in parsed.
      2. EVERY key in parsed must be a valid property of the tool.
      3. Among candidates, prefer the one with the most required keys
         (most specific match) and tie-break on most-keys-overall.

    Returns None if no clean match.
    """
    if not isinstance(parsed, dict) or not parsed:
        return None
    parsed_keys = set(parsed.keys())
    candidates: list[tuple[tuple[int, int], str]] = []

    for schema in schemas:
        fn = schema.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        params = fn.get("parameters") or {}
        properties = params.get("properties") or {}
        required = set(params.get("required") or [])
        prop_keys = set(properties.keys())

        # Skip tools with no parameters — any JSON would match them,
        # which is exactly the wrong behavior.
        if not prop_keys:
            continue
        # ALL required keys must be present.
        if not required.issubset(parsed_keys):
            continue
        # NO foreign keys allowed.
        if not parsed_keys.issubset(prop_keys):
            continue
        # Score: number of required keys matched, then total key overlap.
        score = (len(required), len(parsed_keys & prop_keys))
        candidates.append((score, name))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1], parsed


def _tool_exists(name: str, schemas: list[dict]) -> bool:
    return any(
        (s.get("function") or {}).get("name") == name for s in schemas
    )


def _build_tool_call(name: str, args: dict) -> dict:
    """Build an OpenAI-shaped tool_call dict."""
    return {
        "id": f"call_recovered_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }
