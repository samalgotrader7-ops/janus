"""
memory_consolidate.py — LLM-driven reflection pass (v1.18.0 Phase 8).

Manual only — invoked via ``/memory consolidate`` or ``janus memory
consolidate``. NO built-in cron. Users wire ``agent_create`` (v1.6) with
a schedule string if they want automated cadence.

WHAT IT DOES:
Read all (active, non-superseded) cards. Ask the LLM to identify
patterns, redundancies, or syntheses across MULTIPLE cards. Produce 0-5
new cards with ``origin_kind=consolidation`` summarizing the patterns.

The new cards land in the same store; pre-injection recall picks them
up like any other card. They get high durability (0.7+) by design — a
synthesis of multiple sources is more durable than any single fact.

WHY THERE'S NO BUILT-IN CRON:
LLM calls cost money. Defaulting to "every 60 min" would burn through a
user's budget without their awareness. Manual + the existing trigger
system covers all use cases:
  - Sam types ``/memory consolidate`` once a week → manual control
  - Sam runs ``agent_create("memory consolidator", schedule="every 60 min",
    purpose="Run /memory consolidate")`` → opt-in cadence using existing
    Janus machinery
"""

from __future__ import annotations
import json
from pathlib import Path

from . import config, llm, memory, memory_cards, memory_extract, memory_index


CONSOLIDATE_SYSTEM = """You are a memory consolidator. You receive a list
of structured memory cards from the user's history. Identify patterns,
redundancies, contradictions, or syntheses worth recording as new cards.

CARD TYPES (8): identity, preference, goal, project, habit, decision,
constraint, relationship.

Rules:
- ONLY produce cards that synthesize from MULTIPLE existing cards.
  A "synthesis" means: the new card states something that follows from
  several existing cards together, not from any one alone.
- Set durability HIGH (0.7-1.0) for synthesis cards — they are
  load-bearing reflections, more durable than the underlying facts.
- Set confidence based on how strong the pattern is:
    0.9+ if 3+ existing cards clearly align
    0.7-0.8 if pattern is solid but with some variance
    don't produce the card if the pattern is weak (<0.7)
- Set scope to "global" — syntheses cross-cut individual sources.
- ALWAYS set conflict_resolution="append" — synthesis never replaces.
- Cap at 5 cards per pass. Most passes produce ZERO cards (no new
  patterns since last run is the most common case).

Return STRICT JSON (no prose, no fences):
{
  "cards": [
    {"type": "...", "subject": "...", "content": "...",
     "confidence": 0.x, "importance": 0.x, "durability": 0.x,
     "scope": "global",
     "conflict_with": null, "conflict_resolution": "append"}
  ]
}
"""


def run_once(*, max_input_cards: int = 200) -> dict:
    """Single consolidation pass.

    Returns ``{"examined": int, "written": int}`` — examined is cards
    we sent to the model; written is reflection cards persisted.
    """
    try:
        memory_index.reconcile()
    except Exception:
        pass

    rows = memory_index.list_all()
    if len(rows) < 3:
        # No point synthesizing across <3 cards.
        return {"examined": len(rows), "written": 0}

    # Build the input — load each card's full content.
    summaries: list[dict] = []
    for r in rows[:max_input_cards]:
        try:
            card = memory_cards.read_card(Path(r["path"]))
        except Exception:
            continue
        summaries.append({
            "id": card.id,
            "type": card.type,
            "subject": card.subject,
            "content": card.content[:200],
            "confidence": card.confidence,
            "durability": card.durability,
            "created": card.created,
        })

    if len(summaries) < 3:
        return {"examined": len(summaries), "written": 0}

    user_msg = (
        f"Memory cards ({len(summaries)} of {len(rows)} total):\n"
        + json.dumps(summaries, indent=2, ensure_ascii=False)
    )

    msg = memory._chat_with_model(
        model=config.memory_model(),
        messages=[
            {"role": "system", "content": CONSOLIDATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        json_mode=True,
    )
    try:
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return {"examined": len(summaries), "written": 0}

    cards = memory_extract.parse_cards(
        data,
        current_scope="global",  # synthesis cards always global
        origin_kind="consolidation",
    )
    written = memory.apply_cards(cards, gateway="cli")
    return {"examined": len(summaries), "written": len(written)}
