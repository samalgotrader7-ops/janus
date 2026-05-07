"""
tools/subagent.py — first-class subagent spawn (v1.27.0).

Replaces the lightweight ``delegate`` tool (v1.8.0) with a proper
first-class agent. ``delegate`` stays bundled for back-compat but is
deprecated; new code should call ``subagent`` instead.

WHAT'S NEW vs delegate:

  * **Structured briefing.** ``description`` (3-5 words for traces) +
    ``prompt`` (full task). delegate took a single ``task`` string with
    no separation between "what is this for" and "what to do".

  * **Subagent types.** ``subagent_type`` selects a preset that
    bundles a tool surface AND a system-prompt prefix. Built-in
    presets: ``general`` (default), ``explore``, ``plan``,
    ``code-review``. delegate hard-coded one read-only tool surface
    and no prompt specialization.

  * **Live progress in the parent's chat stream.** While the subagent
    runs, every tool-call / tool-result / final / etc. event is
    wrapped with ``type='subagent_step'`` and forwarded to the
    parent's ``on_step`` callback (read from the app-layer
    thread-local). The user sees streaming progress instead of a
    silent block.

  * **Auditable run records.** Each invocation logs a
    ``type='subagent_run'`` entry to ``~/.janus/log.jsonl`` with the
    description, type, tool surface, max_steps, and a 300-char
    output preview.

WHAT'S PRESERVED from delegate:

  * Recursion guard at depth 1 (a subagent cannot itself call
    subagent). Same threading.local pattern.
  * Auto-mode runs by default with auto_risk_patterns active.
  * ``max_steps`` clamped to ``[1, 20]`` — anything bigger means
    you want ``swarm_run``.
  * Output truncated at 8000 chars so the parent's context doesn't
    blow up on a chatty subagent.
  * Errors-as-observations: tool-level crashes return
    ``"error: subagent crashed: ..."`` instead of propagating.

WHEN TO USE which:

  * One focused sub-task with a string answer back → ``subagent``.
  * Parallel multi-phase work driven by a markdown spec →
    ``swarm_run``.
  * Anything that needs to MUTATE the parent's conversation → just
    do it in the parent. The result is in your turn.

DESIGN — LIVE PROGRESS PLUMBING:

The parent's ``chat()`` runs in a worker thread spawned by
``app.chat_events`` / ``app.run_turn``. That entry point sets
``app._app_thread_local.parent_on_step`` to the queue-push function
before invoking ``executor.chat``. Tool calls run on the same worker
thread, so ``Subagent.run()`` can read the thread-local and forward
events. Surfaces that call ``executor.chat`` directly (without going
through ``app.run_turn``) get graceful no-op behavior — the
thread-local is unset, the subagent runs but no events are forwarded.

DESIGN — WHY NOT ALSO CHANGE TOOL.run SIGNATURE:

Adding ``on_step=`` to every Tool's ``run`` would touch ~30 tool
files for a feature only this one tool uses. A targeted thread-local
read is the smaller, lower-risk change. Future tools that want
event-stream access can use the same pattern (read
``app._app_thread_local.parent_on_step``).
"""

from __future__ import annotations
import threading
from typing import Any

from .. import config
from .base import Tool


# ---------- Subagent type presets ----------
#
# Each preset is (tool_names, prompt_prefix). The prompt_prefix is
# prepended to the JANUS_CHAT_SYSTEM the subagent sees, so it knows
# its specialization in addition to the standard rules.

_PRESETS: dict[str, tuple[tuple[str, ...], str]] = {
    "general": (
        # Default read-only safe set. Same surface as delegate.
        ("fs_read", "fs_list", "fs_grep", "fs_glob",
         "web_fetch", "web_search",
         "session_recent", "session_search"),
        # No prompt prefix — relies on JANUS_CHAT_SYSTEM alone.
        "",
    ),
    "explore": (
        # Pure file search. No web — exploration is local code reading.
        ("fs_read", "fs_list", "fs_grep", "fs_glob"),
        "# Subagent role: EXPLORE\n\n"
        "You are a focused exploration subagent. Your job is to LOCATE "
        "code in the workspace — find files by pattern, grep for symbols "
        "or keywords, answer 'where is X defined' or 'which files "
        "reference Y'.\n\n"
        "Return findings, not analysis. Do NOT speculate about behavior "
        "you haven't read. Quote file:line references. If the answer is "
        "'not found', say so in one sentence.",
    ),
    "plan": (
        # Plan agent: read + ExitPlanMode. No write/exec — strictly design.
        ("fs_read", "fs_list", "fs_grep", "fs_glob",
         "session_recent", "session_search",
         "exit_plan_mode"),
        "# Subagent role: PLAN\n\n"
        "You are a software-architect subagent. Design an implementation "
        "approach for the task — DO NOT implement it. Read the relevant "
        "code, identify the critical files to modify, list the steps in "
        "order, surface architectural trade-offs, and end with a one-line "
        "estimate of how many tool calls implementation will take.\n\n"
        "When you're done, call exit_plan_mode(plan='...') with the "
        "structured plan. The parent agent decides whether to execute.",
    ),
    "code-review": (
        # Read-only review. Same tool set as explore.
        ("fs_read", "fs_list", "fs_grep", "fs_glob",
         "session_recent", "session_search"),
        "# Subagent role: CODE-REVIEW\n\n"
        "You are a code-review subagent. Read the changes / files the "
        "parent specified and evaluate them for: correctness "
        "(bugs / edge-case handling), security (injection / auth / "
        "secrets / SSRF), maintainability (naming / duplication / "
        "abstraction overshoot), test coverage.\n\n"
        "Return findings as a bulleted list grouped by severity "
        "(blocker / major / minor). Quote file:line references. Do not "
        "rewrite code; that's the parent's job.",
    ),
}


def subagent_types() -> list[str]:
    """Public accessor for the available preset names."""
    return list(_PRESETS.keys())


# ---------- Recursion guard ----------
#
# A subagent cannot itself spawn subagent (depth-1 limit). Same pattern
# delegate uses. Independent counter so the two limits don't share state.

_THREAD_LOCAL = threading.local()


def _depth() -> int:
    return getattr(_THREAD_LOCAL, "subagent_depth", 0)


# ---------- Tool ----------


class Subagent(Tool):
    """Spawn a fresh executor.chat for one focused sub-task.

    First-class replacement for delegate: structured briefing,
    subagent_type presets, live progress events to the parent,
    auditable run records.
    """

    name = "subagent"
    description = (
        "Spawn an isolated subagent for ONE focused sub-task. The "
        "subagent gets a fresh executor.chat with no context from "
        "your conversation, a restricted tool surface, and a bounded "
        "step budget. Use for side research, tool-isolated checks, "
        "second-opinion reads, or specialized work via subagent_type "
        "(general / explore / plan / code-review). Live progress "
        "streams to the user; final output returns as a string. "
        "Cannot itself call subagent (recursion blocked)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "Short (3-5 words) label for what this subagent "
                    "is doing. Shown in the parent's progress stream "
                    "and audit log. Example: 'Find auth code' or "
                    "'Review migration safety'."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The full task for the subagent. It has NO "
                    "context from your conversation — give it "
                    "everything it needs. Be specific about what to "
                    "return (a paragraph? a JSON object? a list of "
                    "file:line references?)."
                ),
            },
            "subagent_type": {
                "type": "string",
                "enum": list(_PRESETS.keys()),
                "description": (
                    "Specialization preset. 'general' (default) = "
                    "standard read-only set + standard prompt. "
                    "'explore' = file-search only, no web. "
                    "'plan' = read + ExitPlanMode for design tasks. "
                    "'code-review' = read + structured review prompt."
                ),
            },
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional override of the subagent's tool surface. "
                    "Default: the preset's tool list. Specify ONLY if "
                    "you genuinely need to widen (e.g., add fs_write "
                    "for a sub-task that should produce a file)."
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": (
                    "Hard cap on subagent's tool-calling iterations. "
                    "Default 10. Don't raise above 20 — if the "
                    "subagent needs more, the task is too big for "
                    "subagent (use swarm_run)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override (e.g., a cheaper model "
                    "for narrow research). Default: parent's model."
                ),
            },
        },
        "required": ["description", "prompt"],
    }
    risk = "exec"

    def run(self, args: dict, approver) -> str:
        # ---------- Validation ----------
        if _depth() >= 1:
            return (
                "error: subagent recursion blocked — a subagent cannot "
                "itself call subagent. If you need parallel work, the "
                "parent should use swarm_run instead."
            )

        description = (args.get("description") or "").strip()
        if not description:
            return "error: description required"
        if len(description) > 200:
            description = description[:200].rstrip() + "…"

        prompt_text = (args.get("prompt") or "").strip()
        if not prompt_text:
            return "error: prompt required"

        subagent_type = (args.get("subagent_type") or "general").strip().lower()
        if subagent_type not in _PRESETS:
            return (
                f"error: unknown subagent_type '{subagent_type}'. "
                f"Available: {', '.join(_PRESETS.keys())}"
            )
        preset_tools, preset_prefix = _PRESETS[subagent_type]

        tool_names = args.get("tool_names")
        if tool_names is not None:
            if not isinstance(tool_names, list):
                return "error: tool_names must be a list of strings"
            tool_names = [str(n) for n in tool_names if str(n)]
        if not tool_names:
            tool_names = list(preset_tools)

        ms_raw = args.get("max_steps")
        if ms_raw is None:
            max_steps = 10
        else:
            try:
                max_steps = int(ms_raw)
            except (TypeError, ValueError):
                return "error: max_steps must be an integer"
        max_steps = max(1, min(max_steps, 20))

        model = args.get("model") or None

        # ---------- Approval ----------
        if not approver(
            f"subagent[{subagent_type}] → {description}",
            f"prompt={prompt_text[:80]} tools={tool_names} "
            f"max_steps={max_steps} model={model or '(default)'}",
            capability=("agent", "subagent", description[:40]),
        ):
            return f"refused: subagent({description})"

        # ---------- Lazy imports (avoid circular) ----------
        from .. import app, executor, logger  # noqa: F401
        from . import default_registry, make_protected, CapabilitySet
        from ..permissions import (
            decide as _decide, ALLOW as _ALLOW,
        )

        caps = CapabilitySet()
        sub_tools = default_registry(capabilities=caps, tool_names=tool_names)

        def _sub_approver(action_label, details, **kw) -> bool:
            risk = kw.get("risk") or "read"
            return _decide(risk, "auto") == _ALLOW

        sub_approver = make_protected(_sub_approver, caps, "auto")

        # ---------- Live progress: forward sub-events to parent ----------
        parent_on_step = getattr(
            app._app_thread_local, "parent_on_step", None,
        )

        def _forward_step(event: dict) -> None:
            """Wrap each subagent event with type='subagent_step' and
            push to parent's on_step. The original sub-event is preserved
            under ``inner`` so renderers can drill in."""
            if parent_on_step is None:
                return
            try:
                wrapped = {
                    "type": "subagent_step",
                    "description": description,
                    "subagent_type": subagent_type,
                    "inner": event,
                }
                parent_on_step(wrapped)
            except Exception:
                # Never let a renderer crash break the subagent run.
                pass

        # Notify the parent that a subagent is starting. This event
        # gives renderers a chance to open a spinner / panel / etc.
        if parent_on_step is not None:
            try:
                parent_on_step({
                    "type": "subagent_start",
                    "description": description,
                    "subagent_type": subagent_type,
                    "tool_names": tool_names,
                    "max_steps": max_steps,
                })
            except Exception:
                pass

        # ---------- Save / restore parent state ----------
        _orig_max = config.MAX_STEPS
        config.MAX_STEPS = max_steps
        _THREAD_LOCAL.subagent_depth = _depth() + 1

        # Parent on_step is read by THIS subagent's tool calls (any
        # nested tools that also forward). Clear it during the
        # subagent's run so the subagent's own tools don't accidentally
        # pick up the parent's renderer. (Defense in depth — recursion
        # is already blocked at depth 1.)
        _orig_parent = getattr(app._app_thread_local, "parent_on_step", None)
        app._app_thread_local.parent_on_step = None

        try:
            output, _trace = app.run_turn(
                messages=[],
                user_input=prompt_text,
                tools=sub_tools,
                approver=sub_approver,
                on_step=_forward_step,
                skill_body=preset_prefix,  # specialization prompt
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
            _THREAD_LOCAL.subagent_depth = max(0, _depth() - 1)
            app._app_thread_local.parent_on_step = _orig_parent

        # Notify the parent that the subagent finished.
        if parent_on_step is not None:
            try:
                parent_on_step({
                    "type": "subagent_end",
                    "description": description,
                    "subagent_type": subagent_type,
                    "output_preview": (output or "")[:200],
                })
            except Exception:
                pass

        # ---------- Audit log ----------
        try:
            logger.write({
                "ts": logger.now_iso(),
                "type": "subagent_run",
                "description": description,
                "subagent_type": subagent_type,
                "tool_names": tool_names,
                "max_steps": max_steps,
                "model": model,
                "output_preview": output[:300] if output else "",
            })
        except Exception:
            pass

        if not output:
            return "subagent returned empty output"
        if len(output) > 8000:
            output = output[:8000] + f"\n… [+{len(output) - 8000} more chars]"
        return output


# Public exports for tests / introspection.
__all__ = ["Subagent", "subagent_types", "_THREAD_LOCAL"]
