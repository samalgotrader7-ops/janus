"""
headless.py — non-interactive entry point (v1.0 chat-shaped).

WHY:
For CI, scripts, editors, cron jobs — anywhere there's no human at the
keyboard. Same chat() loop the interactive CLI uses; what's gone is the
prompt for input and the picker theater (which v1.0 retired anyway).

USAGE:
    janus -p "summarize this README"
    echo "compute 2+2" | janus -p
    janus -p "list .py files" --output-format json
    janus --continue -p "what did we figure out earlier?"

OUTPUT FORMATS:
  text  — plain final output to stdout (default).
  json  — single JSON object: request, output, trace, tokens. One line.
  jsonl — one JSON object per trace step, then a final {type:"output"}.

EXIT CODES:
  0  success
  1  executor raised
  2  usage error (empty prompt, unknown flag)
  3  a hook denied a tool call mid-run

APPROVAL POSTURE:
There's no user to prompt, so the headless approver is mode-aware but
defaults to deny on ASK (no TTY for y/N). Read tools always run.
acceptEdits widens to writes auto. bypassPermissions runs everything.
plan denies writes/exec. Set via JANUS_APPROVAL=acceptEdits etc., or
attach a skill via --skill <name> to grant capability tokens for
specific narrow targets without flipping the whole session.
"""

from __future__ import annotations
import json
import re
import sys
import time
from typing import Any

from . import (
    app, config, executor, logger, memory,
    permissions,
    skills as skills_mod,
    cache, conversation, cost,
)
from .tools import default_registry, make_protected, CapabilitySet


EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2
EXIT_HOOK_DENIED = 3


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _make_headless_approver(mode: str):
    """Mode-aware approver with no TTY. ASK becomes DENY because there's
    no one to ask. ALLOW and DENY decisions stand.

    To execute writes/exec from headless: either flip to acceptEdits /
    bypassPermissions via JANUS_APPROVAL, or attach a skill whose
    capability tokens grant the specific action.
    """
    def approver(action_label: str, details: str, **kw) -> bool:
        risk = kw.get("risk") or permissions.risk_from_verb(
            (kw.get("capability") or (None, "", None))[1]
        )
        decision = permissions.decide(risk, mode)
        if decision == permissions.ALLOW:
            return True
        # ASK and DENY both fall through to deny (no TTY).
        return False
    return approver


def run(
    *,
    prompt: str,
    output_format: str = "text",
    no_color: bool = False,
    quiet: bool = False,
    skill_name: str | None = None,
) -> int:
    """Execute one prompt non-interactively. Returns process exit code."""
    config.assert_configured()
    config.ensure_home()

    if not prompt or not prompt.strip():
        sys.stderr.write("error: empty prompt\n")
        return EXIT_USAGE
    if output_format not in ("text", "json", "jsonl"):
        sys.stderr.write(
            f"error: unknown --output-format: {output_format} "
            f"(text|json|jsonl)\n"
        )
        return EXIT_USAGE

    mode = permissions.normalize(config.APPROVAL_MODE)

    # Build the tool registry + approver.
    skill = skills_mod.load(skill_name) if skill_name else None
    caps = skill.capabilities if skill else CapabilitySet()
    base_approver = _make_headless_approver(mode)
    approver = make_protected(base_approver, caps, mode)
    tools = default_registry(capabilities=caps)

    # Conversation may have been pre-loaded by --continue / --resume.
    pending = conversation.take_pending()
    conv = pending if pending is not None else conversation.new(gateway="headless")

    preamble = cache.snapshot().preamble + conv.recent_context_block()

    # Rebuild messages from prior turns when continuing a conversation
    # so the model has context. Otherwise start fresh.
    messages: list[dict] = []
    if pending is not None:
        for t in conv.turns:
            req = (t.get("request") or "").strip()
            out = (t.get("output") or "").strip()
            if req:
                messages.append({"role": "user", "content": req})
            if out:
                messages.append({"role": "assistant", "content": out})

    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": prompt,
        "gateway": "headless",
        "mode": mode,
    }
    if skill is not None:
        record["skill"] = skill.name
        record["skill_state"] = skill.state

    cost.new_turn()

    try:
        t0 = time.time()
        # v1.25.0 Phase 0: route through the surface-agnostic event stream.
        output, trace = app.run_turn(
            messages=messages,
            user_input=prompt,
            tools=tools,
            approver=approver,
            skill_body=(skill.body if skill else ""),
            memory_preamble=preamble,
            mode=mode,
            workspace=str(config.WORKSPACE),
            tool_count=len(tools.names()),
            skill_count=len(skills_mod.list_skills()),
            stream=False,
        )
        record["execute_ms"] = int((time.time() - t0) * 1000)
        record["output"] = output
        record["trace"] = trace
        record["choice"] = "chat"
    except Exception as e:
        record["error"] = f"execute: {type(e).__name__}: {e}"
        try:
            logger.write(record)
        except Exception:
            pass
        sys.stderr.write(f"executor error: {e}\n")
        return EXIT_RUNTIME_ERROR

    try:
        logger.write(record)
    except Exception:
        pass

    # Persist conversation so --continue picks it up.
    try:
        conv.add_turn(
            request=prompt, output=output,
            choice="chat",
            skill=(skill.name if skill else None),
            ts=record["ts"],
        )
        conversation.save(conv)
    except Exception:
        pass

    # Detect hook denials in the trace → exit code 3.
    hook_denied = any(
        isinstance(s, dict) and s.get("hook_denied")
        for s in (trace or [])
    )

    _emit(
        output_format=output_format,
        no_color=no_color,
        quiet=quiet,
        prompt=prompt,
        choice=record.get("choice"),
        output=output,
        trace=trace,
        ts=record["ts"],
    )

    return EXIT_HOOK_DENIED if hook_denied else EXIT_OK


def _emit(
    *,
    output_format: str,
    no_color: bool,
    quiet: bool,
    prompt: str,
    choice: Any,
    output: str,
    trace: list,
    ts: str,
) -> None:
    if output_format == "json":
        envelope = {
            "ts": ts,
            "request": prompt,
            "choice": choice,
            "output": _strip_ansi(output) if no_color else output,
            "trace": trace,
            "tokens": {
                "prompt": cost.turn_stats().prompt_tokens,
                "completion": cost.turn_stats().completion_tokens,
                "usd": cost.turn_stats().usd,
            },
        }
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return
    if output_format == "jsonl":
        for step in trace or []:
            sys.stdout.write(
                json.dumps({"type": "step", **step}, ensure_ascii=False) + "\n"
            )
        sys.stdout.write(json.dumps(
            {"type": "output",
             "text": _strip_ansi(output) if no_color else output},
            ensure_ascii=False,
        ) + "\n")
        return
    # text
    text = _strip_ansi(output) if no_color else output
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
