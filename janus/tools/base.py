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

    def run(self, args: dict, approver: Approver) -> str:
        """Execute the tool. Return a string the model will read as observation.

        approver(action_label, details, *, capability=(tool, verb, target)) -> bool:
        If dangerous, call this before any side-effect. Returns False → return a
        refusal string.
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
        try:
            return tool.run(args, approver)
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


def make_capability_aware(approver: Approver, caps: CapabilitySet) -> Approver:
    """Wrap a base approver so it auto-approves capability-granted actions.

    Tools call approver("action", "details", capability=("fs", "write", "src/x.py")).
    If caps.grants(...), we return True without prompting.
    Otherwise we delegate to the base approver.
    """
    def wrapped(action_label: str, details: str, **kw: Any) -> bool:
        cap = kw.get("capability")
        if cap and len(cap) == 3:
            tool, verb, target = cap
            if caps.grants(tool, verb, target):
                return True
        return approver(action_label, details)
    return wrapped
