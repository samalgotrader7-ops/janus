"""
permissions.py — v1.0 permission modes (Claude-Code-shaped).

Replaces the older "manual / auto / dry-run" approval posture from the
interpretation-first era. The new shape is one of four modes that gate
tool execution by RISK CLASS, not by per-tool y/N theater every turn.

Modes (names mirror Claude Code so muscle memory transfers):

  default          read → allow,  write → ask, exec → ask
  acceptEdits      read → allow,  write → allow, exec → ask
  bypassPermissions  everything → allow (with startup warning)
  plan             read → allow,  write → deny,  exec → deny

WHY RISK CLASSES, NOT PER-TOOL FLAGS:
The old `dangerous: bool` was binary — a tool either always asked or
never did. That conflates "writes a file" (recoverable, the user can
diff) with "executes shell" (arbitrary side effects on the host). With
three risk classes the user can grant write trust without granting
shell trust, which is the exact ergonomic Claude Code's `acceptEdits`
mode delivers.

Capability tokens (from skills) are still honored as a backstop — if a
skill grants `shell.exec: ['git *']`, that grant short-circuits the
mode check via `make_capability_aware` in tools/base.py. So a skill
can widen permissions for a narrow target without flipping the whole
session into bypassPermissions.
"""

from __future__ import annotations
from dataclasses import dataclass, field


# ---------- Mode names ----------

DEFAULT: str = "default"
ACCEPT_EDITS: str = "acceptEdits"
BYPASS: str = "bypassPermissions"
PLAN: str = "plan"
# v1.5: auto mode = bypassPermissions + heuristic risk analyzer.
# Auto-approves like bypass but blocks dangerous tool calls based on
# arg patterns (rm -rf /, fs writes to /etc/, fetches to localhost SSRF,
# etc.). Ideal for long-running unattended swarms where the user can't
# approve each call but a runaway destructive operation would be costly.
AUTO: str = "auto"

ALL_MODES: tuple[str, ...] = (DEFAULT, ACCEPT_EDITS, BYPASS, PLAN, AUTO)

# The order /mode (no arg) and Shift+Tab cycle through. Auto sits next
# to bypass — both are "I trust the agent" modes; auto is the safer one.
CYCLE_ORDER: tuple[str, ...] = (DEFAULT, ACCEPT_EDITS, PLAN, AUTO, BYPASS)


# ---------- Risk classes ----------

RISK_READ: str = "read"
RISK_WRITE: str = "write"
RISK_EXEC: str = "exec"

ALL_RISKS: tuple[str, ...] = (RISK_READ, RISK_WRITE, RISK_EXEC)


# ---------- Decision matrix ----------

ALLOW: str = "allow"
ASK: str = "ask"
DENY: str = "deny"


_MATRIX: dict[str, dict[str, str]] = {
    DEFAULT: {
        RISK_READ:  ALLOW,
        RISK_WRITE: ASK,
        RISK_EXEC:  ASK,
    },
    ACCEPT_EDITS: {
        RISK_READ:  ALLOW,
        RISK_WRITE: ALLOW,
        RISK_EXEC:  ASK,
    },
    BYPASS: {
        RISK_READ:  ALLOW,
        RISK_WRITE: ALLOW,
        RISK_EXEC:  ALLOW,
    },
    PLAN: {
        RISK_READ:  ALLOW,
        RISK_WRITE: DENY,
        RISK_EXEC:  DENY,
    },
    # v1.5 auto: matrix says "allow" baseline; the make_auto_aware
    # wrapper applies risk analysis on top and can flip allow → deny
    # per individual call based on tool args (e.g., rm -rf / blocked
    # even though the matrix says exec=allow).
    AUTO: {
        RISK_READ:  ALLOW,
        RISK_WRITE: ALLOW,
        RISK_EXEC:  ALLOW,
    },
}


def decide(risk: str, mode: str) -> str:
    """Return ALLOW | ASK | DENY for (risk, mode).

    Unknown risk → treat as exec (most restrictive). Unknown mode →
    treat as default. Both fail closed so a typo in config can never
    silently widen permissions.
    """
    risk = risk if risk in ALL_RISKS else RISK_EXEC
    mode = mode if mode in ALL_MODES else DEFAULT
    return _MATRIX[mode][risk]


def cycle_next(current: str) -> str:
    """Next mode in the Shift+Tab cycle. Wraps."""
    if current not in CYCLE_ORDER:
        return CYCLE_ORDER[0]
    i = CYCLE_ORDER.index(current)
    return CYCLE_ORDER[(i + 1) % len(CYCLE_ORDER)]


# ---------- Compatibility shim for old approval modes ----------
#
# Pre-v1.0 config used JANUS_APPROVAL = "manual" | "auto" | "dry-run".
# Map them so users with old .env files don't crash.
#
# v1.5: the legacy `auto → bypassPermissions` mapping is REMOVED because
# `auto` is now a real (and strictly safer) mode in its own right —
# bypass blindly allows everything; auto allows-with-risk-analysis.
# Old configs that said "auto" get the new safer behavior automatically.

_LEGACY_MAP: dict[str, str] = {
    "manual":  DEFAULT,
    "dry-run": PLAN,
}


def normalize(raw: str | None) -> str:
    """Coerce any string to a valid mode name."""
    if not raw:
        return DEFAULT
    raw = str(raw).strip()
    if raw in ALL_MODES:
        return raw
    return _LEGACY_MAP.get(raw, DEFAULT)


# ---------- Verb → risk fallback ----------
#
# Tools call approver(..., capability=(tool, verb, target)). When the
# new `risk=` kwarg isn't passed (legacy callers, MCP tools, hooks),
# the verb is the next-best signal.

_VERB_RISK: dict[str, str] = {
    "read":   RISK_READ,
    "list":   RISK_READ,
    "search": RISK_READ,
    "fetch":  RISK_READ,
    "glob":   RISK_READ,
    "grep":   RISK_READ,
    "write":  RISK_WRITE,
    "edit":   RISK_WRITE,
    "create": RISK_WRITE,
    "exec":   RISK_EXEC,
    "run":    RISK_EXEC,
    "navigate": RISK_EXEC,
}


def risk_from_verb(verb: str) -> str:
    """Best-effort risk inference. Falls back to exec (fail closed)."""
    return _VERB_RISK.get(verb, RISK_EXEC)


# ---------- Active-mode container ----------


@dataclass
class ModeState:
    """Holds the active mode for a session. Mutable so /mode can swap it.

    v1.24.0 — added per-session approval whitelist for tools the user
    has approved with "session" semantics. Approver auto-passes when
    the (tool_name, risk) pair is in `session_grants`.

    v1.24.1 — added persistent "always" grants stored in
    ~/.janus/approvals.json. ModeState loads them lazily on first
    has_grant() call and writes them on grant_persistent().
    """
    current: str = DEFAULT
    session_grants: set = field(default_factory=set)
    _persistent_loaded: bool = False
    _persistent_grants: set = field(default_factory=set)

    def set(self, mode: str) -> str:
        self.current = normalize(mode)
        return self.current

    def cycle(self) -> str:
        self.current = cycle_next(self.current)
        return self.current

    def label(self) -> str:
        """Human-readable label for the statusline."""
        return self.current

    def grant(self, key: tuple) -> None:
        """v1.24.0: add a session-level approval grant.

        `key` is typically (tool_name, risk). Future approver calls
        with the same key auto-approve (no prompt). Cleared on session
        end.
        """
        self.session_grants.add(key)

    def grant_persistent(self, key: tuple) -> None:
        """v1.24.1: add a persistent grant — survives janus restart.

        Writes to ~/.janus/approvals.json. Also sets the in-memory
        bit so the current session sees it immediately.
        """
        self._ensure_loaded()
        self._persistent_grants.add(key)
        # In-memory copy so the rest of the session sees it via has_grant.
        self.session_grants.add(key)
        _save_persistent_grants(self._persistent_grants)

    def revoke_persistent(self, key: tuple) -> None:
        """v1.24.1: remove a persistent grant. Useful for `/grants clear`."""
        self._ensure_loaded()
        self._persistent_grants.discard(key)
        self.session_grants.discard(key)
        _save_persistent_grants(self._persistent_grants)

    def has_grant(self, key: tuple) -> bool:
        self._ensure_loaded()
        return key in self.session_grants or key in self._persistent_grants

    def list_grants(self) -> tuple[set, set]:
        """v1.24.1: return (session, persistent) grant sets for inspection."""
        self._ensure_loaded()
        return (set(self.session_grants), set(self._persistent_grants))

    def clear_grants(self) -> None:
        """Clear in-memory session grants only. Persistent grants stay."""
        self.session_grants.clear()

    def clear_persistent(self) -> None:
        """v1.24.1: wipe all persistent grants. Drops the file."""
        self._persistent_grants.clear()
        # Also drop the in-memory copy of any that were marked persistent.
        _save_persistent_grants(set())

    def _ensure_loaded(self) -> None:
        if self._persistent_loaded:
            return
        self._persistent_loaded = True
        try:
            self._persistent_grants = _load_persistent_grants()
        except Exception:
            self._persistent_grants = set()


# ---------- persistent grant storage (v1.24.1) ----------


def _grants_file_path():
    """Path to the on-disk grants file. Late import of config to avoid
    circular imports during bootstrap."""
    from . import config
    return config.HOME / "approvals.json"


def _load_persistent_grants() -> set:
    """Read ~/.janus/approvals.json. Returns a set of (tool, risk) tuples."""
    p = _grants_file_path()
    if not p.is_file():
        return set()
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out: set = set()
    for entry in data.get("grants", []):
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool", "")
        risk = entry.get("risk", "")
        if tool and risk:
            out.add((str(tool), str(risk)))
    return out


def _save_persistent_grants(grants: set) -> None:
    """Atomic write to ~/.janus/approvals.json with mode 0600."""
    import json as _json
    import os as _os
    p = _grants_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "grants": [
            {"tool": t, "risk": r}
            for (t, r) in sorted(grants)
        ],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
    try:
        _os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
    try:
        _os.chmod(p, 0o600)
    except OSError:
        pass
