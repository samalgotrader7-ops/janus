"""Tests for the branding module — logo + banner rendering."""
from __future__ import annotations

from janus import branding


def test_logo_lines_match_concept_b():
    """Concept B = the bifurcation: a request enters and forks into 3
    interpretation arrows. Visual identity must not regress."""
    lines = branding.LOGO_LINES
    assert len(lines) == 3
    # First and last lines have a single arrow each.
    assert "╱─►" in lines[0]
    assert "╲─►" in lines[2]
    # Middle line has the hub + bifurcation.
    assert "●" in lines[1]
    assert "┼─►" in lines[1]


def test_prompt_glyph_is_single_char():
    """Prompt glyph should be one character so it doesn't cause width
    arithmetic issues in the prompt."""
    assert len(branding.PROMPT_GLYPH) == 1


def test_render_plain_includes_logo_status_and_commands():
    out = branding.render_plain(branding.BannerInputs(
        model="m",
        cwd="/tmp/x",
        home="/home/u/.janus",
        tool_count=23,
        skill_count=4,
        mcp_count=1,
    ))
    # Logo lines present.
    for line in branding.LOGO_LINES:
        assert line in out
    # Status visible.
    assert "model    m" in out
    assert "cwd      /tmp/x" in out
    assert "23 tools · 4 skills · 1 mcp" in out
    # Commands hint present.
    assert branding.COMMANDS_HINT in out
    # Tagline present.
    assert branding.TAGLINE in out


def test_logo_with_titles_pairs_logo_to_text():
    pairs = branding.logo_with_titles(branding.BannerInputs(
        model="m", cwd="/x", home="/y",
        tool_count=0, skill_count=0, mcp_count=0,
    ))
    assert len(pairs) == 3
    # Middle row carries the project name; bottom row carries the tagline.
    assert "janus" in pairs[1][1]
    assert branding.TAGLINE in pairs[2][1]


def test_status_lines_format_is_stable():
    """Smoke: keep the order model → cwd → home so the eye finds them."""
    lines = branding.status_lines(branding.BannerInputs(
        model="anthropic/claude-sonnet-4-6",
        cwd="/proj",
        home="/home/u/.janus",
        tool_count=23, skill_count=2, mcp_count=0,
    ))
    assert lines[0].startswith("   model")
    assert lines[1].startswith("   cwd")
    assert lines[2].startswith("   home")


# ---------- SVG logo ----------


def test_svg_logo_default_uses_currentColor():
    svg = branding.svg_logo()
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "currentColor" in svg


def test_svg_logo_with_explicit_color():
    svg = branding.svg_logo(branding.BRAND_COLOR)
    assert branding.BRAND_COLOR in svg
    # currentColor should be fully replaced.
    assert "currentColor" not in svg


def test_svg_logo_has_three_arrows():
    """Concept B's defining shape: hub circle + three lines fanning out."""
    svg = branding.svg_logo()
    # Three <line> elements (one per branch).
    assert svg.count("<line") == 3
    # One <circle> for the hub.
    assert svg.count("<circle") == 1
    # Marker reference for the arrowheads.
    assert "marker-end=\"url(#janus-arrow)\"" in svg


def test_brand_color_is_set():
    assert branding.BRAND_COLOR.startswith("#")
    assert len(branding.BRAND_COLOR) in (4, 7)  # #rgb or #rrggbb
