"""
tools/delegate.py — lightweight subagent spawn (v1.8.0).

WHY THIS EXISTS:
We already have `swarm_run` for parallel multi-phase work driven by a
markdown spec. That's the right tool when:
  - the work decomposes into multiple parallel sub-tasks
  - the spec is reusable
  - you want per-role models / budgets / cancellation / cost tracking

But most "send a small task to a fresh head" needs DON'T want all that.
A common case is: "research X, then continue what you were doing." You
want a fresh executor.chat with a focused instruction, restricted tool
surface, and a string answer back. No spec file. No JSONL. No aggregator.

Hermes calls this `delegate_tool`. v1.8 ports it.

USE CASES:
- Side research that would pollute your main context ("look up X, return
  one paragraph")
- Risky-tool isolation (give the subagent only fs_read, not fs_write)
- Quick second-opinion ("read this file, tell me if anything looks
  suspicious") without restructuring the conversation

NON-USE CASES:
- Parallel fanout → use swarm_run
- Anything that needs to mutate the parent's conversation → just do it
  in the parent, the result is in your turn
- Multi-step coordinated work → swarm_run

BUDGETS:
- max_steps default 10 (parent's MAX_STEPS is 25)
- model default = parent's model (caller can override for cheaper sub-models)
- tool_names default = read-only set (fs_read, fs_list, fs_grep, fs_glob,
  web_fetch, web_search, session_recent, session_search). Caller can
  override but not escalate beyond what the parent has.
- recursion guard: a delegate cannot call delegate_tool itself
  (threading.local depth check, similar to swarm).

P5 (plain-text state): the subagent's transcript is appended to
log.jsonl with type="delegate_subagent" so the user can audit any
delegated work.
"""

from __future__ import annotations
import threading
from typing import Any

from .. import config
from .base import Tool


# Default tool surface for delegated work — read-only safe set.
# Callers (or skill capabilities) can widen this; we don't allow
# them to escalate beyond what the parent already has access to.
_DEFAULT_DELEGATE_TOOLS = (
    "fs_read", "fs_list", "fs_grep", "fs_glob",
    "web_fetch", "web_search",
    "session_recent", "session_search",
)


# Recursion guard — delegate cannot spawn delegate. Same pattern as
# swarms.recursion: thread-local depth counter incremented on entry.
_THREAD_LOCAL = threading.local()


def _depth() -> int:
    return getattr(_THREAD_LOCAL, "delegate_depth", 0)


class DelegateRecursionError(RuntimeError):
    pass


class Delegate(Tool):
    """Spawn a fresh executor.chat for one focused sub-task."""

    name = "delegate"
    description = (
        "Spawn a fresh executor.chat to handle ONE focused sub-task and "
        "return its final reply as a string. Lighter than swarm_run "
        "(no spec file, no parallel fanout) — use for side research, "
        "tool-isolated checks, second-opinion reads. Subagent gets a "
        "RESTRICTED tool surface (read-only by default) and runs in "
        "auto mode. Cannot itself call delegate (recursion blocked). "
        "Returns the subagent's final assistant reply text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Self-contained task description. The subagent has "
                    "NO context from your conversation — give it "
                    "everything it needs. Be specific about what to "
                    "return (a paragraph? a JSON object? a yes/no?)."
                ),
            },
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional override of the subagent's tool surface. "
                    "Default: " + ", ".join(_DEFAULT_DELEGATE_TOOLS) + ". "
                    "Only specify if you genuinely need to widen "
                    "(e.g., add fs_write for a sub-task that should "
                    "produce a file)."
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": (
                    "Hard cap on subagent's tool-calling iterations. "
                    "Default 10. Don't raise above 20 — if the subagent "
                    "needs more, the task is too big for delegate "
                    "(use swarm_run)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override (e.g., a cheaper model for "
                    "narrow research). Default: parent's model."
                ),
            },
        },
        "required": ["task"],
    }
    risk = "exec"  # subagent can call other tools, so the spawn itself is exec

    def run(self, args: dict, approver) -> str:
        if _depth() >= 1:
            return (
                "error: delegate recursion blocked — a delegated subagent "
                "cannot itself call delegate. If you need parallel work, "
                "the parent should use swarm_run instead."
            )

        task = (args.get("task") or "").strip()
        if not task:
            return "error: task required"

        tool_names = args.get("tool_names")
        if tool_names is not None:
            if not isinstance(tool_names, list):
                return "error: tool_names must be a list of strings"
            tool_names = [str(n) for n in tool_names if str(n)]
        if not tool_names:
            tool_names = list(_DEFAULT_DELEGATE_TOOLS)

        ms_raw = args.get("max_steps")
        if ms_raw is None:
            max_steps = 10
        else:
            try:
                max_steps = int(ms_raw)
            except (TypeError, ValueError):
                return "error: max_steps must be an integer"
        max_steps = max(1, min(max_steps, 20))  # clamp 1-20 (0 → 1)

        model = args.get("model") or None

        if not approver(
            f"delegate → {task[:60]}",
            f"tool_names={tool_names} max_steps={max_steps} "
            f"model={model or '(default)'}",
            capability=("agent", "delegate", task[:40]),
        ):
            return f"refused: delegate({task[:60]})"

        # Delayed imports to avoid circular: tools/__init__ imports us;
        # we need executor + tools/__init__ resolution at call time only.
        from .. import app, executor, logger  # noqa: F401  (executor kept for back-compat refs)
        from . import default_registry, make_protected, CapabilitySet
        from ..permissions import (
            decide as _decide, ALLOW as _ALLOW,
        )

        caps = CapabilitySet()
        sub_tools = default_registry(capabilities=caps, tool_names=tool_names)

        # Subagent runs auto-mode; auto_risk_patterns still apply.
        def _sub_approver(action_label, details, **kw) -> bool:
            risk = kw.get("risk") or "read"
            return _decide(risk, "auto") == _ALLOW

        sub_approver = make_protected(_sub_approver, caps, "auto")

        # Save the parent's MAX_STEPS, lower it for the subagent's
        # bounded run, restore on exit.
        _orig_max = config.MAX_STEPS
        config.MAX_STEPS = max_steps
        _THREAD_LOCAL.delegate_depth = _depth() + 1
        try:
            # v1.25.0 Phase 0: route through the surface-agnostic event
            # stream. delegate is a sub-task spawner — leaving it on raw
            # executor.chat would reintroduce the exact drift Phase 0
            # exists to prevent (parent on app, child on executor).
            output, _trace = app.run_turn(
                messages=[],
                user_input=task,
                tools=sub_tools,
                approver=sub_approver,
                on_step=None,
                skill_body="",  # no skill — pure task execution
                memory_preamble="",  # subagent doesn't need full memory
                mode="auto",
                workspace=str(config.WORKSPACE),
                tool_count=len(sub_tools.schemas()),
                stream=False,
                model=model,
            )
        except Exception as e:
            return f"error: subagent crashed: {type(e).__name__}: {e}"
        finally:
            config.MAX_STEPS = _orig_max
            _THREAD_LOCAL.delegate_depth = max(0, _depth() - 1)

        try:
            logger.write({
                "ts": logger.now_iso(),
                "type": "delegate_subagent",
                "task": task[:200],
                "tool_names": tool_names,
                "max_steps": max_steps,
                "model": model,
                "output_preview": output[:300] if output else "",
            })
        except Exception:
            pass

        if not output:
            return "subagent returned empty output"
        # Trim huge outputs so the parent's context isn't blown.
        if len(output) > 8000:
            output = output[:8000] + f"\n… [+{len(output) - 8000} more chars]"
        return output
