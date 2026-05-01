"""
output_styles.py — render the agent's final output for the gateway (Phase 15).

STYLES:
  markdown — preserve formatting; cli_rich passes through `rich.Markdown`.
  plain    — strip nothing, no rendering.
  terse    — first paragraph only.
  json     — wrap in {"output": "..."} for piping into scripts.

Set via `/output-style <name>` mid-session, or via env JANUS_OUTPUT_STYLE.
"""

from __future__ import annotations
import json

VALID = ("markdown", "plain", "terse", "json")
DEFAULT = "markdown"


def normalize(style: str) -> str:
    style = (style or "").strip().lower()
    return style if style in VALID else DEFAULT


def render(output: str, style: str = DEFAULT) -> str:
    style = normalize(style)
    if style == "plain" or style == "markdown":
        return output
    if style == "terse":
        # Up to the first blank-line break.
        return (output or "").split("\n\n", 1)[0].strip()
    if style == "json":
        return json.dumps({"output": output}, ensure_ascii=False)
    return output
