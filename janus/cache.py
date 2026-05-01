"""
cache.py — Phase 12: snapshot session-stable context once at boot.

WHY:
Both OpenAI and Anthropic prompt caches require the LEADING n tokens of
the request to be byte-identical across turns. Janus historically called
`memory.prepend_for_prompt()` every turn — same content, but a fresh
string allocation each call AND a fresh disk read of `user.md`. Even
when content matches, that pattern wastes the cache because:

(a) The first byte changes if `user.md` changes mid-session (e.g. after
    `memory.apply()` runs because the user approved a propose-diff).
(b) Some providers compare object identity / hash before content.

Snapshotting at session boot fixes (a). For (b), we also expose the same
string instance back to all callers so any provider doing identity
shortcuts wins.

USAGE:
    snap = cache.snapshot()
    # ... loop ...
    interps = interpreter.interpret(req, memory_preamble=snap.preamble, ...)
    # if the user approves a memory diff mid-session, REFRESH:
    if memory_was_changed:
        snap = cache.snapshot()

DESIGN INVARIANT:
This module is the ONLY thing that should hold the per-session preamble
across turns. Other modules read it as a parameter; they don't fetch it
themselves.
"""

from __future__ import annotations
from dataclasses import dataclass

from . import memory


@dataclass
class CacheSnapshot:
    preamble: str

    def __len__(self) -> int:
        return len(self.preamble)


def snapshot() -> CacheSnapshot:
    """Capture the current memory preamble. Call once at session boot.

    Cheap (one disk read + one truncation). Re-call after any operation
    that mutates `user.md` to keep the cache valid.
    """
    return CacheSnapshot(preamble=memory.prepend_for_prompt())
