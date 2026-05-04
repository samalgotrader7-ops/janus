"""
project_context.py — auto-load per-directory instructions (v1.15.0).

WHY THIS EXISTS:
Claude Code reads CLAUDE.md from the workspace and prepends it to the
system prompt. This gives Claude project-specific instructions
("don't touch the auth module", "tests use vitest not jest", "use
make build instead of npm run build") without the user typing them
each turn.

Janus pre-v1.15 auto-loaded ~/.janus/memory/ (user-level) but NOT
project-level instructions. Result: switching between projects
required re-explaining each one's conventions.

v1.15 ports the pattern. Files we look for, in priority order:

  ./JANUS.md
  ./.janus/PROJECT.md
  ./CLAUDE.md      ← shared with Claude Code (so users don't maintain two)
  ./AGENTS.md      ← shared with the open agentskills.io standard

ALL matching files are concatenated (most-relevant first), bounded by
PROJECT_INSTRUCTIONS_MAX_BYTES so a giant CLAUDE.md doesn't blow the
prompt. The block is appended to memory.prepend_for_prompt() between
the multi-category memory and the live state introspection.

OPT-OUT:
JANUS_PROJECT_INSTRUCTIONS=0 disables auto-loading.

WALK-UP:
We walk up from CWD to the home dir, stopping at the first .git
boundary. This matches Claude Code's "find the repo root" behavior —
the user's CWD might be deep inside src/, but the project's CLAUDE.md
lives at the repo root.

P5 (plain-text state): the user OWNS these files. No magic dotfiles
that aren't visible.
"""

from __future__ import annotations
import os
from pathlib import Path

from . import config


PROJECT_INSTRUCTIONS_MAX_BYTES = int(
    os.getenv("JANUS_PROJECT_INSTRUCTIONS_BYTES", "8192")
)

# Filenames we look for, in priority order. First match wins per
# directory; multiple directories' files are concatenated up to the
# byte budget.
INSTRUCTION_FILENAMES = (
    "JANUS.md",
    ".janus/PROJECT.md",
    "CLAUDE.md",
    "AGENTS.md",
)


def is_enabled() -> bool:
    return os.getenv("JANUS_PROJECT_INSTRUCTIONS", "1") not in ("0", "false", "no")


def find_instruction_files(start: Path | None = None) -> list[Path]:
    """Walk up from `start` (default: WORKSPACE), collecting any
    matching instruction files. Stops at the first .git boundary OR
    the user's home directory (whichever comes first).

    Within a directory, the FIRST matching filename in
    INSTRUCTION_FILENAMES wins — we don't load both JANUS.md and
    CLAUDE.md from the same dir.

    Returns paths in walk order (CWD first, then parents).
    """
    if start is None:
        start = Path(config.WORKSPACE).resolve()
    home = Path.home().resolve()
    found: list[Path] = []
    cur = start
    seen_git = False
    while True:
        for fname in INSTRUCTION_FILENAMES:
            candidate = cur / fname
            if candidate.is_file():
                found.append(candidate)
                break  # one file per directory
        # Stop at git root or home — whichever comes first.
        if (cur / ".git").exists():
            seen_git = True
        parent = cur.parent
        if seen_git or cur == parent or cur == home:
            break
        cur = parent
    return found


def load_block() -> str:
    """Return a markdown block for prepending to the system prompt.

    Empty string when:
      - opt-out via env
      - no instruction files found
    """
    if not is_enabled():
        return ""
    files = find_instruction_files()
    if not files:
        return ""

    parts: list[str] = []
    remaining = PROJECT_INSTRUCTIONS_MAX_BYTES
    for path in files:
        if remaining <= 200:  # not enough left for a useful chunk
            break
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Display path relative to the user's home if possible.
        try:
            display = "~/" + str(path.relative_to(Path.home()))
            display = display.replace("\\", "/")
        except ValueError:
            display = str(path)
        if len(text) > remaining:
            text = text[:remaining - 30] + "\n\n[truncated for prompt]"
        parts.append(f"## from {display}\n\n{text.strip()}")
        remaining -= len(text)

    if not parts:
        return ""
    header = (
        "# Project instructions (auto-loaded from working directory)\n\n"
        "These are the rules / conventions for the project the user is "
        "currently working in. They override generic agent advice when "
        "they conflict."
    )
    return header + "\n\n" + "\n\n---\n\n".join(parts) + "\n\n"
