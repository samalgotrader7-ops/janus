"""
tools/session_search.py — Phase 9: agent-facing search over log.jsonl.

Wraps janus.index. The CLI already exposes /search; this exposes the
same to the model so an agent can introspect prior turns mid-task.
"""

from __future__ import annotations
from typing import Callable

from . import base
from .. import index


class SessionSearch(base.Tool):
    name = "session_search"
    description = (
        "Search prior interactions (FTS5 over log.jsonl). Returns up to "
        "10 hits, each with timestamp + request preview + tools used."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "FTS query (Porter+unicode tokenizer)."},
            "limit": {"type": "integer", "description": "Max hits (default 10, max 25)."},
        },
        "required": ["query"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return "error: query is required"
        k = min(int(args.get("limit") or 10), 25)
        try:
            index.sync()
            hits = index.search(query, k=k)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
        if not hits:
            return "(no matches)"
        out = []
        for h in hits:
            line = f"[{h.ts[:19]}] {h.request[:80]}"
            out.append(line)
            if h.tools_used:
                out.append(f"  tools: {h.tools_used}")
        return "\n".join(out)


class SessionRecent(base.Tool):
    name = "session_recent"
    description = (
        "List the most recent interactions (newest first). Default 10, max 25."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Number of records (default 10, max 25)."},
        },
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        k = min(int(args.get("limit") or 10), 25)
        try:
            index.sync()
            hits = index.recent(k=k)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
        if not hits:
            return "(no recent records)"
        return "\n".join(f"[{h.ts[:19]}] {h.request[:80]}" for h in hits)
