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

    v1.24.0 — also stores a per-session approval whitelist for tools the
    user has approved with "always" / "session" semantics. The approver
    consults this before showing a prompt; if the (tool_name, risk) pair
    is in `session_grants`, ALLOW is returned without prompting.
    """
    current: str = DEFAULT
    session_grants: set = field(default_factory=set)

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
        with the same key auto-approve (no prompt).
        """
        self.session_grants.add(key)

    def has_grant(self, key: tuple) -> bool:
        return key in self.session_grants

    def clear_grants(self) -> None:
        self.session_grants.clear()
