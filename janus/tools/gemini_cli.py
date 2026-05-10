"""
tools/gemini_cli.py — Google Gemini CLI wrapper (v1.38.3, refactored
in v1.38.4 to use external_cli_base).

WHY:
Wraps Google's open-source gemini-cli
(https://github.com/google-gemini/gemini-cli). Non-interactive
mode is ``gemini -p [extra_args...] <prompt>``.

ENV OVERRIDES:
  JANUS_GEMINI_BIN     absolute path to the gemini binary
  JANUS_GEMINI_FLAGS   space-separated default flags
"""

from __future__ import annotations

import os

from . import base
from .. import config
from . import external_cli_base as _eb


DEFAULT_TIMEOUT = _eb.DEFAULT_TIMEOUT
TIMEOUT_MAX = _eb.TIMEOUT_MAX
MAX_OUTPUT_BYTES = _eb.MAX_OUTPUT_BYTES

shutil = _eb.shutil  # noqa: F401  — tests patch gc.shutil.which


def _strip_ansi(s: str) -> str:
    return _eb.strip_ansi(s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    return _eb.truncate(s, limit)


def _gemini_binary() -> str | None:
    return _eb.find_binary("gemini", "JANUS_GEMINI_BIN")


def _normalize_extra_args(raw):
    return _eb.normalize_extra_args(raw)


def _env_flags():
    return _eb.env_flags("JANUS_GEMINI_FLAGS")


class GeminiCli(base.Tool):
    name = "gemini_cli"
    description = (
        "Hand off a focused coding sub-task to Google's Gemini CLI "
        "(https://github.com/google-gemini/gemini-cli) running "
        "non-interactively via `gemini -p`. Returns gemini's stdout "
        "(ANSI-stripped). Requires `gemini` on PATH (`npm install -g "
        "@google/gemini-cli`) and an authenticated session. "
        "DESTRUCTIVE — gemini can edit files in cwd. Every call "
        "requires user approval. Default timeout 300s, hard-capped at "
        "JANUS_EXTERNAL_CLI_TIMEOUT (default 600s). "
        "Use `extra_args` to pass gemini flags like '--all-files' "
        "(include all workspace files in context), '--model' "
        "(pin a specific Gemini model), or '--sandbox'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send Gemini. Plain text; "
                    "Gemini decides which files to read/write."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory gemini runs in. Defaults to "
                    "the current Janus workspace."
                ),
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional gemini flags inserted between `-p` "
                    "and the prompt. Examples: ['--all-files'] for "
                    "full context, ['--model', 'gemini-2.5-pro'] to "
                    "pin a model. Caller wins over JANUS_GEMINI_FLAGS."
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
            return "gemini_cli: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"gemini_cli: cwd does not exist: {cwd}"

        timeout = _eb.clamp_timeout(
            args.get("timeout"), DEFAULT_TIMEOUT, TIMEOUT_MAX,
        )

        extra = _normalize_extra_args(args.get("extra_args"))
        env_flags_v = _env_flags()

        binary = _gemini_binary()
        if not binary:
            return (
                "gemini_cli: `gemini` binary not found on PATH. "
                "Install with `npm install -g @google/gemini-cli` or "
                "set JANUS_GEMINI_BIN to its absolute path."
            )

        cmd = [binary, "-p"] + env_flags_v + extra + [prompt]

        flags_summary = ""
        all_flags = env_flags_v + extra
        if all_flags:
            shown = all_flags[:6]
            ellipsis = "…" if len(all_flags) > 6 else ""
            flags_summary = f"   flags: {' '.join(shown)}{ellipsis}\n"

        ok = _eb.request_approval(
            approver=approver,
            name="gemini_cli",
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            extra_lines=flags_summary,
        )
        if not ok:
            return "gemini_cli: refused by user."

        return _eb.execute(
            cmd=cmd,
            cwd=cwd,
            timeout=timeout,
            name="gemini_cli",
            binary_path=binary,
        )
