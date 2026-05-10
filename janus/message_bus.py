"""
message_bus.py — append-only inter-agent message bus
(v1.39.1, Phase 10.3.1).

WHY:
Sam's Layer A (Internal A2A) needs more than the blackboard
key-value store — agents also need to TELL each other things
("I finished file X", "the build broke", "I'm going to try Y").
A blackboard supports that via stomping a key, but you lose the
ordering and the "who said what" history.

This module ships the append-only message log alongside the
blackboard. Each run_id has a paired file:
  ~/.janus/blackboard/<run_id>.json            (key/value, v1.39.0)
  ~/.janus/blackboard/<run_id>.messages.jsonl  (this module)

DESIGN:
  * One JSONL line per message: {ts, from_agent, body, kind}
  * Append-only — never delete, never rewrite past entries.
  * No locking — single-machine assumption (per Sam, 2026-05-10).
  * Concurrent appenders: POSIX append-mode is atomic up to
    PIPE_BUF (~4KB), and our messages are typically smaller.
    Two agents writing simultaneously may interleave bytes only
    on multi-megabyte messages — accepted limitation.

API:
  send(run_id, body, *, from_agent=None, kind="msg") → Message
  recv(run_id, *, since=None, from_agent=None, limit=None) → list[Message]
  recv_since_ts(run_id, since_ts: float, ...)
  clear(run_id)
  Message.from_jsonl(line) / Message.to_jsonl()

USAGE FROM SUBAGENTS:
v1.39.1 ships this primitive + two model-callable tools
(``bus_send`` / ``bus_recv``) so subagents can coordinate via
the bus without the orchestrator threading state through arg
passing. The tools are added to the default registry.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import config


# ---------- Message dataclass ----------


@dataclass
class Message:
    ts: float                  # unix timestamp (seconds, float)
    body: Any                  # JSON-serializable payload (string, dict, list, …)
    from_agent: Optional[str] = None
    kind: str = "msg"          # "msg" | "status" | "error" — extensible

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_jsonl(cls, line: str) -> Optional["Message"]:
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "ts" not in data or "body" not in data:
            return None
        return cls(
            ts=float(data.get("ts") or 0.0),
            body=data.get("body"),
            from_agent=data.get("from_agent"),
            kind=str(data.get("kind") or "msg"),
        )


# ---------- paths ----------


def _root() -> "os.PathLike":
    d = config.HOME / "blackboard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_run_id(run_id: str) -> str:
    if not run_id or not run_id.strip():
        raise ValueError("run_id required")
    safe = run_id.strip()
    for ch in (":", "/", "\\"):
        safe = safe.replace(ch, "_")
    return safe


def path_for(run_id: str):
    """Return the messages.jsonl path for a run_id."""
    return _root() / f"{_safe_run_id(run_id)}.messages.jsonl"


# ---------- API ----------


def send(
    run_id: str,
    body: Any,
    *,
    from_agent: Optional[str] = None,
    kind: str = "msg",
) -> Message:
    """Append a message to the bus. Returns the Message that was
    written (with the timestamp set)."""
    if body is None:
        raise ValueError("body required")
    # Validate JSON-serializability up front
    try:
        json.dumps(body)
    except (TypeError, ValueError) as e:
        raise ValueError(f"body not JSON-serializable: {e}")
    if from_agent is not None and not isinstance(from_agent, str):
        raise ValueError("from_agent must be a string or None")

    msg = Message(
        ts=time.time(),
        body=body,
        from_agent=from_agent,
        kind=kind or "msg",
    )
    p = path_for(run_id)
    # Open in 'a' mode for atomic append. Python's text mode here is
    # OK because we write a single line including the trailing \n.
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(msg.to_jsonl() + "\n")
    return msg


def recv(
    run_id: str,
    *,
    since: Optional[float] = None,
    from_agent: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Message]:
    """Read messages from the bus.

    Filters:
      * since=<unix_ts>   only messages with ts > since
      * from_agent=<id>   only messages from that agent
      * limit=<int>       cap result count (most recent at end)

    Returns a list ordered by ts ascending (oldest first).
    """
    p = path_for(run_id)
    if not p.is_file():
        return []
    out: list[Message] = []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                m = Message.from_jsonl(line)
                if m is None:
                    continue
                if since is not None and m.ts <= since:
                    continue
                if from_agent is not None and m.from_agent != from_agent:
                    continue
                out.append(m)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def clear(run_id: str) -> None:
    """Drop the message log for run_id."""
    p = path_for(run_id)
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


def list_run_ids() -> list[str]:
    """Return sorted list of run_ids that have a message log on disk."""
    d = _root()
    out = []
    for p in d.glob("*.messages.jsonl"):
        # Stem includes ".messages" — strip it.
        name = p.name[: -len(".messages.jsonl")]
        out.append(name)
    return sorted(out)
