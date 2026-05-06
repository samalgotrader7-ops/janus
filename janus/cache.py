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

    v1.18: also runs the one-shot legacy → cards migration on first call
    (idempotent — checks a marker file). After migration, recall picks
    up the migrated cards alongside any new extractions.

    v1.19: also installs the bundled interview question library on
    first call (idempotent — checks `_bundled_installed` marker). The
    user can then run `/interview` immediately.
    """
    try:
        from . import memory_migrate
        memory_migrate.maybe_migrate()
    except Exception:
        # Migration failure must not block session boot.
        pass
    try:
        from . import interviews
        interviews.maybe_install_bundled()
    except Exception:
        # Same defensive guard — interview install must not block boot.
        pass
    return CacheSnapshot(preamble=memory.prepend_for_prompt())
