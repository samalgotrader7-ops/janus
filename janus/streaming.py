"""
streaming.py — incremental SSE chat stream parser (Phase 14).

WHY:
Without streaming the user sees nothing until the entire response is
ready. With streaming you see the model "type" token by token. Order-
of-magnitude better feel for any non-trivial answer.

PROTOCOL:
OpenAI-compatible Server-Sent Events. Each `data: <json>` line carries
a chunk shaped like:
    {"choices": [{"delta": {"content": "Hello"}, "index": 0}]}
Chunks may carry `delta.tool_calls` for streaming tool-call assembly.
The stream terminates with `data: [DONE]`.

Anthropic-direct uses a different envelope (event names, content
blocks). We don't speak Anthropic-direct — providers like OpenRouter
adapt Anthropic to OpenAI shape. If the user ever points
`JANUS_API_BASE` at api.anthropic.com directly, this module needs an
extension.
"""

from __future__ import annotations
import json
from typing import Any, Iterator

import requests

from . import config


def chat_stream(
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    temperature: float = 0.7,
    model: str | None = None,
) -> Iterator[Any]:
    """Streaming chat completion.

    Yields:
      str — each text delta as it arrives.
      dict (final yield) — the assembled message:
            {"role": "assistant", "content": "...", "tool_calls": [...]}
        Caller can read tool_calls from this for the next loop iteration.

    `model` overrides config.MODEL per call (v1.4 — used by swarms to
    mix cheap/expensive models per role).

    Retry/backoff applies only to the INITIAL connect. Mid-stream
    failures (server hangs up partway through SSE) are not resumable —
    the partial response is returned.
    """
    url = f"{config.API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    from . import llm
    chosen_model = model or config.MODEL
    payload: dict[str, Any] = {
        "model": chosen_model,
        "messages": llm.apply_cache_markers(messages),
        "temperature": temperature,
        "stream": True,
    }
    # v1.16.2: respect JANUS_NO_TOOLS — same reason as in llm.chat.
    if tools and not config.NO_TOOLS:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    with llm._post_with_retry(
        url, headers=headers, json_payload=payload,
        timeout=config.LLM_TIMEOUT, stream=True,
    ) as r:
        # v1.16.1 — same actionable 404 as llm.chat. Streaming-mode 404s
        # are exactly the same failure shape (model id not found at the
        # endpoint), just hit through a different code path.
        # v1.16.2 — also pass tools-presence so the message can suggest
        # JANUS_NO_TOOLS=1 when relevant.
        if r.status_code == 404:
            raise llm._explain_404(
                chosen_model, config.API_BASE, r,
                had_tools=bool(payload.get("tools")),
            )
        r.raise_for_status()
        accumulated = ""
        # Tool-call accumulation: provider streams partial deltas, we
        # merge by index. Each entry: {id, type, function: {name, arguments}}
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None

        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            blob = line[5:].strip()
            if blob == "[DONE]":
                break
            try:
                chunk = json.loads(blob)
            except json.JSONDecodeError:
                continue

            # Some providers send a final usage block on the last event.
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            text = delta.get("content")
            if text:
                accumulated += text
                yield text

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    # Push usage to the cost tracker (same path as llm.chat).
    try:
        from . import cost
        cost.record(chosen_model, usage)
    except Exception:
        pass

    final: dict[str, Any] = {
        "role": "assistant",
        "content": accumulated,
    }
    if tool_acc:
        final["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    yield final
