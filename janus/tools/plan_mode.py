"""
tools/plan_mode.py — model-callable plan-presentation tool (v1.15.0).

WHY THIS EXISTS:
Pre-v1.15 a user could `/mode plan` and Janus would refuse all
write/exec. Useful for safety, but the workflow was awkward: the
model would write a plan in chat, the user would read it, and then
THE USER had to type `/mode default` to switch out. Two roundtrips
to start work.

Claude Code has `ExitPlanMode` — a tool the MODEL calls when it has
a plan ready, packaging the plan in the call args and asking the
user via the framework whether to proceed. v1.15 ports this.

CONTRACT:
- Only meaningful in mode='plan'. In other modes the tool returns
  an info message; the model has no need to call it.
- The plan body is rendered to the user via the same approver flow
  as any dangerous tool — the user sees the plan and approves or
  refuses. Approval = "switch the conversation to default mode and
  re-issue the user's last request to proceed with the plan."
  Refusal = "stay in plan; reply in chat instead."
- The framework (cli_rich, gateway) detects the approval and flips
  the session's mode_state. Tool only signals INTENT; mode flip
  is done by an external watcher because the tool has no reference
  to the session state.

DESIGN — TOOL OUTPUT IS A SIGNAL:
The tool returns the literal string PLAN_APPROVED or PLAN_REFUSED.
The chat surface inspects the trace for a tool result starting with
PLAN_APPROVED and acts: switch mode, replay the user's last input.

This is intentionally simple — the alternative (callback into a
session-state mutator) would require threading session refs through
every tool which we deliberately avoid.
"""

from __future__ import annotations
from typing import Callable

from .base import Tool


# Sentinels the chat surface watches for.
PLAN_APPROVED = "PLAN_APPROVED"
PLAN_REFUSED = "PLAN_REFUSED"


class ExitPlanMode(Tool):
    """Present a finished plan to the user and request mode-switch out of plan."""

    name = "exit_plan_mode"
    description = (
        "Present a completed plan to the user and request to leave "
        "plan mode so you can execute it. Use ONLY in mode=plan, "
        "after you've read enough of the codebase / requirements to "
        "have a concrete plan. The plan should be specific (file "
        "paths, function names, the change in each file) — not vague. "
        "If the user approves, the framework switches mode to default "
        "and you can begin executing. If they refuse, stay in plan "
        "and refine. Tool returns 'PLAN_APPROVED' or 'PLAN_REFUSED'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "The plan, in markdown. Include: WHAT you'll do, "
                    "WHICH files / functions are touched, WHY. Numbered "
                    "or bulleted steps. Skip code dumps — the user wants "
                    "the SHAPE of the change, not the diff."
                ),
            },
        },
        "required": ["plan"],
    }
    # Read-class because presenting a plan is observation; the actual
    # work happens AFTER mode-switch via other tools that gate on their
    # own risk class. This means plan mode (which DENIES write/exec)
    # still allows exit_plan_mode itself to run.
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        plan = (args.get("plan") or "").strip()
        if not plan:
            return "error: plan body is required"
        # The approver SHOWS the plan and asks. Capability key is fixed —
        # plan-exit isn't really capability-scoped (it's a meta-operation).
        ok = approver(
            "exit_plan_mode",
            plan[:4000],
            capability=("plan", "exit", "session"),
        )
        return PLAN_APPROVED if ok else PLAN_REFUSED
