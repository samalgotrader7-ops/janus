"""
branding.py — logo, version, prompt glyph, and banner rendering.

Single source of truth so the basic CLI, the rich CLI, the web UI, and
(future) bot greetings all look identical. Anything visual that should
be consistent across gateways lives here.

DESIGN — "Bifurcation":
A request enters the agent and forks into 2-3 candidate interpretations.
The user picks one. The logo encodes that architectural choice — the
thing that makes Janus different from Hermes/Claude Code/Codex CLI.

  ╱─►
●─┼─►
  ╲─►
"""

from __future__ import annotations
from dataclasses import dataclass


VERSION = "1.1"
TAGLINE = "intent-first · safety-first agent"

# The prompt glyph echoes the bifurcation arrows. ASCII-safe enough on
# any UTF-8 terminal; no special font required.
PROMPT_GLYPH = "›"

# Inline runtime markers. Mirror the logo arrows for consistency.
TOOL_CALL_ARROW = "→"
TOOL_OK = "✓"
TOOL_FAIL = "✗"
LEAF_START = "▸"

# Subtle hint — commands are discoverable via /help and tab-completion,
# not listed inline like a menu.
COMMANDS_HINT = "Type /help for available commands"


# Three-line logo. Indexed so callers can place adjacent text on a per-line basis.
LOGO_LINES = (
    "       ╱─►",
    "   ●──┼─►",
    "       ╲─►",
)


# Brand color — the magenta used in CLI ANSI escapes. Single source of truth
# so web UI / favicon / future surfaces stay visually consistent.
BRAND_COLOR = "#a020f0"


# ---------- SVG logo ----------
#
# Same Concept-B Bifurcation as the ASCII LOGO_LINES, vectorized.
# Uses `currentColor` so callers can theme via CSS `color:` property.
# Designed in a 32x32 viewBox; browsers scale crisply to any size.

_SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" \
fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round">
  <defs>
    <marker id="janus-arrow" viewBox="0 0 10 10" refX="9" refY="5" \
markerUnits="strokeWidth" markerWidth="4" markerHeight="4" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="{color}" stroke="none"/>
    </marker>
  </defs>
  <circle cx="6" cy="16" r="2.5" fill="{color}" stroke="none"/>
  <g marker-end="url(#janus-arrow)">
    <line x1="9" y1="16" x2="22" y2="7"/>
    <line x1="9" y1="16" x2="26" y2="16"/>
    <line x1="9" y1="16" x2="22" y2="25"/>
  </g>
</svg>"""


def svg_logo(color: str = "currentColor") -> str:
    """Return the SVG markup with the chosen stroke color.

    Use 'currentColor' for inline-in-page use (inherits CSS).
    Use a literal color (e.g. BRAND_COLOR) for favicons / detached uses.
    """
    return _SVG_TEMPLATE.format(color=color)


@dataclass
class BannerInputs:
    model: str
    cwd: str
    home: str
    tool_count: int
    skill_count: int
    mcp_count: int


def logo_with_titles(b: BannerInputs) -> list[tuple[str, str]]:
    """Return three (logo_line, side_text) tuples — caller colors and prints.

    Layout:
        ╱─►
      ●─┼─►   janus  v0.12
        ╲─►   intent-first · safety-first agent
    """
    return [
        (LOGO_LINES[0], ""),
        (LOGO_LINES[1], f"   janus  v{VERSION}"),
        (LOGO_LINES[2], f"   {TAGLINE}"),
    ]


def status_lines(b: BannerInputs) -> list[str]:
    """Return the per-line status block (no ANSI). Counts are right-aligned
    after the home path so the eye lands on them last."""
    counts = (
        f"{b.tool_count} tools · {b.skill_count} skills · {b.mcp_count} mcp"
    )
    return [
        f"   model    {b.model}",
        f"   cwd      {b.cwd}",
        f"   home     {b.home}     ·   {counts}",
    ]


def render_plain(b: BannerInputs) -> str:
    """No-color banner rendering. Used by tests; both CLIs colorize their
    own."""
    out: list[str] = [""]
    for logo, title in logo_with_titles(b):
        out.append(logo + title)
    out.append("")
    out.extend(status_lines(b))
    out.append("")
    out.append(f"   {COMMANDS_HINT}")
    return "\n".join(out)
