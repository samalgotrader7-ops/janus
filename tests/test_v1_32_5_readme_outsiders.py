"""Tests for v1.32.5 — README polish for cold readers (Phase 5.6).

WHAT THIS SHIPS:
README additions for users who haven't seen Janus before:
  * "60-second quickstart" section right after the value-prop bullets
  * "Easiest — one-line installer" path leads the install section
  * "Docker (any platform)" path with compose stack
  * "Tutorials" section linking to docs/tutorials/01-04
  * "Production deployment" section with the install_services.sh
    + systemd workflow

INVARIANTS PINNED:
  * 60-second quickstart appears before the table of contents
  * One-line installer appears in install section
  * Docker option documented
  * Tutorials cross-linked from README
  * Production deployment section mentions install_services.sh
  * Production section mentions systemd, linger, post-merge hook
"""

from __future__ import annotations

from pathlib import Path

import pytest


README_PATH = Path(__file__).parent.parent / "README.md"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


# -------------------- 60-second quickstart --------------------


def test_readme_has_quickstart_section(readme_text):
    """60-second quickstart appears prominently — gives a cold
    reader the install + configure + run path in one block."""
    assert "60-second quickstart" in readme_text


def test_quickstart_appears_before_toc(readme_text):
    """Quickstart should be ABOVE the table of contents — first
    thing a reader sees after the value-prop bullets."""
    quickstart_idx = readme_text.index("60-second quickstart")
    toc_idx = readme_text.index("Table of contents")
    assert quickstart_idx < toc_idx


def test_quickstart_has_three_steps(readme_text):
    """1. install   2. configure   3. chat"""
    quickstart_idx = readme_text.index("60-second quickstart")
    block = readme_text[quickstart_idx: quickstart_idx + 1500]
    assert "# 1." in block or "1. Install" in block
    assert "# 2." in block or "2. Configure" in block
    assert "# 3." in block or "3. Chat" in block
    # Three concrete commands
    assert "install.sh" in block
    assert "janus onboard" in block


# -------------------- Install section paths --------------------


def test_readme_has_one_line_installer_path(readme_text):
    """The install.sh one-liner is the first install path users
    see in the Install section."""
    install_idx = readme_text.index("## Install")
    install_block = readme_text[install_idx: install_idx + 3000]
    assert "Easiest" in install_block or "one-line" in install_block.lower()
    assert "install.sh" in install_block


def test_readme_has_docker_path(readme_text):
    """Docker is the cross-platform install path. Both standalone
    docker run AND docker compose covered."""
    install_idx = readme_text.index("## Install")
    install_block = readme_text[install_idx: install_idx + 3000]
    assert "Docker" in install_block
    assert "ghcr.io/samalgotrader7-ops/janus" in install_block
    assert "docker compose up" in install_block


def test_readme_keeps_from_source_path(readme_text):
    """Existing 'from source' (clone + venv + pip install -e) path
    stays available for contributors. Don't drop it — just demote."""
    install_idx = readme_text.index("## Install")
    install_block = readme_text[install_idx: install_idx + 5000]
    assert "From source" in install_block or "git clone" in install_block


# -------------------- Tutorials section --------------------


def test_readme_has_tutorials_section(readme_text):
    """Tutorials section should be its own heading, linking to
    each of the 4 tutorial files."""
    assert "## Tutorials" in readme_text
    tut_idx = readme_text.index("## Tutorials")
    tut_block = readme_text[tut_idx: tut_idx + 1500]
    for f in (
        "docs/tutorials/01-hello-janus.md",
        "docs/tutorials/02-your-first-skill.md",
        "docs/tutorials/03-memory-loop.md",
        "docs/tutorials/04-connect-mcp.md",
    ):
        assert f in tut_block


def test_tutorials_in_table_of_contents(readme_text):
    """TOC mentions Tutorials so readers can jump to it."""
    toc_idx = readme_text.index("## Table of contents")
    toc_block = readme_text[toc_idx: toc_idx + 1500]
    assert "Tutorials" in toc_block


# -------------------- Production deployment section --------------------


def test_readme_has_production_section(readme_text):
    """Production deployment section covers the systemd path so
    VPS users find it without digging into scripts/."""
    assert "## Production deployment" in readme_text


def test_production_section_mentions_install_services(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    prod_block = readme_text[prod_idx: prod_idx + 2500]
    assert "install_services.sh" in prod_block


def test_production_section_mentions_systemd_and_linger(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    prod_block = readme_text[prod_idx: prod_idx + 2500]
    assert "systemd" in prod_block
    assert "linger" in prod_block


def test_production_section_mentions_post_merge_hook(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    prod_block = readme_text[prod_idx: prod_idx + 2500]
    # Hook OR core.hooksPath OR auto-restart phrasing
    assert "post-merge" in prod_block.lower() or \
        "core.hooksPath" in prod_block or \
        "auto-restart" in prod_block.lower()


def test_production_section_in_table_of_contents(readme_text):
    toc_idx = readme_text.index("## Table of contents")
    toc_block = readme_text[toc_idx: toc_idx + 1500]
    assert "Production deployment" in toc_block


# -------------------- Sanity --------------------


def test_readme_still_has_value_props(readme_text):
    """Make sure the polish didn't drop the bullets that explain
    WHY anyone should care."""
    assert "Model-agnostic" in readme_text
    assert "Plain-text everywhere" in readme_text or "Plain-text" in readme_text
    assert "Skills you teach it" in readme_text or "skills" in readme_text.lower()


# -------------------- Version pin --------------------


def test_version_bumped_to_1_32_5_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 5)
