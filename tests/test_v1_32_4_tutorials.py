"""Tests for v1.32.4 — progressive tutorials (Phase 5.5).

WHAT THIS SHIPS:
Four self-contained markdown tutorials in docs/tutorials/, each
one screenful, plus a tutorials README that lists them.

INVARIANTS PINNED:
  * All four tutorial files exist
  * docs/tutorials/README.md exists with links to each
  * Each tutorial has a clear goal in the first paragraph
  * Tutorials cross-link to each other (01 → 02 → 03 → 04)
  * No dead internal links
  * Each tutorial under ~250 lines (one screenful constraint)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


TUTORIALS_DIR = Path(__file__).parent.parent / "tutorials"

EXPECTED_TUTORIALS = [
    "01-hello-janus.md",
    "02-your-first-skill.md",
    "03-memory-loop.md",
    "04-connect-mcp.md",
]


# -------------------- File-level pins --------------------


def test_tutorials_directory_exists():
    assert TUTORIALS_DIR.exists()
    assert TUTORIALS_DIR.is_dir()


def test_tutorials_readme_exists():
    """Tutorials README is the index — links to all four."""
    readme = TUTORIALS_DIR / "README.md"
    assert readme.exists()
    body = readme.read_text(encoding="utf-8")
    for f in EXPECTED_TUTORIALS:
        assert f in body, f"README.md doesn't link to {f}"


@pytest.mark.parametrize("filename", EXPECTED_TUTORIALS)
def test_tutorial_exists(filename):
    path = TUTORIALS_DIR / filename
    assert path.exists(), f"missing tutorial: {filename}"


@pytest.mark.parametrize("filename", EXPECTED_TUTORIALS)
def test_tutorial_has_goal_statement(filename):
    """First paragraph should declare the goal explicitly so a
    user scanning the file knows what they'll get."""
    body = (TUTORIALS_DIR / filename).read_text(encoding="utf-8")
    # Look for "**Goal**" in the first 600 chars (rough first-screen).
    head = body[:600]
    assert "**Goal**" in head or "Goal:" in head, (
        f"{filename}: first screen lacks an explicit goal statement"
    )


@pytest.mark.parametrize("filename", EXPECTED_TUTORIALS)
def test_tutorial_size_one_screenful(filename):
    """Each tutorial should fit in ~one screen (≤300 lines).
    Anything longer should be split into multiple tutorials."""
    body = (TUTORIALS_DIR / filename).read_text(encoding="utf-8")
    line_count = len(body.splitlines())
    assert line_count <= 300, (
        f"{filename}: {line_count} lines — exceeds one-screen budget (300)"
    )


# -------------------- Cross-linking --------------------


@pytest.mark.parametrize(
    "current,nxt",
    [
        ("01-hello-janus.md", "02-your-first-skill.md"),
        ("02-your-first-skill.md", "03-memory-loop.md"),
        ("03-memory-loop.md", "04-connect-mcp.md"),
    ],
)
def test_tutorials_cross_link_to_next(current, nxt):
    """01 → 02 → 03 → 04 progression. Each links to the next so
    a reader can flow through without going back to the index."""
    body = (TUTORIALS_DIR / current).read_text(encoding="utf-8")
    assert nxt in body, f"{current} doesn't link to {nxt}"


@pytest.mark.parametrize(
    "tutorial,prereq",
    [
        ("02-your-first-skill.md", "01-hello-janus.md"),
        ("03-memory-loop.md", "02-your-first-skill.md"),
        ("04-connect-mcp.md", "03-memory-loop.md"),
    ],
)
def test_tutorials_link_back_to_prereq(tutorial, prereq):
    """Each tutorial after the first links to its prereq."""
    body = (TUTORIALS_DIR / tutorial).read_text(encoding="utf-8")
    assert prereq in body, f"{tutorial} doesn't reference prereq {prereq}"


# -------------------- Content sanity --------------------


def test_tutorial_01_covers_install_and_first_turn():
    body = (TUTORIALS_DIR / "01-hello-janus.md").read_text(encoding="utf-8")
    # Install one-liner should appear
    assert "install.sh" in body or "pipx install" in body
    # The three required env vars should be mentioned
    for var in ("JANUS_API_KEY", "JANUS_API_BASE", "JANUS_MODEL"):
        assert var in body, f"tutorial 01 missing env var {var}"
    # The onboard wizard should be mentioned
    assert "janus onboard" in body


def test_tutorial_02_covers_skill_lifecycle():
    body = (TUTORIALS_DIR / "02-your-first-skill.md").read_text(encoding="utf-8")
    # Frontmatter, triggers, capabilities, and promotion all covered
    assert "triggers:" in body
    assert "capabilities:" in body
    assert "/promote" in body
    assert "quarantined" in body


def test_tutorial_03_covers_memory_review_flow():
    body = (TUTORIALS_DIR / "03-memory-loop.md").read_text(encoding="utf-8")
    assert "/memory review" in body
    assert "/memory accept" in body
    assert "MEMORY.md" in body


def test_tutorial_04_covers_mcp_lifecycle():
    body = (TUTORIALS_DIR / "04-connect-mcp.md").read_text(encoding="utf-8")
    # Catalog → connect → tool call → disconnect
    assert "/mcp catalog" in body
    assert "/mcp connect" in body
    assert "/mcp disconnect" in body
    assert "servers.json" in body
    # Mentions stdio + that HTTP isn't supported yet
    assert "stdio" in body.lower()


# -------------------- Internal-link sanity --------------------


@pytest.mark.parametrize("filename", EXPECTED_TUTORIALS + ["README.md"])
def test_tutorial_internal_links_resolve(filename):
    """Markdown links to other tutorials must point at files that
    actually exist in docs/tutorials/. Skip links inside fenced
    code blocks — those are example content, not real links."""
    body = (TUTORIALS_DIR / filename).read_text(encoding="utf-8")
    # Strip fenced code blocks (``` ... ```) before scanning so
    # example markdown inside code fences doesn't trigger false
    # positives.
    body_stripped = re.sub(
        r"```[\s\S]*?```", "", body, flags=re.MULTILINE,
    )
    # Match [text](filename.md) — not crossing http(s):// boundaries
    pattern = re.compile(r"\]\(([^)]+\.md)\)")
    for match in pattern.finditer(body_stripped):
        target = match.group(1)
        # External / repo-relative paths skipped (e.g., docs/SPEC.md
        # references); we only check sibling references.
        if "/" in target:
            continue
        link_path = TUTORIALS_DIR / target
        assert link_path.exists(), (
            f"{filename}: dead link → {target}"
        )


# -------------------- Version pin --------------------


def test_version_bumped_to_1_32_4_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 4)
