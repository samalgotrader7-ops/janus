"""
project_detect.py — workspace project-type detection (v1.28.4).

Pure-compute heuristic that classifies the workspace into a coarse
project type (python / node / rust / go / mixed / unknown) by
looking for standard manifest files. The result feeds:

  * A one-line hint in the system prompt so the model knows what
    ecosystem it's working in (helps choose ``pytest`` vs ``npm
    test`` vs ``cargo test`` for verification, suggests the right
    file-line-reference shape, etc.).
  * The ``/project`` slash command that surfaces the detection
    plus the indicators that fired.
  * Future: skill / tool gating — skills could declare
    ``project_types: [python]`` in frontmatter and only auto-attach
    when the current project matches. v1.28.4 ships the
    detection; gating is a v1.28.x or v1.29.x follow-up.

DESIGN — MULTIPLE INDICATORS:

  Some projects look like multiple types (a Python project with
  a ``package.json`` for a docs-site frontend, etc.). We classify
  by THE STRONGEST signal — the presence of a primary manifest
  ranks higher than ancillary files. When two strong signals fire
  we return "mixed".

DESIGN — ENV OVERRIDE:

  ``JANUS_PROJECT_TYPE=<type>`` short-circuits detection. Useful
  for monorepos where auto-detection picks the wrong root, or for
  testing.

DESIGN — RESPECT THE WORKSPACE BOUNDARY:

  Detection only looks at the workspace root passed in. We don't
  walk parents or descend into subdirectories. The user owns
  ``config.WORKSPACE``; we trust it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config


# ---------- Manifest signals ----------
#
# Order matters: indicators earlier in the list have higher
# precedence when multiple project types are detected.

PYTHON_INDICATORS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "requirements.txt",
)

NODE_INDICATORS = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)

RUST_INDICATORS = ("Cargo.toml",)

GO_INDICATORS = ("go.mod",)


# ---------- Suggested test commands ----------
#
# Used by the /project slash command + as a hint in the project
# block. Not authoritative — verification.py has its own detection
# for the auto-run-tests path. Just a documentation aid.

TEST_COMMANDS = {
    "python": "pytest",
    "node": "npm test",
    "rust": "cargo test",
    "go": "go test ./...",
    "mixed": "(multiple — see the detected indicators)",
    "unknown": "(no project markers found)",
}


@dataclass
class ProjectInfo:
    """Snapshot of project detection for a workspace.

    ``type`` is one of: python / node / rust / go / mixed / unknown.
    ``indicators`` lists the specific manifest files that fired.
    ``test_command`` is a suggestion, not authoritative.
    ``workspace`` is the absolute path.
    ``source`` is "env" when ``JANUS_PROJECT_TYPE`` overrode
    detection, "auto" otherwise.
    """
    type: str = "unknown"
    indicators: list[str] = field(default_factory=list)
    test_command: str = ""
    workspace: str = ""
    source: str = "auto"

    @property
    def is_known(self) -> bool:
        return self.type not in ("unknown", "")


# ---------- Detection ----------


def _gather_indicators(workspace: Path) -> dict[str, list[str]]:
    """Walk the indicator catalogues, return per-type lists of
    files that exist in the workspace root."""
    out: dict[str, list[str]] = {
        "python": [], "node": [], "rust": [], "go": [],
    }
    for kind, names in (
        ("python", PYTHON_INDICATORS),
        ("node", NODE_INDICATORS),
        ("rust", RUST_INDICATORS),
        ("go", GO_INDICATORS),
    ):
        for name in names:
            if (workspace / name).is_file():
                out[kind].append(name)
    return out


def detect_project_type(workspace: str | Path | None = None) -> ProjectInfo:
    """Classify the workspace. Returns ``ProjectInfo``.

    Resolution order:
      1. ``JANUS_PROJECT_TYPE`` env var (short-circuit, source="env")
      2. Auto-detection over manifest files in the workspace root.

    Workspaces that match multiple types → "mixed". No matches →
    "unknown".
    """
    if workspace is None:
        workspace = config.WORKSPACE
    workspace_path = Path(workspace).resolve() if workspace else Path(".")

    # Env override
    env_type = os.environ.get("JANUS_PROJECT_TYPE", "").strip().lower()
    if env_type:
        return ProjectInfo(
            type=env_type,
            indicators=[],
            test_command=TEST_COMMANDS.get(env_type, ""),
            workspace=str(workspace_path),
            source="env",
        )

    if not workspace_path.exists():
        return ProjectInfo(
            type="unknown", indicators=[], test_command="",
            workspace=str(workspace_path), source="auto",
        )

    found = _gather_indicators(workspace_path)
    matched_types = [k for k, files in found.items() if files]
    all_indicators: list[str] = []
    for k in matched_types:
        all_indicators.extend(f"{k}:{f}" for f in found[k])

    if not matched_types:
        ptype = "unknown"
    elif len(matched_types) == 1:
        ptype = matched_types[0]
    else:
        ptype = "mixed"

    return ProjectInfo(
        type=ptype,
        indicators=all_indicators,
        test_command=TEST_COMMANDS.get(ptype, ""),
        workspace=str(workspace_path),
        source="auto",
    )


# ---------- Rendering ----------


def render_prompt_block(info: ProjectInfo) -> str:
    """One-paragraph block for the system prompt.

    Returns "" when type is unknown — no point telling the model
    "we couldn't figure out what this is."
    """
    if not info.is_known:
        return ""
    lines = [f"## Project type: {info.type}"]
    if info.indicators:
        # Show the first 4 indicators only; the model doesn't need
        # the whole list, just enough to know we're not guessing.
        shown = info.indicators[:4]
        more = (
            f" (+{len(info.indicators) - 4} more)"
            if len(info.indicators) > 4 else ""
        )
        lines.append(f"Indicators: {', '.join(shown)}{more}")
    if info.test_command and info.test_command.startswith("("):
        # "(multiple)" / "(no project markers)" — informational only
        pass
    elif info.test_command:
        lines.append(f"Test command: `{info.test_command}`")
    return "\n".join(lines)


def render_summary(info: ProjectInfo) -> str:
    """ASCII summary for the /project slash command."""
    lines = []
    lines.append(f"  type: {info.type}  (source: {info.source})")
    lines.append(f"  workspace: {info.workspace}")
    if info.indicators:
        lines.append("  indicators:")
        for ind in info.indicators:
            lines.append(f"    {ind}")
    else:
        lines.append("  indicators: (none found)")
    if info.test_command:
        lines.append(f"  test command: {info.test_command}")
    return "\n".join(lines)


__all__ = [
    "ProjectInfo",
    "PYTHON_INDICATORS",
    "NODE_INDICATORS",
    "RUST_INDICATORS",
    "GO_INDICATORS",
    "TEST_COMMANDS",
    "detect_project_type",
    "render_prompt_block",
    "render_summary",
]
