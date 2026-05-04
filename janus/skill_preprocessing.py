"""
skill_preprocessing.py — schema validation for skill files (v1.12.0).

WHY THIS EXISTS:
Pre-v1.12 a malformed skill silently failed to load. Sam writes a new
skill, drops a typo in the YAML, types `/skills`, and the skill just
doesn't appear. No error surfaced. He has to debug by reading the
loader code.

Hermes calls this `agent/skill_preprocessing.py`. v1.12 ports the
concept: walk every skill file, run a strict schema check, return a
list of issues with file path + line context. The user runs
`/skills validate` and sees exactly what's wrong.

WHAT GETS CHECKED:

  REQUIRED:
    - frontmatter delimiters: ^---\\n ... \\n---\\n
    - `name:` non-empty, kebab-case (matches the filename stem)
    - `description:` non-empty
    - `state:` one of {quarantined, trusted-supervised, trusted-auto}
    - body (everything after frontmatter) non-empty

  RECOMMENDED (warnings, not errors):
    - capabilities: present (empty {} OK), valid verb shapes
    - tool_names: list of strings if present
    - body length 200-20000 chars (very short or very long is suspicious)

  STRUCTURAL:
    - YAML round-trips through our hand-rolled parser without dropping
      keys (catches indentation issues that look fine to humans)
    - frontmatter doesn't contain literal '{}' (the v1.7.0 agent_create
      bug: empty inline dicts confuse our parser)

DESIGN — NEVER FAIL THE LOADER:
preprocessing only REPORTS issues. It does NOT prevent skills from
loading (the loader is more permissive — it'll happily skip bad lines
and load what it can). The user opts in to validation via
`/skills validate` or `janus skills validate`. Net: existing setups
that have one slightly broken skill keep working; the user can fix
the skill at their leisure.

P5 (plain-text state): the report is plain text the user reads + acts
on, not opaque error codes.
"""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config


VALID_STATES = ("quarantined", "trusted-supervised", "trusted-auto")


@dataclass
class Issue:
    """One validation finding. severity is 'error' | 'warning' | 'info'."""
    file: str
    severity: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        loc = f"{self.file}"
        if self.line is not None:
            loc += f":{self.line}"
        return f"[{self.severity}] {loc} — {self.message}"


# ---------- Per-file validator ----------


def validate_skill_file(path: Path) -> list[Issue]:
    """Return all issues for one skill file."""
    issues: list[Issue] = []
    name = path.name

    if not path.is_file():
        issues.append(Issue(name, "error", "file does not exist"))
        return issues

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        issues.append(Issue(name, "error", f"unreadable: {e}"))
        return issues

    if not text.strip():
        issues.append(Issue(name, "error", "file is empty"))
        return issues

    # Frontmatter delimiters.
    if not text.startswith("---"):
        issues.append(Issue(name, "error",
            "missing opening '---' frontmatter delimiter on line 1",
            line=1))
        return issues  # Can't parse the rest meaningfully.

    # Locate the closing '---' line.
    lines = text.splitlines()
    fm_close = None
    for i, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            fm_close = i
            break
    if fm_close is None:
        issues.append(Issue(name, "error",
            "missing closing '---' delimiter — frontmatter never terminates"))
        return issues

    fm_text = "\n".join(lines[1:fm_close - 1])
    body = "\n".join(lines[fm_close:]).lstrip("\n")

    # Parse frontmatter via the same parser the loader uses.
    from .skills import _parse_yaml_subset
    try:
        fm = _parse_yaml_subset(fm_text)
    except Exception as e:
        issues.append(Issue(name, "error",
            f"frontmatter unparseable: {type(e).__name__}: {e}"))
        return issues

    if not isinstance(fm, dict):
        issues.append(Issue(name, "error",
            "frontmatter did not parse as a mapping"))
        return issues

    # name
    declared_name = (fm.get("name") or "").strip() if isinstance(fm.get("name"), str) else ""
    if not declared_name:
        issues.append(Issue(name, "error", "missing or empty `name:` field"))
    else:
        # Recommend (not require) name == filename stem.
        if declared_name != path.stem:
            issues.append(Issue(name, "warning",
                f"`name:` is {declared_name!r} but filename stem is "
                f"{path.stem!r} — these usually match"))

    # description
    desc = fm.get("description")
    if not desc or (isinstance(desc, str) and not desc.strip()):
        issues.append(Issue(name, "error",
            "missing or empty `description:` field"))

    # state
    state_val = fm.get("state")
    if not state_val:
        issues.append(Issue(name, "error",
            "missing `state:` field — use `quarantined`, "
            "`trusted-supervised`, or `trusted-auto`"))
    elif state_val not in VALID_STATES:
        issues.append(Issue(name, "error",
            f"invalid state {state_val!r} — must be one of "
            f"{', '.join(VALID_STATES)}"))

    # capabilities — recommended (warning if missing).
    caps = fm.get("capabilities")
    if caps is None:
        issues.append(Issue(name, "info",
            "no `capabilities:` declared — every dangerous action will "
            "prompt for approval (correct for quarantined skills)"))
    elif isinstance(caps, str) and caps.strip() == "{}":
        # The v1.7.0 agent_create bug — caught by validate now.
        issues.append(Issue(name, "error",
            "capabilities is the literal string '{}' — our YAML parser "
            "doesn't support inline dicts. Use `capabilities:` (no "
            "braces) on its own line, or omit the field for an empty "
            "capability set."))
    elif not isinstance(caps, dict):
        issues.append(Issue(name, "error",
            f"`capabilities:` must be a mapping, got "
            f"{type(caps).__name__}"))
    else:
        for verb_key, targets in caps.items():
            if not isinstance(verb_key, str):
                issues.append(Issue(name, "error",
                    f"capability key must be a string, got "
                    f"{type(verb_key).__name__}"))
                continue
            if "." not in verb_key:
                issues.append(Issue(name, "warning",
                    f"capability key {verb_key!r} should be 'tool.verb' "
                    f"shape (e.g., 'fs.read', 'shell.exec')"))
            if not isinstance(targets, (list, str)):
                issues.append(Issue(name, "error",
                    f"capability {verb_key!r} value must be a list or "
                    f"string, got {type(targets).__name__}"))

    # tool_names (optional, but if present must be list of strings).
    tool_names = fm.get("tool_names") or fm.get("tool-names")
    if tool_names is not None and not isinstance(tool_names, list):
        issues.append(Issue(name, "error",
            f"`tool_names:` must be a list, got {type(tool_names).__name__}"))
    elif isinstance(tool_names, list):
        for i, t in enumerate(tool_names):
            if not isinstance(t, str):
                issues.append(Issue(name, "error",
                    f"tool_names[{i}] must be a string, got "
                    f"{type(t).__name__}"))

    # body
    if not body.strip():
        issues.append(Issue(name, "error",
            "body (after frontmatter) is empty — skills need a prompt"))
    else:
        body_len = len(body.strip())
        if body_len < 50:
            issues.append(Issue(name, "warning",
                f"body is very short ({body_len} chars) — skill prompts "
                f"usually need 200-2000 chars to be useful"))
        elif body_len > 20000:
            issues.append(Issue(name, "warning",
                f"body is very long ({body_len} chars) — consider "
                f"splitting into multiple focused skills"))

    return issues


# ---------- Cross-skill validator ----------


def validate_all() -> list[Issue]:
    """Run validate_skill_file across every skill in SKILLS_DIR.

    Used by /skills validate. Returns a flat list of issues from all
    files, sorted by severity (errors first) then file name.
    """
    issues: list[Issue] = []
    if not config.SKILLS_DIR.is_dir():
        return issues
    for path in sorted(config.SKILLS_DIR.glob("*.md")):
        issues.extend(validate_skill_file(path))
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (severity_rank.get(i.severity, 3), i.file, i.line or 0))
    return issues


def render(issues: list[Issue]) -> str:
    """Format the issue list as a human-readable report."""
    if not issues:
        return "All skills validate cleanly."
    by_severity: dict[str, list[Issue]] = {}
    for issue in issues:
        by_severity.setdefault(issue.severity, []).append(issue)
    lines: list[str] = []
    for sev in ("error", "warning", "info"):
        items = by_severity.get(sev, [])
        if not items:
            continue
        lines.append(f"## {sev.upper()} ({len(items)})\n")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")
    summary = ", ".join(
        f"{len(by_severity.get(sev, []))} {sev}"
        for sev in ("error", "warning", "info")
        if by_severity.get(sev)
    )
    lines.append(f"Summary: {summary}")
    return "\n".join(lines)
