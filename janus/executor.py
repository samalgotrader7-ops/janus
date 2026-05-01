"""
executor.py — the agent loop.

After interpretation is chosen, this module runs the actual task with
tool access. Standard tool-use loop:

  user msg + chosen interpretation
    └─> model
          ├─> if tool_calls: run each, append results, loop
          └─> if final text: return it

Bounded by config.MAX_STEPS to prevent runaway loops.

WHY THIS LOOP IS SIMPLER THAN HERMES:
Hermes runs sub-agents, has a 90-step default, and supports interrupt-and-
redirect mid-flight. We don't. v1 stays linear: one user turn produces one
final output. We add multi-turn complexity only when the logs prove a
single turn isn't enough for real tasks. Almost always it is.
"""

from __future__ import annotations
import json
from typing import Callable

from . import config, llm, hooks
from .tools import Registry


def execute(
    original_request: str,
    chosen_label: str,
    chosen_action: str,
    tools: Registry,
    approver: Callable[..., bool],
    on_step: Callable[[dict], None] | None = None,
    *,
    skill_body: str = "",
    memory_preamble: str = "",
    temperature: float = 0.7,
    stream: bool = False,
) -> tuple[str, list[dict]]:
    """Run the executor loop. Returns (final_text, trace).

    `trace` is a list of structured step records for logging.
    `on_step` is called with each step (for live UI updates).
    `skill_body` (Phase 3): if non-empty, prepended to the system prompt.
    `memory_preamble` (Phase 2): if non-empty, prepended above the skill body.
    `temperature`: pinned at 0 by the eval harness for deterministic replays.
    """
    head = ""
    if memory_preamble:
        head += memory_preamble + "\n"
    if skill_body:
        head += f"# Active skill\n\n{skill_body}\n\n---\n\n"
    system = (
        head
        + f'The user originally asked: "{original_request}"\n\n'
        f"After clarification, they confirmed they want this specific interpretation:\n"
        f"  label: {chosen_label}\n"
        f"  action: {chosen_action}\n\n"
        f"Workspace (where files and shell run): {config.WORKSPACE}\n\n"
        f"Use tools as needed. Be direct and concise. Do not narrate the "
        f"interpretation step itself in your final answer — just deliver the result. "
        f"When the task is complete, respond with the final answer and no further tool calls."
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": original_request},
    ]
    trace: list[dict] = []

    # Phase 11: load hooks once per execute() call. No-hook path is O(1).
    hooks_index = hooks.load_hooks()

    for step in range(config.MAX_STEPS):
        if stream:
            # Stream the assistant turn token-by-token; the last yield is
            # the assembled message dict (content + tool_calls).
            msg: dict = {}
            try:
                gen = llm.chat_stream(
                    messages=messages, tools=tools.schemas(),
                    temperature=temperature,
                )
                for chunk in gen:
                    if isinstance(chunk, str):
                        if on_step:
                            on_step({"step": step, "type": "stream_chunk",
                                     "text": chunk})
                    elif isinstance(chunk, dict):
                        msg = chunk
            except Exception as e:
                # Fall back to non-streaming on any stream error.
                msg = llm.chat(messages=messages, tools=tools.schemas(),
                               temperature=temperature)
        else:
            msg = llm.chat(messages=messages, tools=tools.schemas(),
                           temperature=temperature)

        # Append the assistant turn verbatim (preserves tool_calls structure).
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # Final answer.
            text = msg.get("content") or ""
            trace.append({"step": step, "type": "final", "text": text})
            if on_step:
                on_step(trace[-1])
            return text, trace

        # Execute each tool call requested by the model.
        for call in tool_calls:
            fn = call["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            step_record = {
                "step": step,
                "type": "tool_call",
                "tool": name,
                "args": args,
            }
            if on_step:
                on_step(step_record)

            # PreToolUse hook can deny, modify args, or inject context.
            pre = hooks.fire(
                hooks.PRE_TOOL_USE,
                {"tool": name, "args": args},
                match_field="tool",
                hooks_index=hooks_index,
            )
            if not pre.allow:
                result = (
                    f"refused by hook: {pre.reason}"
                    if pre.reason else "refused by hook"
                )
                step_record["hook_denied"] = True
            else:
                if pre.modified_args is not None:
                    args = pre.modified_args
                    step_record["args"] = args
                    step_record["hook_modified_args"] = True
                result = tools.call(name, args, approver)
                if pre.injected_context:
                    result = result + "\n\n[hook context]\n" + pre.injected_context

                # PostToolUse hook (no deny semantics — fires for observation).
                post = hooks.fire(
                    hooks.POST_TOOL_USE,
                    {"tool": name, "args": args, "result": result[:4000]},
                    match_field="tool",
                    hooks_index=hooks_index,
                )
                if post.injected_context:
                    result = result + "\n\n[hook post-context]\n" + post.injected_context

            # Truncate massive outputs in trace (full output still goes to model).
            preview = result if len(result) < 800 else result[:800] + "…[truncated in log]"
            step_record["result_preview"] = preview
            trace.append(step_record)
            if on_step:
                # Update with result preview for UI.
                on_step({**step_record, "type": "tool_result"})

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    # Hit step limit without producing a final answer.
    return (
        f"[stopped: reached {config.MAX_STEPS} step limit without final answer]",
        trace,
    )
