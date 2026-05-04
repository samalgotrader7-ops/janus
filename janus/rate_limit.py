"""
rate_limit.py — per-provider request/token tracker (v1.11.0).

WHY THIS EXISTS:
v1.4 added retry/backoff at the llm.chat boundary (3 attempts with
exponential jitter). That handles transient 5xx and a single 429. But
when you're running a long-lived agent (cron jobs, swarms), you want
visibility into the BUDGET at the provider level: how many requests
in the last minute, when did the last 429 land, are we approaching a
quota wall.

Hermes calls this `agent/rate_limit_tracker.py`. Janus ports the
pattern, lighter:

  - In-memory rolling window (60s default) of {timestamp, tokens}
    per (provider, model) bucket.
  - record_request(provider, model, tokens, ok=True) at every llm.chat
    completion (success OR 429).
  - get_summary() for the /stats slash command + janus stats CLI.
  - last_429_at per bucket lets the retry logic know "we just got
    rate-limited, sleep harder".

DESIGN — IN-MEMORY ONLY (with last_429_at on disk):
The rolling-window data is recreated every restart. That's fine —
rate limits are a recent-state concept. We persist ONLY the most
recent 429 timestamp per bucket (~/.janus/rate_limit_state.json) so
a fresh process knows it shouldn't hammer a provider that just
limited the previous process.

P5 (plain-text state): the persisted file is a plain JSON dict the
user can `cat` or `rm` to reset.

P7 (bounded everything): cap each bucket at 1000 entries; oldest are
discarded when over. Memory growth is constant.
"""

from __future__ import annotations
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import config


# Rolling window length (seconds). 60 is the right default — most
# provider rate limits are per-minute or per-second.
WINDOW_SECONDS = 60

# Hard cap per bucket so a hot path can't OOM us.
MAX_ENTRIES_PER_BUCKET = 1000


@dataclass
class _BucketStats:
    """In-memory rolling window for one (provider, model) pair."""
    requests: deque = field(default_factory=lambda: deque(maxlen=MAX_ENTRIES_PER_BUCKET))
    # Persistent: last 429 timestamp (epoch seconds). Survives restart
    # via _STATE_FILE so a fresh process backs off appropriately.
    last_429_at: float = 0.0
    # Counter — total 429s ever seen (resets each restart).
    total_429: int = 0
    # Counter — total successful requests this process.
    total_ok: int = 0


# Module-level state — keyed by "provider/model" string.
_BUCKETS: dict[str, _BucketStats] = {}
# Reentrant lock — get_summary() holds the lock while calling
# cooldown_seconds() which re-acquires it. A plain Lock would deadlock.
_LOCK = threading.RLock()


def _key(provider: str, model: str) -> str:
    return f"{(provider or 'unknown').strip()}/{(model or 'unknown').strip()}"


def _state_path():
    return config.HOME / "rate_limit_state.json"


# ---------- Public API ----------


def record_request(
    *, provider: str, model: str,
    tokens: int = 0, ok: bool = True,
    status_429: bool = False,
) -> None:
    """Record one request. Called by llm.chat after every completion.

    `ok` False means the call failed for any reason. `status_429` True
    flips the 'last 429' bookkeeping that retry logic reads. We don't
    couple this module to the HTTP layer — caller passes status hints.
    """
    with _LOCK:
        b = _BUCKETS.setdefault(_key(provider, model), _BucketStats())
        now = time.time()
        b.requests.append((now, int(tokens), bool(ok)))
        if ok:
            b.total_ok += 1
        if status_429:
            b.last_429_at = now
            b.total_429 += 1
            _persist_state()


def cooldown_seconds(provider: str, model: str) -> float:
    """Hint to the retry path: how long should we wait before the next
    request because the bucket just got 429'd?

    Logic: 30s for the first 30s after a 429, 0 thereafter. Cheap and
    predictable. Hermes uses a more sophisticated model with provider-
    specific reset headers; we'll add that when we hit a real wall.
    """
    with _LOCK:
        b = _BUCKETS.get(_key(provider, model))
        if not b or not b.last_429_at:
            return 0.0
        elapsed = time.time() - b.last_429_at
        if elapsed >= 30:
            return 0.0
        return 30 - elapsed


def get_summary() -> dict[str, Any]:
    """Snapshot every bucket. Returns a dict suitable for printing
    or feeding to render_summary()."""
    with _LOCK:
        out: dict[str, Any] = {}
        cutoff = time.time() - WINDOW_SECONDS
        for key, b in _BUCKETS.items():
            recent = [(t, tok, ok) for (t, tok, ok) in b.requests if t >= cutoff]
            tokens_in_window = sum(tok for (_, tok, _) in recent)
            errors_in_window = sum(1 for (_, _, ok) in recent if not ok)
            out[key] = {
                "requests_in_window": len(recent),
                "tokens_in_window": tokens_in_window,
                "errors_in_window": errors_in_window,
                "total_ok": b.total_ok,
                "total_429": b.total_429,
                "cooldown_remaining": round(cooldown_seconds(*key.split("/", 1)), 1),
            }
        return out


def render_summary(stats: dict[str, Any]) -> str:
    """Format get_summary() output as a markdown table for `/stats`."""
    if not stats:
        return "no rate-limit data yet (no requests recorded this process)"
    lines = [
        "## Rate limits (rolling 60s window)",
        "",
        "| provider/model | reqs | tokens | errors | total ok | total 429 | cooldown |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, s in sorted(stats.items()):
        cooldown = f"{s['cooldown_remaining']}s" if s['cooldown_remaining'] else "—"
        lines.append(
            f"| {key} | {s['requests_in_window']} | {s['tokens_in_window']} | "
            f"{s['errors_in_window']} | {s['total_ok']} | {s['total_429']} | "
            f"{cooldown} |"
        )
    return "\n".join(lines)


def reset() -> None:
    """Clear all buckets + remove persistent state. Useful for tests."""
    with _LOCK:
        _BUCKETS.clear()
    try:
        p = _state_path()
        if p.exists():
            p.unlink()
    except OSError:
        pass


# ---------- Persistence ----------


def _persist_state() -> None:
    """Write last_429_at per bucket to disk. Called when a 429 lands."""
    try:
        config.ensure_home()
        data = {
            key: {"last_429_at": b.last_429_at, "total_429": b.total_429}
            for key, b in _BUCKETS.items() if b.last_429_at
        }
        _state_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_state() -> None:
    """Restore last_429_at from disk on first import."""
    p = _state_path()
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    with _LOCK:
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            b = _BUCKETS.setdefault(key, _BucketStats())
            try:
                b.last_429_at = float(entry.get("last_429_at") or 0.0)
                b.total_429 = int(entry.get("total_429") or 0)
            except (TypeError, ValueError):
                continue


# Auto-load on import. Failure-silent (P8).
try:
    _load_state()
except Exception:
    pass
