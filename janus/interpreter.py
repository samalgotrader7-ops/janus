"""
interpreter.py — the heart of Janus.

This is the IP. Tune relentlessly.

WHAT IT DOES:
Given a user request, return 1-3 distinct interpretations the user can
choose between. The model is told NOT to answer — only to interpret.

WHY ITS OWN MODULE:
Because we'll iterate on this prompt more than any other code in the
project. Isolating it makes A/B comparisons trivial: swap the prompt,
re-run a fixed set of recorded prompts from the log, see if interpretation
quality moves.
"""

from __future__ import annotations
from typing import TypedDict

from . import llm


class Interpretation(TypedDict):
    label: str
    action: str
    risk: str


SYSTEM = """You are an interpreter, not an executor.

The user will give you a request. Do NOT answer it directly.

Your job: identify 2-3 genuinely DIFFERENT things they might want.
"Different" means the actions you'd take are different — not just different wordings.

Rank by likelihood given ONLY the request itself (no other context).

For each interpretation:
- label: a 5-8 word title (concrete, action-oriented)
- action: one paragraph describing what you'd actually do
- risk: what could go wrong, or why this might be the wrong reading

If the request is genuinely unambiguous (e.g. "what is 2+2", or contains
explicit instructions for a single specific action), return only ONE
interpretation.

Output strictly JSON with this schema:
{
  "interpretations": [
    {"label": "...", "action": "...", "risk": "..."}
  ]
}

No prose outside the JSON. No markdown fences. Just the JSON object."""


def interpret(
    user_request: str,
    *,
    memory_preamble: str = "",
    skill_hints: str = "",
    temperature: float = 0.7,
) -> list[Interpretation]:
    """Return 1-3 interpretation candidates.

    `memory_preamble`: optional user.md context. Prepended to the SYSTEM prompt
        when present. Empty by default to preserve Phase 1 behavior.
    `skill_hints`: optional list of relevant skills the model should be aware
        of (purely informational — picking is the user's job).
    `temperature`: pinned at 0 by the eval harness for deterministic replays.
    """
    system_parts = [SYSTEM]
    if memory_preamble:
        system_parts.append("\n\n" + memory_preamble)
    if skill_hints:
        system_parts.append(
            "\n\nThe following skills exist and may be relevant. "
            "Do NOT reference them in the interpretations — the user picks them "
            "separately. They are listed only to help you produce sharper labels:\n"
            + skill_hints
        )
    system = "".join(system_parts)

    msg = llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_request},
        ],
        json_mode=True,
        temperature=temperature,
    )
    data = llm.parse_json_loose(msg.get("content") or "{}")
    items = data.get("interpretations") or []
    # Defensive: clamp to 3, ensure required fields exist.
    out: list[Interpretation] = []
    for x in items[:3]:
        out.append({
            "label": str(x.get("label", "")).strip() or "(unnamed)",
            "action": str(x.get("action", "")).strip(),
            "risk": str(x.get("risk", "")).strip() or "—",
        })
    return out
