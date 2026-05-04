"""
title_generator.py — auto-name conversations from the first turn (v1.9.0).

WHY:
Pre-v1.9 the `/resume` picker showed conversation IDs (timestamp + hex)
and turn count. On a busy day with 8 conversations open you can't tell
them apart. Hermes solves this by calling a cheap LLM after the first
turn to name the conversation in 3-6 words.

WHEN IT FIRES:
- After the FIRST turn completes (a conversation with one turn finally
  has enough signal to name).
- Skipped when title is already set (idempotent — re-saving doesn't
  re-call the model).
- Skipped if the request is purely conversational fluff ("hi", "thanks").
  Heuristic: skip if first request is < 12 chars OR starts with greeting
  words.
- Skipped if config.TITLE_AUTO_GENERATE is disabled (env opt-out).

CHEAP MODEL:
Uses config.memory_model() which the user can override with
JANUS_MEMORY_MODEL — by default falls back to the main MODEL.

FAILURE MODE:
On any error (HTTP 5xx, timeout, parse failure) we leave title=""
and never retry. The next save will see title=="" and try again — but
since most calls happen in the post-turn save path, it's at most one
LLM call per turn boundary. P8 (errors are observations): a missing
title is just a missing convenience, not a crash.
"""

from __future__ import annotations
import os
import re

from . import config


_GREETING_WORDS = (
    "hi", "hello", "hey", "thanks", "thank", "ok", "okay",
    "yes", "no", "yo", "sup",
)


def should_generate_title(c) -> bool:
    """Skip checks for shallow / disabled cases."""
    if c.title:
        return False  # already named
    if not c.turns:
        return False  # nothing to name
    # Env opt-out (default ON via JANUS_TITLE_AUTO=1).
    if os.getenv("JANUS_TITLE_AUTO", "1") in ("0", "false", "no"):
        return False
    first_req = (c.turns[0].get("request") or "").strip()
    if len(first_req) < 12:
        return False
    first_word = first_req.split(maxsplit=1)[0].lower().rstrip(",.!?")
    if first_word in _GREETING_WORDS:
        return False
    return True


_TITLE_SYSTEM = """You name a Janus conversation in 3-6 words.

Read the first user request and the first agent reply. Produce ONE
short title that captures the GIST of what was being worked on. No
quotes, no preamble, no markdown — just the title text.

Good titles:
- Refactor janus memory module
- Compare janus and hermes
- Build samoul news agent
- Debug telegram approval keyboard

Bad titles:
- A conversation about the user's project (too vague)
- "Refactor janus memory module" (don't quote)
- The user asked Janus to refactor the memory module... (too long)"""


def generate_title(c) -> str:
    """Single LLM call to produce a title for the first turn.

    Returns the title string. Empty string on failure (caller can
    check and skip). Never raises — failure is silent.
    """
    if not c.turns:
        return ""
    first = c.turns[0]
    user_req = (first.get("request") or "").strip()[:500]
    agent_out = (first.get("output") or "").strip()[:1000]

    user_msg = (
        f"USER REQUEST:\n{user_req}\n\n"
        f"AGENT REPLY (first 1000 chars):\n{agent_out}"
    )

    # Single requests POST — same wire format as memory.propose_diff,
    # not pulling in llm.chat (which uses config.MODEL globally) so we
    # can pin the model explicitly to memory_model.
    import requests
    try:
        r = requests.post(
            f"{config.API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.memory_model(),
                "messages": [
                    {"role": "system", "content": _TITLE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.3,
                "max_tokens": 30,
            },
            timeout=min(30, config.LLM_TIMEOUT),
        )
        r.raise_for_status()
        title = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""

    return _normalize_title(title)


def _normalize_title(raw: str) -> str:
    """Strip quotes, trailing punctuation, length cap. Lowercase only the
    first letter if the rest is already lowercase (so 'Janus' stays
    capitalized but 'do this' becomes 'Do this').
    """
    t = raw.strip()
    # Strip outer quotes if both present.
    while len(t) >= 2 and t[0] in ('"', "'", "`") and t[-1] == t[0]:
        t = t[1:-1].strip()
    # Strip trailing period / ellipsis (the model loves to add them).
    t = re.sub(r"[.!?…]+$", "", t).strip()
    # Hard cap.
    if len(t) > 80:
        t = t[:79] + "…"
    if not t:
        return ""
    # Capitalize first letter if the result is all lowercase.
    if t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def maybe_generate(c) -> bool:
    """Convenience: check + generate + assign in one call.

    Returns True if a title was generated and assigned (so callers know
    to re-save the conversation). False otherwise.
    """
    if not should_generate_title(c):
        return False
    t = generate_title(c)
    if not t:
        return False
    c.title = t
    return True
