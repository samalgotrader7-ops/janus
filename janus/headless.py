"""
headless.py — non-interactive entry point (Phase 16).

WHY:
For CI, scripts, editors, cron jobs — anywhere there's no human at the
keyboard. Same interpreter + executor + capability + hook stack as the
interactive CLI; what's gone is the user interaction.

USAGE:
    janus -p "summarize this README"
    echo "compute 2+2" | janus -p
    janus -p "list .py files" --output-format json
    janus --continue -p "what did we figure out earlier?"

OUTPUT FORMATS:
  text  — plain final output to stdout (default).
  json  — single JSON object: request, interpretations, choice, output,
          trace, tokens. One line, no trailing newline.
  jsonl — one JSON object per trace step, then a final {type:"output"}.

EXIT CODES:
  0  success
  1  interpreter or executor raised
  2  usage error (empty prompt, unknown flag)
  3  a hook denied a tool call mid-run

APPROVAL POSTURE:
There's no user to prompt, so the headless approver is "deny everything
not capability-granted." Attach a skill via --skill <name> to widen the
allowlist. Read-only tools (`fs_read`, `fs_list`, `web_fetch`, etc.)
work without any skill because they skip the approver gate.
"""

from __future__ import annotations
import json
import re
import sys
import time
from typing import Any

from . import (
    config, interpreter, executor, logger, memory,
    skills as skills_mod,
    cache, conversation, cost,
)
from .tools import default_registry, make_capability_aware, CapabilitySet


EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE = 2
EXIT_HOOK_DENIED = 3


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _deny(*a, **kw) -> bool:
    return False


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

    # Build the tool registry + approver.
    skill = skills_mod.load(skill_name) if skill_name else None
    caps = skill.capabilities if skill else CapabilitySet()
    approver = make_capability_aware(_deny, caps)
    tools = default_registry(capabilities=caps)

    # Conversation may have been pre-loaded by --continue / --resume.
    pending = conversation.take_pending()
    conv = pending if pending is not None else conversation.new()

    preamble = cache.snapshot().preamble + conv.recent_context_block()

    record: dict[str, Any] = {
        "ts": logger.now_iso(),
        "model": config.MODEL,
        "workspace": str(config.WORKSPACE),
        "request": prompt,
        "gateway": "headless",
    }
    if skill is not None:
        record["skill"] = skill.name
        record["skill_state"] = skill.state

    cost.new_turn()

    try:
        interps = interpreter.interpret(prompt, memory_preamble=preamble)
        record["interpretations"] = interps
    except Exception as e:
        record["error"] = f"interpret: {type(e).__name__}: {e}"
        try:
            logger.write(record)
        except Exception:
            pass
        sys.stderr.write(f"interpreter error: {e}\n")
        return EXIT_RUNTIME_ERROR

    if not interps:
        sys.stderr.write("error: interpreter returned no candidates\n")
        try:
            logger.write(record)
        except Exception:
            pass
        return EXIT_RUNTIME_ERROR

    chosen = interps[0]
    record["choice"] = "auto-first"

    try:
        t0 = time.time()
        output, trace = executor.execute(
            original_request=prompt,
            chosen_label=chosen.get("label", ""),
            chosen_action=chosen.get("action", prompt),
            tools=tools,
            approver=approver,
            skill_body=(skill.body if skill else ""),
            memory_preamble=preamble,
        )
        record["execute_ms"] = int((time.time() - t0) * 1000)
        record["output"] = output
        record["trace"] = trace
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
            choice="auto-first",
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

    # Render to stdout.
    _emit(
        output_format=output_format,
        no_color=no_color,
        quiet=quiet,
        prompt=prompt,
        interps=interps,
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
    interps: list,
    choice: Any,
    output: str,
    trace: list,
    ts: str,
) -> None:
    if output_format == "json":
        envelope = {
            "ts": ts,
            "request": prompt,
            "interpretations": interps,
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
