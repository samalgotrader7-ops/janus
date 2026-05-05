"""
memory_recall.py — top-K card retrieval (v1.18.0 Phase 3+4).

PIPELINE (per user turn, pre-LLM):
  1. FTS5 query against subject + content (memory_index.query_fts)
  2. Re-rank: bm25_score * exp(-age_days / 30)  (recency decay)
  3. Filter by scope: global OR matches current origin OR project-CWD-descendant
  4. Phantom guard: Path.exists() check on each candidate
  5. Take top-K, fit to budget_bytes (truncate body to ~180 chars/card)
  6. Render as ``## Relevant memories`` block

LOG EVERY RECALL:
Each invocation that returns ≥1 card writes one row to
``recalls.jsonl`` with ``{ts, query, scope, card_ids}`` for the
``/memory stats`` ROI dashboard. Bumping ``stats.recall_count`` is a
side-effect of ``top_k_block`` — pre-injection observability.

WHY RECONCILE LAZILY:
The cache reconciles on first call per process (cheap when warm).
Phase 5 extraction inline-updates the index when writing new cards,
so the index doesn't grow stale within a session. ``/memory reindex``
covers the rare case where someone manually edits cards/ on disk.
"""

from __future__ import annotations
import datetime as _dt
import json
import math
from pathlib import Path

from . import config, memory_index, session_context


_RECONCILED_THIS_PROCESS = False


def _ensure_reconciled() -> None:
    """Reconcile once per process. Subsequent calls are no-op."""
    global _RECONCILED_THIS_PROCESS
    if _RECONCILED_THIS_PROCESS:
        return
    try:
        memory_index.reconcile()
    except Exception:
        # Reconcile failure must NOT kill the chat loop. Worst case:
        # recall returns nothing and the user sees no recall block.
        pass
    _RECONCILED_THIS_PROCESS = True


def _parse_iso_z(s: str) -> _dt.datetime:
    """Parse ``2026-05-05T14:30:00Z`` as a UTC-aware datetime.

    Falls back to ``now()`` when the input is malformed — better to
    treat unparseable timestamps as fresh than to crash.
    """
    s = (s or "").rstrip("Z")
    try:
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return _dt.datetime.now(_dt.timezone.utc)


def _recency_decay(created: str, *, half_life_days: float = 30.0,
                   now: _dt.datetime | None = None) -> float:
    """``exp(-age_days / half_life_days)``. Today: ~1.0; 60d ago: ~0.14."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    age_days = max(0.0, (now - _parse_iso_z(created)).total_seconds() / 86400)
    return math.exp(-age_days / half_life_days)


def _truncate(s: str, n: int) -> str:
    """Single-line truncate with ellipsis. Newlines collapse to spaces."""
    s = (s or "").strip().replace("\n", " ").replace("\r", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def top_k(
    query: str,
    *,
    current_scope: str | None = None,
    cwd: Path | None = None,
    top_k: int = None,  # noqa: shadows top_k param of recursive call by design
    budget_bytes: int = None,
) -> list[dict]:
    """Return up to ``top_k`` re-ranked card dicts within budget.

    Each dict is the metadata dict from ``memory_index.query_fts`` plus
    a synthesized ``_line`` field (the rendered single-line markdown).
    """
    if not query.strip():
        return []
    if top_k is None:
        top_k = getattr(config, "MEMORY_RECALL_TOP_K", 5)
    if budget_bytes is None:
        budget_bytes = getattr(config, "MEMORY_RECALL_BUDGET_BYTES", 900)

    _ensure_reconciled()
    candidates = memory_index.query_fts(query, limit=30)
    if not candidates:
        return []

    if current_scope is None:
        current_scope = session_context.current_scope()

    now = _dt.datetime.now(_dt.timezone.utc)
    for c in candidates:
        c["_rerank"] = c["score"] * _recency_decay(c["created"], now=now)
    candidates.sort(key=lambda c: c["_rerank"], reverse=True)

    selected: list[dict] = []
    bytes_used = 0
    for c in candidates:
        if len(selected) >= top_k:
            break
        # Phantom guard: card was in DB but file deleted between
        # reconcile and now. Skip silently — DB will catch up next reconcile.
        if not Path(c["path"]).exists():
            continue
        # Privacy filter
        if not session_context.scope_matches(c["scope"], current_scope, cwd=cwd):
            continue
        truncated = _truncate(c["content"], 180)
        line = f"- [{c['type']}:{c['subject']}] {truncated}"
        # Budget check: never blow the budget, but always include at least
        # the highest-ranked card if it'd fit on its own.
        if bytes_used + len(line) > budget_bytes and selected:
            break
        selected.append({**c, "_truncated": truncated, "_line": line})
        bytes_used += len(line) + 1  # +1 for newline join

    return selected


def top_k_block(
    query: str,
    *,
    current_scope: str | None = None,
    cwd: Path | None = None,
    top_k_value: int | None = None,
    budget_bytes: int | None = None,
) -> str:
    """Render the top-K recall as a markdown block.

    Returns the empty string when nothing matches (caller drops the block
    entirely rather than emitting an empty header).

    Side effects: appends to ``recalls.jsonl`` and bumps ``stats.recall_count``.
    """
    cards = top_k(
        query,
        current_scope=current_scope,
        cwd=cwd,
        top_k=top_k_value,
        budget_bytes=budget_bytes,
    )
    if not cards:
        return ""
    _log_recall(query, cards, current_scope or session_context.current_scope())
    try:
        memory_index.bump_recall([c["id"] for c in cards])
    except Exception:
        pass
    body = "\n".join(c["_line"] for c in cards)
    return f"## Relevant memories\n\n{body}\n"


def _log_recall(query: str, cards: list[dict], scope: str) -> None:
    """Append one row to ``~/.janus/memory/recalls.jsonl`` for /memory stats."""
    log_path = config.MEMORY_DIR / "recalls.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "query": (query or "")[:500],
            "scope": scope,
            "card_ids": [c["id"] for c in cards],
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        # Logging is best-effort; don't break recall on disk full / readonly.
        pass


def reset_reconcile_flag() -> None:
    """Test hook — force the next top_k call to re-reconcile."""
    global _RECONCILED_THIS_PROCESS
    _RECONCILED_THIS_PROCESS = False
