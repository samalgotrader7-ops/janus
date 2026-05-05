"""
tools/memory_search.py — model-callable structured memory search (v1.18.0 Phase 6).

Pre-injection recall (memory_recall.top_k_block in executor.chat) gives
the model the most relevant cards automatically. This tool lets the
model ALSO ASK on demand mid-turn: "what do I know about X?".

Returns the same shape as the recall block (compact markdown bullets)
so the cognitive load is consistent — a card looks the same whether
auto-injected or fetched explicitly.

CAPABILITY: memory.search — read-class. Default-allow in all modes.
"""

from __future__ import annotations
from typing import Callable

from . import base
from .. import memory_recall, memory_index, memory_cards


_MAX_OUTPUT_BYTES = 2000
_DEFAULT_TOP_K = 5
_MAX_TOP_K = 20


class MemorySearch(base.Tool):
    name = "memory_search"
    description = (
        "Search structured memory cards by free-text query. Returns up "
        "to top_k cards as markdown bullets — same format as the "
        "auto-injected recall block. Use when the user asks 'what do "
        "you know about X' or you suspect a relevant fact was extracted "
        "in a prior turn. Empty result means no relevant cards exist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search; matches subject + content.",
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional filter to specific card types. Valid types: "
                    "identity, preference, goal, project, habit, decision, "
                    "constraint, relationship."
                ),
            },
            "scope": {
                "type": "string",
                "description": (
                    "Optional scope override ('global' / 'cli' / "
                    "'telegram:<chat_id>' / etc). Defaults to current origin."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max cards to return (default 5, max 20).",
            },
        },
        "required": ["query"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "error: query is required"
        types = args.get("types") or None
        if types is not None and not isinstance(types, list):
            return "error: types must be a list of strings"
        if isinstance(types, list):
            types = [str(t).strip() for t in types if t]
            invalid = [t for t in types if t not in memory_cards.TYPES]
            if invalid:
                return (
                    f"error: invalid types {invalid}; "
                    f"valid types are {list(memory_cards.TYPES)}"
                )
        scope = args.get("scope")
        scope = str(scope).strip() if scope else None
        try:
            top_k = int(args.get("top_k") or _DEFAULT_TOP_K)
        except (TypeError, ValueError):
            top_k = _DEFAULT_TOP_K
        top_k = max(1, min(_MAX_TOP_K, top_k))

        try:
            cards = memory_recall.top_k(
                query,
                current_scope=scope,
                top_k=top_k,
                budget_bytes=_MAX_OUTPUT_BYTES,
            )
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

        # Optional types filter — applied AFTER recall so BM25 still ranks
        # against the broader corpus (we don't want to bias the rerank
        # toward a narrow type subset).
        if types:
            cards = [c for c in cards if c["type"] in types]

        if not cards:
            return "(no matching memory cards)"

        # Bump recall_count for the cards actually returned to the model.
        try:
            memory_index.bump_recall([c["id"] for c in cards])
        except Exception:
            pass

        lines = ["## Matching memory cards", ""]
        for c in cards:
            lines.append(c["_line"])
            lines.append(
                f"  (id={c['id']} scope={c['scope']} "
                f"conf={c['confidence']:.1f} dur={c['durability']:.1f})"
            )
        out = "\n".join(lines)
        if len(out) > _MAX_OUTPUT_BYTES:
            out = out[: _MAX_OUTPUT_BYTES - 1] + "…"
        return out
