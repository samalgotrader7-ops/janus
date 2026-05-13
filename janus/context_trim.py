"""
janus.context_trim — shared conversation-history trimmer (v1.42.4).

WHY THIS EXISTS:
Long-running sessions (especially `/goal` auto-continue loops, multi-turn
Telegram chats, and any conversation that accumulates large tool
results) can grow past the LLM's request size limit. With MiMo-V2.5-Pro
on the OpenAI-compatible endpoint, requests over ~25K tokens or ~80K
characters can return 500 errors silently. Worse, the cost per turn
keeps climbing.

This module owns the budget enforcement. v1.41.4 had a private copy
in `gateways/telegram.py`; v1.42.4 hoists it here so every surface
(Telegram, web, CLI) trims uniformly.

CONTRACT:
  trim_messages(messages, max_chars=...) -> list[dict]
  - Preserves the FIRST message if role=='system' (the big static prompt).
  - Walks BACKWARDS through the rest keeping the newest messages that fit.
  - If the most-recent message alone exceeds budget, truncates its
    content to fit (preserves user intent without breaching budget).
  - Inserts a synthetic "earlier conversation trimmed" system message
    so the model knows context was dropped.

ENV: JANUS_MAX_CONTEXT_CHARS (default 80000 ≈ 20K tokens).
"""

from __future__ import annotations

import os
from typing import Any


DEFAULT_MAX_CHARS = int(os.environ.get("JANUS_MAX_CONTEXT_CHARS", "80000"))


def _content_len(m: dict) -> int:
    """Char length of message content. Returns 0 for non-string content
    (callers using cache_control blocks already know their size)."""
    c = m.get("content", "")
    return len(c) if isinstance(c, str) else 0


def trim_messages(
    messages: list[dict[str, Any]],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Trim conversation history to fit within a character budget.

    Returns a NEW list (does not mutate the input). The session file
    still retains ALL messages; only the API payload is trimmed.
    """
    if not messages:
        return messages
    total = sum(_content_len(m) for m in messages)
    if total <= max_chars:
        return list(messages)

    # Preserve the system message (first message if role=system).
    system_msgs: list[dict[str, Any]] = []
    rest = list(messages)
    if messages and messages[0].get("role") == "system":
        system_msgs = [messages[0]]
        rest = list(messages[1:])

    kept: list[dict[str, Any]] = []
    kept_chars = sum(_content_len(m) for m in system_msgs)
    for m in reversed(rest):
        m_chars = _content_len(m)
        if kept_chars + m_chars > max_chars:
            if not kept and isinstance(m.get("content"), str):
                # First (most-recent) message and it doesn't fit alone —
                # truncate its content to use whatever budget remains
                # (minus a 200-char header for the truncation notice).
                budget_for_msg = max(500, max_chars - kept_chars - 200)
                trimmed_msg = dict(m)
                trimmed_msg["content"] = (
                    m["content"][:budget_for_msg]
                    + f"\n\n[message truncated from {m_chars} to "
                    f"{budget_for_msg} chars to fit context window]"
                )
                kept.append(trimmed_msg)
            break
        kept.append(m)
        kept_chars += m_chars

    kept.reverse()
    result = system_msgs + kept
    if len(result) < len(messages):
        dropped = len(messages) - len(result)
        # Synthetic notice so the model knows older context was trimmed.
        result.insert(len(system_msgs), {
            "role": "system",
            "content": (
                f"[Earlier conversation trimmed: {dropped} messages "
                f"dropped to fit context window of {max_chars} chars]"
            ),
        })
    return result
