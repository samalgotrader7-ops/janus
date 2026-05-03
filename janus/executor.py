"""
executor.py — the agent loop.

TWO ENTRY POINTS:

  execute()  — legacy interpreter-confirmed flow. Used by cli.py (basic),
               gateways/telegram, gateways/web, gateways/whatsapp,
               headless.py, orchestrator.py. Preserved for back-compat
               while v1.0 migrates the surfaces one at a time.

  chat()     — v1.0 Claude-Code-shaped flow. Used by cli_rich.py. Takes
               the full conversation message list and the new user input;
               appends user+assistant+tool turns as it goes; no
               interpretation gate. Mode-aware approver (from
               permissions.py) decides allow / ask / deny per tool risk.

Both share the inner tool-call loop logic — chat() is just execute()
without the interpretation framing in the system prompt and with
multi-turn message history.

Bounded by config.MAX_STEPS to prevent runaway loops.
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


# ---------- v1.0 chat() — Claude-Code-shaped loop ----------


JANUS_CHAT_SYSTEM = """You are Janus — an interactive coding assistant running \
locally for the user.

When the user mentions "Janus" they mean this framework you are running inside, \
NOT the Roman god, the Bond villain, or any other "Janus".

You have a set of tools. Use them when they help; reply directly when they don't.
The runtime gates dangerous tools by permission mode (default / acceptEdits / \
plan / bypassPermissions) — you do not need to ask the user before calling a \
tool. If a tool is denied by the mode, you'll see the refusal and can adapt.

Be direct. Don't narrate the plan before doing it. Don't apologize for using \
tools. When the task is done, give a short answer summarizing what you found \
or did. Skip preamble like "I'll help you with that".

If the user is just chatting — asking a question, replying, clarifying — just \
answer them. Not every turn needs tool use.

# Janus configuration surface
Persistent state under ~/.janus/:
- user.md           plain-text user model (memory)
- skills/           markdown skills (one per file)
- conversations/    saved JSON sessions (--continue / --resume)
- hooks.json        PreToolUse / PostToolUse / SessionStart hooks
- mcp/servers.json  MCP server configs
- log.jsonl         append-only audit trail of every turn

Telegram gateway: env JANUS_TELEGRAM_TOKEN + JANUS_TELEGRAM_CHATS, then \
`janus telegram`. Web gateway: `janus web`. WhatsApp: `janus whatsapp`.
Model / API: env JANUS_API_KEY + JANUS_API_BASE + JANUS_MODEL.

Do NOT invent config files or schemas you haven't been shown. If unsure how \
something is configured, say so and point the user to the README rather than \
fabricating a plausible-looking schema."""


def _build_chat_system(
    *,
    workspace: str,
    mode: str = "default",
    memory_preamble: str = "",
    skill_body: str = "",
    tool_count: int | None = None,
    skill_count: int | None = None,
) -> str:
    """Compose the system message for the v1.0 chat loop."""
    parts: list[str] = []
    if memory_preamble:
        parts.append(memory_preamble.rstrip())
        parts.append("\n\n---\n\n")
    if skill_body:
        parts.append(f"# Active skill\n\n{skill_body.rstrip()}\n\n---\n\n")
    parts.append(JANUS_CHAT_SYSTEM)

    inv_bits: list[str] = []
    if tool_count is not None:
        inv_bits.append(f"{tool_count} tool(s)")
    if skill_count is not None:
        inv_bits.append(f"{skill_count} installed skill(s)")
    if inv_bits:
        parts.append(
            "\n\nRight now you have access to " + " and ".join(inv_bits) + "."
        )

    parts.append(f"\n\nWorkspace: {workspace}")
    parts.append(f"\nPermission mode: {mode}")
    if mode == "plan":
        parts.append(
            "\n\nYou are in PLAN mode — write and exec tools will be denied. "
            "Use only read tools. Propose a plan in prose; the user will switch "
            "modes when ready to execute."
        )
    elif mode == "bypassPermissions":
        parts.append(
            "\n\nYou are in BYPASS mode — every tool will run without asking. "
            "Be especially careful: prefer narrow, reversible actions."
        )
    return "".join(parts)


def chat(
    *,
    messages: list[dict],
    user_input: str,
    tools: Registry,
    approver: Callable[..., bool],
    on_step: Callable[[dict], None] | None = None,
    skill_body: str = "",
    memory_preamble: str = "",
    mode: str = "default",
    workspace: str | None = None,
    tool_count: int | None = None,
    skill_count: int | None = None,
    temperature: float = 0.7,
    stream: bool = True,
    model: str | None = None,
) -> tuple[str, list[dict]]:
    """v1.0 Claude-Code-shaped chat turn.

    `messages` — the running conversation. MUTATED IN PLACE: this turn's
    user / assistant / tool messages are appended so the next call sees
    them. The caller keeps a single list reference across turns.

    `model` (v1.4) — overrides config.MODEL for this turn only. Swarm
    sub-agents pass it to mix cheap/expensive models per role.

    Returns `(final_text, trace)`. The trace is the per-turn tool-call
    audit (same shape as execute()'s trace) for logging.
    """
    ws = workspace or str(config.WORKSPACE)
    system = _build_chat_system(
        workspace=ws,
        mode=mode,
        memory_preamble=memory_preamble,
        skill_body=skill_body,
        tool_count=tool_count,
        skill_count=skill_count,
    )

    # Refresh the system message at index 0 every turn — memory and mode
    # can change between turns, and the cost of one cache miss is cheaper
    # than diverging context.
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": system}
    else:
        messages.insert(0, {"role": "system", "content": system})

    messages.append({"role": "user", "content": user_input})

    trace: list[dict] = []
    hooks_index = hooks.load_hooks()

    # Only pass `model=` when explicitly set so existing fake_llm stubs
    # (and any other test doubles that don't accept the kwarg yet) keep
    # working. Real llm.chat / chat_stream always accept it.
    model_kw = {"model": model} if model is not None else {}

    for step in range(config.MAX_STEPS):
        if stream:
            msg: dict = {}
            try:
                gen = llm.chat_stream(
                    messages=messages, tools=tools.schemas(),
                    temperature=temperature, **model_kw,
                )
                for chunk in gen:
                    if isinstance(chunk, str):
                        if on_step:
                            on_step({"step": step, "type": "stream_chunk",
                                     "text": chunk})
                    elif isinstance(chunk, dict):
                        msg = chunk
            except Exception:
                msg = llm.chat(messages=messages, tools=tools.schemas(),
                               temperature=temperature, **model_kw)
        else:
            msg = llm.chat(messages=messages, tools=tools.schemas(),
                           temperature=temperature, **model_kw)

        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            text = msg.get("content") or ""
            trace.append({"step": step, "type": "final", "text": text})
            if on_step:
                on_step(trace[-1])
            return text, trace

        for call in tool_calls:
            fn = call["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            step_record = {
                "step": step, "type": "tool_call",
                "tool": name, "args": args,
            }
            if on_step:
                on_step(step_record)

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

                post = hooks.fire(
                    hooks.POST_TOOL_USE,
                    {"tool": name, "args": args, "result": result[:4000]},
                    match_field="tool",
                    hooks_index=hooks_index,
                )
                if post.injected_context:
                    result = result + "\n\n[hook post-context]\n" + post.injected_context

            preview = result if len(result) < 800 else result[:800] + "…[truncated in log]"
            step_record["result_preview"] = preview
            trace.append(step_record)
            if on_step:
                on_step({**step_record, "type": "tool_result"})

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    return (
        f"[stopped: reached {config.MAX_STEPS} step limit without final answer]",
        trace,
    )
