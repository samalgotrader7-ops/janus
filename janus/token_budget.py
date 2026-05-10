"""
token_budget.py — token-budget-aware compression decision (v1.35.3,
Phase 9.2).

WHY:
Conversation compaction (conversation.py compact_old_turns) pre-
v1.35.3 fires at a fixed turn count (JANUS_COMPACT_THRESHOLD,
default 20 turns). Heuristic but unaware of:
  * actual token count (a 20-turn convo with short turns is fine;
    a 5-turn convo with long tool outputs may already overflow)
  * the model's context window size

This module ships a budget-aware compression decision:
  should_compact(messages, model) → bool
  estimate_tokens(text) → int (rough word-count heuristic)

KEEP IT SIMPLE:
We don't pull tiktoken or any tokenizer dependency. Word-count
× 1.3 is the standard rough estimate (English text ≈ 1 token per
0.75 words). Off by ~10-20% but enough to gate compaction on
"are we near the model's window?".

Per-model windows are looked up from KNOWN_MODEL_WINDOWS; unknown
models default to 128K (modern baseline). Users can override with
JANUS_CONTEXT_WINDOW.

DEFAULT BEHAVIOR PRESERVED:
JANUS_TOKEN_BUDGET_COMPACT defaults OFF; existing turn-count
threshold continues to fire. When set, the compaction check uses
the budget heuristic instead — fires when used > 70% of the
window (configurable via JANUS_COMPACT_RATIO).
"""

from __future__ import annotations

import os
import re
from typing import Iterable


# Approximate context windows for common models. Add via PR.
KNOWN_MODEL_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-7": 200_000,
    # OpenAI
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "o1-mini": 128_000,
    # Llama 3 / Mistral common sizes
    "llama-3-8b": 128_000,
    "llama-3-70b": 128_000,
    "mistral-large": 128_000,
    "mistral-7b": 32_768,
}

DEFAULT_WINDOW = 128_000
DEFAULT_RATIO = 0.70


def is_enabled() -> bool:
    return os.environ.get(
        "JANUS_TOKEN_BUDGET_COMPACT", "0",
    ).lower() in ("1", "true", "yes", "on")


def context_window(model: str) -> int:
    """Resolve the context window for `model`. Strips provider
    prefixes (e.g. 'anthropic/' / 'openai/') for lookup. Honors
    JANUS_CONTEXT_WINDOW env override."""
    override = os.environ.get("JANUS_CONTEXT_WINDOW")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    if not model:
        return DEFAULT_WINDOW
    # Strip provider prefix: 'anthropic/claude-haiku-4-5' → 'claude-haiku-4-5'
    if "/" in model:
        model = model.split("/", 1)[1]
    return KNOWN_MODEL_WINDOWS.get(model, DEFAULT_WINDOW)


def compact_ratio() -> float:
    """JANUS_COMPACT_RATIO env override (0.0 to 1.0). Default 0.70 —
    triggers when we've used 70% of the window."""
    raw = os.environ.get("JANUS_COMPACT_RATIO")
    if not raw:
        return DEFAULT_RATIO
    try:
        ratio = float(raw)
    except ValueError:
        return DEFAULT_RATIO
    return max(0.1, min(0.99, ratio))


_WORD_PAT = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """Rough word-count × 1.3 estimate. English text is roughly
    0.75 words per token, so words × 1.3 ≈ tokens. Off by 10-20%
    but enough for budget-gating decisions."""
    if not text:
        return 0
    words = len(_WORD_PAT.findall(text))
    return int(words * 1.3)


def estimate_messages_tokens(messages: Iterable[dict]) -> int:
    """Sum token estimates across a message list. Walks 'content'
    fields (string OR list-of-blocks shape that apply_cache_markers
    produces)."""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or ""
                    total += estimate_tokens(str(text))
                elif isinstance(block, str):
                    total += estimate_tokens(block)
    return total


def should_compact(messages: Iterable[dict], model: str) -> bool:
    """Return True if the message list is using >= compact_ratio()
    of the model's context window."""
    if not is_enabled():
        return False
    used = estimate_messages_tokens(messages)
    window = context_window(model)
    threshold = int(window * compact_ratio())
    return used >= threshold
