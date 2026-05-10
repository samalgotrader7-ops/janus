"""Tests for the public-launch README.

Originally shipped in v1.32.5 (cold-reader polish on the old README
shape with TOC). v1.34.5 rewrote README for public launch with new
structure (banner image, mermaid diagram, no TOC). This test now
pins the v1.34.5 invariants.

INVARIANTS PINNED:
  * 60-second quickstart section present
  * pipx install + Docker + one-line installer paths all documented
  * Tutorials section links to tutorials/01-04
  * Production deployment section mentions install_services.sh +
    systemd + linger + post-merge hook
  * Banner / logo asset referenced
  * Mermaid architecture diagram present
  * 'Self-improving' framing per Option C
  * License section present
"""

from __future__ import annotations

from pathlib import Path

import pytest


README_PATH = Path(__file__).parent.parent / "README.md"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


# -------------------- Hero / branding --------------------


def test_readme_references_banner_or_logo(readme_text):
    """Hero block at the top should reference the SVG banner / logo
    asset so GitHub renders it visually."""
    assert "assets/banner.svg" in readme_text or "assets/logo.svg" in readme_text


def test_readme_has_self_improving_framing(readme_text):
    """Per Option C, the lead description is 'self-improving AI
    agent for developers'. Pin this so future polish doesn't
    accidentally regress to the old positioning."""
    assert "self-improving" in readme_text.lower()


def test_readme_has_three_gateways_callout(readme_text):
    """Three-surface story is core to Janus's positioning."""
    assert "CLI" in readme_text
    assert "Telegram" in readme_text
    assert "Web" in readme_text


def test_readme_has_badges(readme_text):
    """Standard public-repo badges (PyPI version + license + Python)."""
    assert "img.shields.io" in readme_text


# -------------------- Quickstart --------------------


def test_readme_has_quickstart_section(readme_text):
    assert "60-second quickstart" in readme_text


def test_quickstart_has_three_install_configure_chat_steps(readme_text):
    """Quickstart shows install → configure → chat path."""
    qs_idx = readme_text.index("60-second quickstart")
    block = readme_text[qs_idx: qs_idx + 1500]
    # Three concrete commands
    assert "pipx install janus-agent" in block or "install.sh" in block
    assert "janus onboard" in block
    # Followed by `janus` to start chat
    assert "\njanus\n" in block or "# 3." in block.lower() or "3. Chat" in block


# -------------------- Install paths --------------------


def test_readme_has_pypi_install_path(readme_text):
    """`pipx install janus-agent` is the canonical path now that
    the package is on PyPI."""
    assert "pipx install janus-agent" in readme_text


def test_readme_has_docker_install_path(readme_text):
    """Docker install instructions present."""
    assert "ghcr.io/samalgotrader7-ops/janus" in readme_text
    assert "docker compose up" in readme_text or "docker run" in readme_text


def test_readme_has_one_line_installer(readme_text):
    """The curl-pipe one-liner."""
    assert "curl -sSL" in readme_text
    assert "install.sh" in readme_text


def test_readme_has_from_source_path(readme_text):
    """Contributors install via `pipx install -e`."""
    assert "git clone" in readme_text
    assert "pipx install -e" in readme_text


# -------------------- Tutorials --------------------


def test_readme_has_tutorials_section(readme_text):
    """Tutorials section links to all four files at the new
    public-friendly tutorials/ path (was docs/tutorials/ before
    the public-launch repo cleanup)."""
    assert "## Tutorials" in readme_text
    tut_idx = readme_text.index("## Tutorials")
    block = readme_text[tut_idx: tut_idx + 1500]
    for f in (
        "tutorials/01-hello-janus.md",
        "tutorials/02-your-first-skill.md",
        "tutorials/03-memory-loop.md",
        "tutorials/04-connect-mcp.md",
    ):
        assert f in block, f"Tutorials section missing link to {f}"


# -------------------- Production deployment --------------------


def test_readme_has_production_section(readme_text):
    assert "## Production deployment" in readme_text


def test_production_section_mentions_install_services(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    block = readme_text[prod_idx: prod_idx + 2500]
    assert "install_services.sh" in block


def test_production_section_mentions_systemd_and_linger(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    block = readme_text[prod_idx: prod_idx + 2500]
    assert "systemd" in block
    assert "linger" in block


def test_production_section_mentions_caddy_or_nginx(readme_text):
    prod_idx = readme_text.index("## Production deployment")
    block = readme_text[prod_idx: prod_idx + 2500]
    assert "caddy" in block.lower() or "nginx" in block.lower()


# -------------------- Architecture diagram --------------------


def test_readme_has_mermaid_diagram(readme_text):
    """Mermaid diagram showing user → gateways → agent loop."""
    assert "```mermaid" in readme_text
    # Pin that gateways + agent loop are referenced in the diagram
    mermaid_idx = readme_text.index("```mermaid")
    block = readme_text[mermaid_idx: mermaid_idx + 1500]
    # All three gateways
    assert "CLI" in block
    assert "Telegram" in block
    assert "Web" in block


# -------------------- Footer / license --------------------


def test_readme_has_license_section(readme_text):
    assert "## License" in readme_text
    assert "MIT" in readme_text
