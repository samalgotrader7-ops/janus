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

# v1.31.13 — model-facing mode-awareness messages. Field-validation
# finding from Sam's VPS (2026-05-08): after PLAN_REFUSED, the
# model often responded with "ready to execute when you say go" —
# but mode is still 'plan' (refusal does NOT trigger mode switch),
# so any subsequent fs_write / fs_edit / shell call would be
# blocked by mode 'plan'. The model didn't know mode was still
# plan because the bare "PLAN_REFUSED" sentinel carried no
# context. v1.31.13 enriches both result strings with explicit
# guidance — the model receives concrete next-step instructions
# instead of a bare token. The sentinel substrings remain literal
# so cli_rich's post-turn mode-switch detector
# (which uses ``PLAN_APPROVED in str(result_preview)``) keeps
# working without changes.

PLAN_APPROVED_MESSAGE = (
    "PLAN_APPROVED — user approved your plan. The framework will "
    "switch mode to default at the end of this turn. You may begin "
    "executing the plan now (subsequent write/exec calls in THIS "
    "turn may still be blocked because the mode-switch is "
    "post-turn; if so, finish your text reply and the user's next "
    "message will run with default mode active)."
)
PLAN_REFUSED_MESSAGE = (
    "PLAN_REFUSED — user wants you to refine the plan, not execute "
    "it. Mode is STILL 'plan' (writes/exec are BLOCKED). Do NOT "
    "attempt fs_write, fs_edit, fs_multi_edit, shell, code_exec_*, "
    "or any other write/exec tool — they will all return 'blocked "
    "by mode plan'. Instead: ask the user what they'd like changed "
    "(call clarify if helpful), iterate the plan in chat, then "
    "call exit_plan_mode again with the revised plan when ready. "
    "The user must switch mode (/mode default or /mode auto) "
    "BEFORE any execution can happen."
)


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
        # v1.31.13 — return the full guidance message instead of the
        # bare sentinel. Sentinel substrings ("PLAN_APPROVED" /
        # "PLAN_REFUSED") remain literal at the start of each
        # message so existing detectors (cli_rich post-turn mode
        # switch) keep working unchanged.
        return PLAN_APPROVED_MESSAGE if ok else PLAN_REFUSED_MESSAGE
