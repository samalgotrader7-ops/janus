"""
trajectory.py — full chat-loop recording for replay + future RL (v1.13.0).

WHY THIS EXISTS:
~/.janus/log.jsonl captures one entry per TURN — request, output,
trace summary, ts. Useful for grep but not for replay: it doesn't
include the full sequence of (system prompt, messages, tool calls,
tool results, model deltas) that the executor saw at each step.

For:
  - debugging "why did the model do X?" with full context reproduction
  - regression detection (replay yesterday's prompt at temp=0; assert
    the same outcome under today's code)
  - future RL training (Atropos-style — Hermes uses these recordings
    as offline training data)

…we need the FULL trajectory: what messages went into each LLM call,
what came back, what tool args were dispatched, what tool results
returned. v1.13 ports Hermes' agent/trajectory.py recorder.

LAYOUT:
  ~/.janus/trajectories/
    <conv_id>/
      <ts>__<seq>.jsonl    # one file per turn

Each file is a sequence of events:
  {"type": "system", "content": "..."}
  {"type": "user", "content": "..."}
  {"type": "assistant_partial", "content": "..."}     (streaming chunk)
  {"type": "tool_call", "name": "...", "args": {...}}
  {"type": "tool_result", "name": "...", "result": "..."}
  {"type": "assistant_final", "content": "..."}
  {"type": "metadata", "model": "...", "mode": "...", "elapsed_ms": ...}

P5 (plain-text state): JSONL per turn. cat-able, jq-able, replayable
without our code.

P7 (bounded everything): JANUS_TRAJECTORY env gates recording.
Default OFF — recording every turn would be ~5-50 KB extra per turn,
which adds up for someone running 1000s of turns. User opts in via
JANUS_TRAJECTORY=1 / on / true.

Trajectories are PII-redacted via janus.redact (matches log.jsonl
behavior). Even though trajectories are local, the user might
share them for support or training data — redaction prevents
accidental leak.
"""

from __future__ import annotations
import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import config


# Module-level state — current trajectory writer per thread.
_LOCAL = threading.local()


def is_enabled() -> bool:
    """Read JANUS_TRAJECTORY env. Default off — recording is opt-in."""
    v = os.getenv("JANUS_TRAJECTORY", "0").lower().strip()
    return v in ("1", "true", "on", "yes")


def _ts_for_filename() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(":", "-")


def _trajectories_dir() -> Path:
    return config.HOME / "trajectories"


# ---------- Public API ----------


class TrajectoryWriter:
    """One open file per active turn. Writes one JSON line per event.

    Use as a context manager OR explicitly via close(). Thread-local
    `_LOCAL.writer` makes record(...) below work without passing the
    writer through every layer.
    """

    def __init__(self, conv_id: str, *, seq: int | None = None):
        self.conv_id = conv_id or "no-conv"
        self.path = _path_for(self.conv_id, seq=seq)
        self._fh = None
        self._closed = False

    def __enter__(self) -> "TrajectoryWriter":
        self.open()
        _LOCAL.writer = self
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if getattr(_LOCAL, "writer", None) is self:
            _LOCAL.writer = None
        self.close()

    def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError:
            self._fh = None  # silent — recording is best-effort

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._fh:
                self._fh.flush()
                self._fh.close()
        except OSError:
            pass
        self._closed = True

    def write_event(self, event: dict[str, Any]) -> None:
        if not self._fh or self._closed:
            return
        try:
            # Redact secrets at write time so trajectories are safe to share.
            from . import redact
            scrubbed = redact.redact_obj(event)
            self._fh.write(json.dumps(scrubbed, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception:
            # Failure-silent (P8) — never crash the chat loop on a
            # recording bug. The user can debug the recorder later.
            pass


def record(event: dict[str, Any]) -> None:
    """Write an event to the active trajectory if one is open.

    Used by executor + tools to log without each layer needing to know
    about TrajectoryWriter directly. No-op when no writer is active
    (most of the time, since recording is opt-in).
    """
    w = getattr(_LOCAL, "writer", None)
    if w is None:
        return
    if not isinstance(event, dict):
        return
    # Stamp every event so replay can reorder if the file gets shuffled.
    event.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    w.write_event(event)


def open_trajectory(conv_id: str, *, seq: int | None = None) -> TrajectoryWriter | None:
    """Open a writer for one turn. Returns None when recording is off
    so the caller can short-circuit without `if is_enabled()` checks."""
    if not is_enabled():
        return None
    return TrajectoryWriter(conv_id, seq=seq)


def _path_for(conv_id: str, *, seq: int | None) -> Path:
    safe_id = "".join(c for c in conv_id if c.isalnum() or c in "-_") or "default"
    base = _trajectories_dir() / safe_id
    if seq is not None:
        return base / f"{_ts_for_filename()}__{seq:04d}.jsonl"
    return base / f"{_ts_for_filename()}.jsonl"


# ---------- Listing / replay helpers ----------


def list_trajectories(*, conv_id: str | None = None) -> list[dict[str, Any]]:
    """Return [{conv_id, file, ts, size}, ...] sorted newest-first."""
    root = _trajectories_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for conv_dir in root.iterdir():
        if not conv_dir.is_dir():
            continue
        if conv_id and conv_dir.name != conv_id:
            continue
        for f in conv_dir.glob("*.jsonl"):
            try:
                stat = f.stat()
            except OSError:
                continue
            out.append({
                "conv_id": conv_dir.name,
                "file": str(f),
                "ts": f.stem,
                "size": stat.st_size,
            })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def read_trajectory(path: str | Path) -> list[dict[str, Any]]:
    """Read a trajectory file back into a list of events.

    Skips malformed lines silently (the file might have been clipped
    by an OOM kill mid-write). Returns events in file order.
    """
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out
