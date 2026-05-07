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


VERSION = "1.24.4"
TAGLINE = "intent-first · safety-first agent"


# ---------- v1.24.3: emoji-safe terminal rendering ----------
#
# When Janus runs under tmux/SSH with a misconfigured locale (LANG=C,
# missing UTF-8 in tmux's default-terminal, etc.) emojis come out as
# mojibake — UTF-8 bytes interpreted as Latin-1, sometimes doubly
# encoded by intermediate layers. Sam reported "Ã°ÂÂÂ" in place of
# "🎯" on his Ubuntu deploy.
#
# We can't fix the broken terminal from inside Janus, but we CAN:
#   1. Detect when the terminal is unlikely to render emoji correctly.
#   2. Provide an opt-out env var to force ASCII fallbacks.
#   3. Offer a glyph() helper that emits emoji-or-fallback consistently.

import os as _os
import sys as _sys


def _terminal_is_emoji_safe() -> bool:
    """Heuristic: is the current terminal likely to render 4-byte UTF-8?

    Returns True when stdout is bound to a UTF-8 encoder AND the locale
    looks reasonable. False when LANG/LC_ALL is C/POSIX/ANSI_X3.4-1968,
    or when stdout encoding is not UTF-8.
    """
    try:
        enc = (getattr(_sys.stdout, "encoding", None) or "").lower()
    except Exception:
        enc = ""
    if "utf" not in enc:
        return False
    lang = (
        _os.environ.get("LC_ALL")
        or _os.environ.get("LC_CTYPE")
        or _os.environ.get("LANG")
        or ""
    ).lower()
    if not lang:
        # Empty locale on POSIX often means C-default. Conservative no.
        return _os.name != "posix"
    if lang in ("c", "posix", "ansi_x3.4-1968"):
        return False
    return "utf" in lang or lang.startswith("en") or lang.startswith("en_")


def _emoji_disabled() -> bool:
    """True if the user explicitly disabled emoji output, or the
    terminal looks unsafe for emoji rendering."""
    flag = _os.environ.get("JANUS_NO_EMOJI", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # No explicit flag — auto-detect.
    return not _terminal_is_emoji_safe()


def glyph(emoji: str, ascii_fallback: str) -> str:
    """Return ``emoji`` when the terminal can render it, else
    ``ascii_fallback``.

    Use this for all user-facing output that contains 4-byte UTF-8
    glyphs (most colorful emoji). Single-codepoint pictographs in the
    BMP (✓ ✗ → ●) are usually safe in any UTF-8 terminal and don't
    need wrapping.

    Example:
        print(glyph("🎯", "->") + " interview mode on")

    The result on a healthy terminal: "🎯 interview mode on"
    On a Latin-1-leaking session: "-> interview mode on"
    """
    return ascii_fallback if _emoji_disabled() else emoji


def emoji_safe_text(text: str) -> str:
    """Strip 4-byte UTF-8 emoji from arbitrary text when the terminal
    can't render them. Conservative — only replaces common emoji
    ranges, leaves BMP characters (arrows, box-drawing, ✓ ✗) alone.
    """
    if not _emoji_disabled():
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        # Common emoji ranges (not exhaustive; we err on the side of
        # leaving text intact). Replace with empty string — ASCII
        # context (square brackets, labels) usually carries the meaning.
        if (
            0x1F300 <= cp <= 0x1FAFF   # Misc symbols, emoticons, transport
            or 0x2600 <= cp <= 0x27BF  # Misc symbols + dingbats
            or 0x2B00 <= cp <= 0x2BFF  # Misc symbols and arrows
        ):
            # Allow common BMP arrows that we use everywhere.
            if cp in (0x2192, 0x2190, 0x2191, 0x2193,  # ← ↑ ↓ →
                      0x2713, 0x2717,                   # ✓ ✗
                      0x25CF, 0x25B8, 0x25C2):          # ● ▸ ◂
                out.append(ch)
                continue
            # Otherwise drop.
            continue
        out.append(ch)
    return "".join(out)

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
