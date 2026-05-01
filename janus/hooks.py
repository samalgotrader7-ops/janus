"""
hooks.py — Phase 11: lifecycle hooks (PreToolUse, PostToolUse, etc.)

PROTOCOL:
A hook is a JSON spec naming an event, an optional matcher (regex against
an event-specific field — `tool` for PreToolUse/PostToolUse), and a shell
command. The command receives the event payload as JSON on stdin and
returns a decision JSON on stdout.

Decisions follow the Claude Code convention so hooks are portable:

    {"decision": "allow"}                 # default; tool runs as-is
    {"decision": "deny", "reason": "…"}   # block, return reason to model
    {"decision": "modify", "modified_args": {...}}   # rewrite args
    {"injected_context": "..."}           # add context to the model

Exit code 1 also denies (for hooks written as shell one-liners that don't
emit JSON). Any other exit code with non-JSON stdout is treated as allow
with a logged warning.

EVENTS:
- SessionStart, SessionEnd
- UserPromptSubmit, Interpret
- PreToolUse, PostToolUse
- Stop, StopFailure

CONFIG:
- ~/.janus/hooks.json (single file, {"hooks": {<event>: [hook, ...]}})
- ~/.janus/hooks/*.json (one or more files, each is either a single hook
  with "event" field or a {<event>: [...]} block)

NO-HOOK COST:
When no hooks are configured, `fire()` short-circuits to a permissive
HookDecision in O(1). Existing tests pass unchanged.
"""

from __future__ import annotations
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config


# ---------- Event names (constants for cross-module reference) ----------


SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
INTERPRET = "Interpret"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
STOP_FAILURE = "StopFailure"

ALL_EVENTS = (
    SESSION_START, SESSION_END,
    USER_PROMPT_SUBMIT, INTERPRET,
    PRE_TOOL_USE, POST_TOOL_USE,
    STOP, STOP_FAILURE,
)


# ---------- Types ----------


@dataclass
class Hook:
    event: str
    command: str
    matcher: str = ""

    def matches(self, target: str) -> bool:
        if not self.matcher:
            return True
        try:
            return re.search(self.matcher, target) is not None
        except re.error:
            return False


@dataclass
class HookDecision:
    allow: bool = True
    reason: str = ""
    modified_args: dict | None = None
    injected_context: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "HookDecision":
        decision = (d.get("decision") or "allow").lower()
        return cls(
            allow=(decision != "deny"),
            reason=str(d.get("reason") or ""),
            modified_args=(
                d.get("modified_args")
                if isinstance(d.get("modified_args"), dict) else None
            ),
            injected_context=str(d.get("injected_context") or ""),
        )

    def merge(self, other: "HookDecision") -> "HookDecision":
        """Combine with another. Conservative: any deny wins. Modified
        args from later hooks override earlier ones. Injected context is
        concatenated."""
        ctx_parts = [
            x for x in (self.injected_context, other.injected_context) if x
        ]
        return HookDecision(
            allow=self.allow and other.allow,
            reason=other.reason if not self.allow else (other.reason or self.reason),
            modified_args=other.modified_args or self.modified_args,
            injected_context="\n".join(ctx_parts),
        )


# ---------- Loading ----------


def load_hooks() -> dict[str, list[Hook]]:
    """Read all configured hooks from filesystem."""
    out: dict[str, list[Hook]] = {ev: [] for ev in ALL_EVENTS}

    # Single-file form.
    if config.HOOKS_FILE.exists():
        try:
            data = json.loads(config.HOOKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        block = data.get("hooks", data) if isinstance(data, dict) else {}
        if isinstance(block, dict):
            _consume(out, block)

    # Per-file form.
    if config.HOOKS_DIR.is_dir():
        for p in sorted(config.HOOKS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and "event" in data:
                ev = str(data["event"])
                if ev in ALL_EVENTS:
                    h = _coerce_hook(ev, data)
                    if h:
                        out[ev].append(h)
            elif isinstance(data, dict):
                _consume(out, data.get("hooks", data))
    return out


def _consume(out: dict[str, list[Hook]], block: dict) -> None:
    for ev, items in block.items():
        if ev not in ALL_EVENTS:
            continue
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for d in items:
            h = _coerce_hook(ev, d)
            if h:
                out[ev].append(h)


def _coerce_hook(event: str, d: Any) -> Hook | None:
    if not isinstance(d, dict) or not d.get("command"):
        return None
    return Hook(
        event=event,
        command=str(d["command"]),
        matcher=str(d.get("matcher", "")),
    )


# ---------- Firing ----------


def fire(
    event: str,
    payload: dict,
    *,
    match_field: str | None = None,
    hooks_index: dict[str, list[Hook]] | None = None,
) -> HookDecision:
    """Run all matching hooks for `event`. Returns combined decision.

    If `hooks_index` is None, hooks are loaded fresh. Pass an index to
    reuse a cached load across many fires within one turn.
    """
    if hooks_index is None:
        hooks_index = load_hooks()
    candidates = hooks_index.get(event) or []
    if not candidates:
        return HookDecision()  # no-hook fast path
    if match_field is not None:
        target = str(payload.get(match_field, ""))
        candidates = [h for h in candidates if h.matches(target)]
    final = HookDecision()
    for hook in candidates:
        try:
            d = _run_hook(hook, event, payload)
        except Exception as e:
            d = HookDecision(allow=True, reason=f"hook error: {type(e).__name__}: {e}")
        final = final.merge(d)
    return final


def _run_hook(hook: Hook, event: str, payload: dict) -> HookDecision:
    proc = subprocess.run(
        hook.command,
        input=json.dumps({"event": event, "payload": payload}),
        capture_output=True,
        text=True,
        shell=True,
        cwd=str(config.WORKSPACE),
        timeout=config.HOOK_TIMEOUT,
    )
    # Exit code 1 = deny (Claude Code convention).
    if proc.returncode == 1:
        return HookDecision(
            allow=False,
            reason=(proc.stdout.strip() or proc.stderr.strip() or "hook returned 1"),
        )
    out = (proc.stdout or "").strip()
    if not out:
        return HookDecision(allow=True)
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return HookDecision(allow=True)
    if not isinstance(d, dict):
        return HookDecision(allow=True)
    return HookDecision.from_dict(d)
