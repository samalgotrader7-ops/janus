"""
tools/base.py — tool framework.

DESIGN NOTE:
Each tool is a class with:
  - name: identifier the LLM sees
  - description: human-language description (LLM uses this to choose)
  - parameters: JSON schema for arguments
  - dangerous: bool — whether to require approval before executing
  - run(args, approver) -> str: actual execution

Phase 2/3 addition:
  The Registry now carries an optional CapabilitySet from the active skill.
  `approver(...)` accepts a structured `capability=(tool, verb, target)` kwarg.
  Tools that opt in pass the kwarg; the approver short-circuits to True when
  the active skill grants the action via its tokens.

  Phase 1 tools that don't pass the kwarg still work — they just always go
  through the y/n prompt for dangerous actions.
"""

from __future__ import annotations
from typing import Any, Callable

from .capabilities import CapabilitySet


# Approver signature: action_label, details, **kwargs (capability=(tool,verb,target))
Approver = Callable[..., bool]


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    dangerous: bool = False  # if True, run() must call approver before side-effects
    # v1.0: risk class for the permission-mode matrix.
    # "read" — pure observation; "write" — file/state mutation; "exec" —
    # arbitrary code or external side effects. Subclasses override.
    risk: str = "exec"

    def run(self, args: dict, approver: Approver) -> str:
        """Execute the tool. Return a string the model will read as observation.

        approver(action_label, details, *, capability=(tool, verb, target),
                 risk="read"|"write"|"exec") -> bool:
        If dangerous, call this before any side-effect. Returns False → return a
        refusal string. The Registry injects `risk=` automatically based on the
        tool's class attribute, so individual tools never need to pass it.
        """
        raise NotImplementedError

    def schema(self) -> dict:
        """OpenAI tool-call format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Registry:
    """Holds the active tool set and dispatches calls.

    `capabilities` is the active skill's grants (empty by default = Phase 1
    behavior, every dangerous action prompts).
    """

    def __init__(self, tools: list[Tool], capabilities: CapabilitySet | None = None):
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self.capabilities: CapabilitySet = capabilities or CapabilitySet()

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict, approver: Approver) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        # v1.0: inject the tool's risk class into every approver call so
        # the permission-mode approver can decide allow / ask / deny without
        # each individual tool having to opt in.
        # v1.5: also inject `args` + `tool_name` so auto-mode wrappers can
        # do per-call risk analysis (e.g., flag `rm -rf /` even when the
        # matrix says allow). Tools didn't have to pass these manually.
        risk = getattr(tool, "risk", "exec")

        def tool_approver(action_label: str, details: str, **kw: Any) -> bool:
            kw.setdefault("risk", risk)
            kw.setdefault("args", args)
            kw.setdefault("tool_name", name)
            return approver(action_label, details, **kw)

        try:
            return tool.run(args, tool_approver)
        except Exception as e:
            # Errors are observations, not crashes. The model sees them
            # and can correct course.
            return f"error: {type(e).__name__}: {e}"

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def set_capabilities(self, caps: CapabilitySet) -> None:
        self.capabilities = caps

    def add_tool(self, tool: Tool) -> None:
        """Mount a tool into a live registry. Used by MCP (Phase 10) to add
        third-party tools after construction."""
        self._tools[tool.name] = tool

    def remove_tool(self, name: str) -> None:
        """Drop a tool by name (no-op if absent). v1.8: gateway swaps the
        bundled callback-less Clarify with a callback-bearing instance."""
        self._tools.pop(name, None)


def make_capability_aware(approver: Approver, caps: CapabilitySet) -> Approver:
    """Wrap a base approver so it auto-approves capability-granted actions.

    Tools call approver("action", "details", capability=("fs", "write", "src/x.py")).
    If caps.grants(...), we return True without prompting.
    Otherwise we delegate to the base approver.

    v1.5: kwargs (risk, args, tool_name, capability, ...) are forwarded
    to the base approver — earlier versions dropped them, breaking the
    auto-mode wrapper which needs `args` + `tool_name` for risk analysis.
    """
    def wrapped(action_label: str, details: str, **kw: Any) -> bool:
        cap = kw.get("capability")
        if cap and len(cap) == 3:
            tool, verb, target = cap
            if caps.grants(tool, verb, target):
                return True
        return approver(action_label, details, **kw)
    return wrapped


def make_protected(
    base_approver: Approver,
    caps: CapabilitySet,
    mode: str = "default",
) -> Approver:
    """Standard approver layering for v1.5: capability-aware + (when
    mode='auto') auto-mode risk analyzer. One-stop helper so call sites
    don't have to reproduce the layering boilerplate.

    Layer order matters:
      auto → capability → mode-aware base
    Auto fires FIRST so a capability-granted dangerous call still blocks
    (skill widening doesn't override safety).

    Use:
      approver = make_protected(_make_mode_approver(mode), caps, mode)
    Replaces the v1.0-1.4 idiom of `make_capability_aware(base, caps)`
    when the caller wants auto-mode behavior to apply automatically when
    mode='auto'. For non-auto modes the helper is equivalent to plain
    make_capability_aware.
    """
    wrapped = make_capability_aware(base_approver, caps)
    if mode == "auto":
        wrapped = make_auto_aware(wrapped)
    return wrapped


def make_auto_aware(approver: Approver) -> Approver:
    """Wrap a base approver so dangerous tool calls are auto-blocked
    based on heuristic risk analysis (v1.5 auto mode).

    Pipeline:
      1. Run auto_mode.analyze_call(tool_name, args) on the raw call.
      2. If the analyzer returns BLOCK, refuse without consulting the
         base approver — Janus says "this action is risky" and the
         model gets a refusal string it can react to (P8: errors are
         observations).
      3. Otherwise delegate to the base approver as normal.

    The wrapper reads tool_name and args from approver kwargs, which the
    Registry now injects automatically (Registry.call). Tools that
    haven't been updated still work — auto-mode just can't analyze them
    (no kwargs → falls through to base approver).

    Used by executor.execute / executor.chat when mode='auto'. Composes
    cleanly with make_capability_aware: in production the layering is
    auto → capability → permission-mode (auto checks first because a
    capability-granted `rm -rf /` is still bad).
    """
    # Lazy import: auto_mode imports config which imports environment;
    # avoid the cycle at module-load time.
    from .. import auto_mode

    def wrapped(action_label: str, details: str, **kw: Any) -> bool:
        tool_name = kw.get("tool_name") or ""
        args = kw.get("args") or {}
        verdict = auto_mode.analyze_call(
            tool_name, args, capability=kw.get("capability"),
        )
        if not verdict.allowed:
            # Record the block in kw so the base approver (or a logger
            # wrapper) can surface it. Block is FINAL — don't fall
            # through; auto-mode's whole point is to refuse without
            # asking.
            kw["auto_blocked"] = True
            kw["auto_block_reason"] = verdict.reason
            return False
        return approver(action_label, details, **kw)
    return wrapped
