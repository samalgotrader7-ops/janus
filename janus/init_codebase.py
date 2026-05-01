"""
init_codebase.py — `/init`: bootstrap user.md + skills from a workspace
(Phase 15).

WHY:
The first 5 minutes of using Janus is currently "stare at an empty
prompt and wonder what to type." `/init` removes that friction by
scanning the cwd, calling one cheap LLM pass, and proposing a starter
user.md + 1-3 skills the user can review and accept.

NEVER WRITES WITHOUT CONFIRMATION (P4):
This module produces a `proposal` dict. The CLI shows it, asks y/N per
piece, and only writes on yes. The model's draft is an offer, not a
commitment.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from . import config, llm


_PACKAGE_FILES = (
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "requirements.txt", "Gemfile", "build.gradle", "pom.xml",
    "tsconfig.json", "Makefile", "docker-compose.yml", "Dockerfile",
)
_README_NAMES = ("README.md", "README", "readme.md", "Readme.md")


def scan_workspace(max_excerpt_bytes: int = 2000) -> dict:
    """Build a small context bundle from the workspace root. Bounded —
    won't read into giant repos, just the tip of what's there."""
    ws = Path(config.WORKSPACE)
    out: dict[str, Any] = {
        "workspace": str(ws),
        "top_level": [],
        "readme": "",
        "package_files": {},
    }
    if not ws.exists():
        return out
    for child in sorted(ws.iterdir())[:100]:
        if child.name.startswith("."):
            continue
        out["top_level"].append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
        })
    for name in _README_NAMES:
        p = ws / name
        if p.is_file():
            try:
                out["readme"] = p.read_text(
                    encoding="utf-8", errors="replace",
                )[:max_excerpt_bytes]
            except Exception:
                pass
            break
    for name in _PACKAGE_FILES:
        p = ws / name
        if p.is_file():
            try:
                out["package_files"][name] = p.read_text(
                    encoding="utf-8", errors="replace",
                )[:max_excerpt_bytes]
            except Exception:
                continue
    return out


_INIT_SYSTEM = """You bootstrap a Janus user model + a small starter skill
set from a workspace.

You receive:
- `top_level`: items in the workspace root (file/dir)
- `readme`: README excerpt
- `package_files`: snippets from pyproject.toml / package.json / etc.

Produce STRICT JSON:
{
  "user_md_additions": [
    {"section": "Identity", "text": "..."},
    {"section": "Active projects", "text": "..."}
  ],
  "skill_proposals": [
    {"name": "kebab-case",
     "description": "one line",
     "capabilities": {"shell.exec": ["..."], "fs.read": ["**"]},
     "body": "markdown procedure (3-6 numbered steps)"}
  ]
}

GUIDELINES:
- 1-3 skills max. Bias toward fewer. The user can /skill new for more.
- Capabilities should be MINIMAL — prefer specific globs over broad ones.
- Skill bodies: numbered procedures. Short. The user reads each one.
- user_md_additions: 1-3 sections covering durable facts about the user
  and the active project. NOT one-off task content.

No prose, no markdown fences. STRICT JSON only."""


def propose() -> dict:
    """Single LLM call. Returns the parsed proposal or {} on failure."""
    ctx = scan_workspace()
    try:
        msg = llm.chat(
            messages=[
                {"role": "system", "content": _INIT_SYSTEM},
                {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.3,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        data = llm.parse_json_loose(msg.get("content") or "{}")
    except Exception as e:
        return {"error": f"unparseable JSON: {e}"}
    if not isinstance(data, dict):
        return {"error": "LLM did not return a JSON object"}
    return {
        "user_md_additions": data.get("user_md_additions") or [],
        "skill_proposals": data.get("skill_proposals") or [],
    }


def render(proposal: dict) -> str:
    """Pretty-print for terminal review."""
    if proposal.get("error"):
        return f"  error: {proposal['error']}"
    lines: list[str] = []
    lines.append("  proposed user.md additions:")
    adds = proposal.get("user_md_additions") or []
    if not adds:
        lines.append("    (none)")
    for a in adds:
        lines.append(f"    [{a.get('section', '')}]")
        for ln in str(a.get("text", "")).splitlines():
            lines.append(f"      {ln}")
    lines.append("")
    lines.append("  proposed skills:")
    sks = proposal.get("skill_proposals") or []
    if not sks:
        lines.append("    (none)")
    for s in sks:
        lines.append(f"    {s.get('name', '?')} — {s.get('description', '')}")
        lines.append(f"      capabilities: {s.get('capabilities', {})}")
    return "\n".join(lines)


def apply_user_md(additions: list) -> int:
    """Apply user_md_additions as memory ops. Returns count applied."""
    from . import memory
    ops = []
    for a in additions or []:
        section = (a.get("section") or "").strip()
        text = (a.get("text") or "").strip()
        if section and text:
            ops.append({"op": "create_section", "section": section, "text": text})
    if ops:
        memory.apply(ops)
    return len(ops)


def apply_skill(proposal: dict) -> Path | None:
    """Persist one proposed skill as a quarantined .md."""
    from . import skills
    if not proposal:
        return None
    return skills.write_draft(proposal)
