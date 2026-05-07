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


# ---------- Multi-stage / "swarm-shaped" consolidation (v1.29.0) ----------
#
# The single-LLM-call ``run_once`` works for small memories but
# struggles with large stores: too much input to one call, and the
# model has to find patterns within AND across types in one shot.
#
# v1.29.0 adds a multi-stage variant:
#
#   Stage 1 (parallel): one LLM call per card type (identity,
#   preference, project, etc.) that finds WITHIN-TYPE patterns
#   only. Up to 8 calls fire concurrently via ThreadPoolExecutor.
#
#   Stage 2 (single): one LLM call takes Stage 1's pattern lists
#   and writes the final 0-5 synthesis cards.
#
# Benefits: each Stage 1 call sees less input → better focus and
# cheaper per-call. Stage 2 sees PATTERNS (compact) not raw cards
# → small input even with 1000+ cards. Total token spend is
# typically lower than a single shot for large stores.
#
# Cost: more LLM calls. 1 store-wide call → 9 (8 types + 1
# synthesis). For tiny stores the multi-stage path is overkill;
# ``run_once`` remains the default.
#
# Opt-in via ``/memory consolidate --multi-stage`` or env var
# ``JANUS_CONSOLIDATE_STRATEGY=multi_stage``.


STAGE1_SYSTEM = """You analyze memory cards of ONE type for within-type patterns.

INPUT: a list of cards, all of the same type (e.g. all "habit" cards).

TASK: find patterns ACROSS multiple cards in this type. Examples:
  - "5 habit cards all describe morning routines" → pattern about
    morning-time agency
  - "3 project cards all reference the same teammate" → pattern
    about that teammate's role

DON'T: produce cards. Just describe patterns in 1-3 sentences each.
DON'T: guess at synthesis if no clear pattern exists. Empty output
       is the right answer most of the time.

Return STRICT JSON:
  {"patterns": ["short description 1", "short description 2", ...]}

Return {"patterns": []} if nothing meaningful emerges. Cap at 5
patterns even if you see more — the synthesizer prefers focused
input.
"""


STAGE2_SYSTEM = """You synthesize cross-type insights from per-type pattern lists.

INPUT: a JSON object mapping card type → list of within-type patterns
identified in stage 1. Some types may have empty lists (no patterns
found).

TASK: identify CROSS-TYPE syntheses — insights that follow from
multiple types together. Example:
  - "morning-routine habits + early-bird identity card +
    project-deadline goal" → "Sam optimizes for morning deep-work
    blocks before standup-noise hits"

Output 0-5 synthesis cards using the same shape as standalone
consolidation. Set durability HIGH (0.7-1.0), scope=global,
origin_kind handled by the caller.

Return STRICT JSON:
  {"cards": [{"type": "...", "subject": "...", "content": "...",
              "confidence": 0.x, "importance": 0.x,
              "durability": 0.x, "scope": "global",
              "conflict_with": null,
              "conflict_resolution": "append"}]}

Most passes return {"cards": []} — that's fine. Don't fabricate.
"""


def _extract_patterns_for_type(card_type: str, cards: list[dict]) -> list[str]:
    """Stage 1: find within-type patterns. Returns a list of short
    pattern descriptions (1-3 sentences each).

    Empty list on LLM error / empty result — never raises.
    """
    if not cards:
        return []
    user_msg = (
        f"Card type: {card_type}\n"
        f"Cards ({len(cards)}):\n"
        + json.dumps(cards, indent=2, ensure_ascii=False)
    )
    try:
        msg = memory._chat_with_model(
            model=config.memory_model(),
            messages=[
                {"role": "system", "content": STAGE1_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            json_mode=True,
        )
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return []
    patterns = data.get("patterns") if isinstance(data, dict) else None
    if not isinstance(patterns, list):
        return []
    out: list[str] = []
    for p in patterns:
        if isinstance(p, str) and p.strip():
            out.append(p.strip()[:400])  # cap line length
    return out[:5]


def run_multi_stage(*, max_input_cards: int = 200) -> dict:
    """Multi-stage consolidation: per-type pattern extraction in
    parallel → cross-type synthesis.

    Returns ``{"examined": int, "written": int, "stages": 2,
    "patterns_per_type": dict}`` so callers can show the structure
    that produced the final cards.

    Always at least 2 stages (8 parallel + 1 synthesis).
    """
    try:
        memory_index.reconcile()
    except Exception:
        pass

    rows = memory_index.list_all()
    if len(rows) < 3:
        return {"examined": len(rows), "written": 0, "stages": 0,
                "patterns_per_type": {}}

    # Group cards by type. Each group is a stage-1 input.
    by_type: dict[str, list[dict]] = {}
    for r in rows[:max_input_cards]:
        try:
            card = memory_cards.read_card(Path(r["path"]))
        except Exception:
            continue
        ctype = str(card.type)
        bucket = by_type.setdefault(ctype, [])
        bucket.append({
            "id": card.id,
            "subject": card.subject,
            "content": card.content[:200],
            "confidence": card.confidence,
            "durability": card.durability,
            "created": card.created,
        })

    examined = sum(len(v) for v in by_type.values())
    if examined < 3:
        return {"examined": examined, "written": 0, "stages": 0,
                "patterns_per_type": {}}

    # ---- Stage 1: parallel per-type pattern extraction ----
    import concurrent.futures as _cf
    patterns_per_type: dict[str, list[str]] = {}
    nonempty_types = [(t, cards) for t, cards in by_type.items() if cards]
    # Cap workers — 8 types max in v1.18 schema, but be defensive.
    max_workers = max(1, min(len(nonempty_types), 8))
    with _cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {
            pool.submit(_extract_patterns_for_type, t, cards): t
            for t, cards in nonempty_types
        }
        for fut in _cf.as_completed(futs):
            t = futs[fut]
            try:
                patterns_per_type[t] = fut.result()
            except Exception:
                patterns_per_type[t] = []

    # If no patterns surfaced anywhere, skip stage 2 (no point
    # asking the synthesizer).
    has_any = any(v for v in patterns_per_type.values())
    if not has_any:
        return {
            "examined": examined,
            "written": 0,
            "stages": 1,
            "patterns_per_type": patterns_per_type,
        }

    # ---- Stage 2: cross-type synthesis ----
    user_msg = (
        "Per-type patterns from stage 1:\n"
        + json.dumps(patterns_per_type, indent=2, ensure_ascii=False)
    )
    try:
        msg = memory._chat_with_model(
            model=config.memory_model(),
            messages=[
                {"role": "system", "content": STAGE2_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            json_mode=True,
        )
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception:
        return {
            "examined": examined,
            "written": 0,
            "stages": 2,
            "patterns_per_type": patterns_per_type,
        }

    cards = memory_extract.parse_cards(
        data,
        current_scope="global",
        origin_kind="consolidation",
    )
    written = memory.apply_cards(cards, gateway="cli")
    return {
        "examined": examined,
        "written": len(written),
        "stages": 2,
        "patterns_per_type": patterns_per_type,
    }
