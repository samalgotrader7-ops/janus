"""
commands.py — user-defined slash commands (Phase 15).

WHY:
Power users build muscle memory around their own shortcuts. Claude Code
exposes `~/.claude/commands/*.md`; we mirror it so every user-authored
command is portable across the two tools.

FORMAT:
A single markdown file with optional YAML frontmatter:

    ---
    name: refactor          # optional — defaults to filename stem
    description: ...        # optional — shown in /help
    ---

    Refactor the following code:

    {args}

    Return only the rewritten version, no commentary.

LOCATIONS (workspace overrides home on name conflict):
    ~/.janus/commands/<name>.md         — global
    <workspace>/.janus/commands/<name>.md — per-project

INVOCATION:
    /refactor my whole src/main.py file

The body is rendered with `{args}` replaced by everything after the
command name. The result is fed back into the agent loop AS IF the user
had typed it — same interpretation gate, same approval flow.

V1 SCOPE (per CLI_ENHANCEMENT_PLAN §6):
Body-only — no `!` for shell, no `@` for files yet. Add when there's
real demand.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from . import config, skills as skills_mod


@dataclass
class CustomCommand:
    name: str            # invoked as /<name>
    description: str
    body: str
    path: Path

    def render(self, args: str) -> str:
        """Substitute {args} / {ARGS} placeholders."""
        return (
            self.body
            .replace("{args}", args)
            .replace("{ARGS}", args)
        )


def search_dirs() -> list[Path]:
    """Locations scanned for command files. Order = priority (later wins)."""
    out: list[Path] = [config.HOME / "commands"]
    ws_local = Path(config.WORKSPACE) / ".janus" / "commands"
    if ws_local.resolve() != out[0].resolve():
        out.append(ws_local)
    return out


def load_all() -> dict[str, CustomCommand]:
    """name -> CustomCommand. Later directories override earlier ones."""
    out: dict[str, CustomCommand] = {}
    for d in search_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                cmd = _load_one(p)
            except Exception:
                continue
            if cmd:
                out[cmd.name] = cmd
    return out


def _load_one(path: Path) -> CustomCommand | None:
    text = path.read_text(encoding="utf-8")
    fm, body = skills_mod.parse_frontmatter(text)
    name = str(fm.get("name") or path.stem).strip()
    if not name or "/" in name or "\\" in name:
        return None
    desc = str(fm.get("description") or "").strip()
    return CustomCommand(
        name=name,
        description=desc,
        body=body.strip(),
        path=path,
    )
