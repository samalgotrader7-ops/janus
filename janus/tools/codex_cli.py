"""
tools/codex_cli.py — OpenAI Codex CLI wrapper (v1.38.2, refactored
in v1.38.4 to use external_cli_base).

WHY:
Sam confirmed (2026-05-10): we wrap openai/codex — the open-source
agent CLI from OpenAI (GitHub: openai/codex). Non-interactive
mode is ``codex exec "<prompt>"``. Same shape as claude_code
(`-p`) and aider (`--message`).

ENV OVERRIDES:
  JANUS_CODEX_BIN     absolute path to the codex binary
  JANUS_CODEX_FLAGS   space-separated default flags (caller args
                      win position)
"""

from __future__ import annotations

import os

from . import base
from .. import config
from . import external_cli_base as _eb


DEFAULT_TIMEOUT = _eb.DEFAULT_TIMEOUT
TIMEOUT_MAX = _eb.TIMEOUT_MAX
MAX_OUTPUT_BYTES = _eb.MAX_OUTPUT_BYTES

shutil = _eb.shutil  # noqa: F401  — tests patch cx.shutil.which


def _strip_ansi(s: str) -> str:
    return _eb.strip_ansi(s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    return _eb.truncate(s, limit)


def _codex_binary() -> str | None:
    return _eb.find_binary("codex", "JANUS_CODEX_BIN")


def _normalize_extra_args(raw):
    return _eb.normalize_extra_args(raw)


def _env_flags():
    return _eb.env_flags("JANUS_CODEX_FLAGS")


class CodexCli(base.Tool):
    name = "codex_cli"
    description = (
        "Hand off a focused coding sub-task to OpenAI's Codex CLI "
        "(https://github.com/openai/codex) running non-interactively "
        "via `codex exec`. Returns codex's stdout (ANSI-stripped). "
        "Requires `codex` on PATH (`npm i -g @openai/codex` or the "
        "binary release) and an authenticated session. DESTRUCTIVE — "
        "codex can edit files in cwd. Every call requires user "
        "approval. Default timeout 300s, hard-capped at "
        "JANUS_EXTERNAL_CLI_TIMEOUT (default 600s). "
        "Use `extra_args` to pass codex flags like '--json' for "
        "structured output or '--model <id>' to override its model."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send codex. Plain text; codex "
                    "decides which files to read/write."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory codex runs in. Defaults to the "
                    "current Janus workspace."
                ),
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional codex flags inserted between `exec` "
                    "and the prompt. Examples: ['--json'] for "
                    "structured output, ['--model', 'gpt-5'] to pin "
                    "a specific model. Caller wins over "
                    "JANUS_CODEX_FLAGS."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Wall-clock timeout in seconds. Default "
                    f"{DEFAULT_TIMEOUT}, hard-capped at "
                    f"JANUS_EXTERNAL_CLI_TIMEOUT (default {TIMEOUT_MAX}s)."
                ),
            },
        },
        "required": ["prompt"],
    }
    dangerous = True
    risk = "exec"

    def run(self, args: dict, approver: base.Approver) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "codex_cli: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"codex_cli: cwd does not exist: {cwd}"

        timeout = _eb.clamp_timeout(
            args.get("timeout"), DEFAULT_TIMEOUT, TIMEOUT_MAX,
        )

        extra = _normalize_extra_args(args.get("extra_args"))
        env_flags_v = _env_flags()

        binary = _codex_binary()
        if not binary:
            return (
                "codex_cli: `codex` binary not found on PATH. Install "
                "from https://github.com/openai/codex (npm or binary "
                "release) or set JANUS_CODEX_BIN to its absolute path."
            )

        cmd = [binary, "exec"] + env_flags_v + extra + [prompt]

        flags_summary = ""
        all_flags = env_flags_v + extra
        if all_flags:
            shown = all_flags[:6]
            ellipsis = "…" if len(all_flags) > 6 else ""
            flags_summary = f"   flags: {' '.join(shown)}{ellipsis}\n"

        ok = _eb.request_approval(
            approver=approver,
            name="codex_cli",
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            extra_lines=flags_summary,
        )
        if not ok:
            return "codex_cli: refused by user."

        return _eb.execute(
            cmd=cmd,
            cwd=cwd,
            timeout=timeout,
            name="codex_cli",
            binary_path=binary,
        )
