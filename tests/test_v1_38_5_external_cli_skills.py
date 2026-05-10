"""Tests for v1.38.5 — bundled skill catalog for external CLI wrappers.

Pin: each of the 4 new skills + the updated delegate-to-agent
parses cleanly and grants the right capability tokens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import skills


SKILLS_DIR = Path(skills.__file__).parent / "skills_bundled"

NEW_SKILLS = [
    "claude-code-handoff",
    "aider-refactor",
    "codex-cli-task",
    "gemini-cli-task",
]

EXPECTED_CAPABILITY_KEYS = {
    "claude-code-handoff": "external_cli.claude_code",
    "aider-refactor":      "external_cli.aider",
    "codex-cli-task":      "external_cli.codex_cli",
    "gemini-cli-task":     "external_cli.gemini_cli",
}


def _read_skill(name: str) -> tuple[dict, str]:
    p = SKILLS_DIR / f"{name}.md"
    assert p.is_file(), f"missing skill file: {p}"
    text = p.read_text(encoding="utf-8")
    return skills.parse_frontmatter(text)


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_skill_file_exists(name):
    p = SKILLS_DIR / f"{name}.md"
    assert p.is_file()


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_frontmatter_parses(name):
    fm, body = _read_skill(name)
    assert fm.get("name") == name
    assert fm.get("description"), f"{name} missing description"
    assert fm.get("state") == "quarantined", (
        f"{name} should ship as quarantined; user promotes manually"
    )
    assert body.strip(), f"{name} has empty body"


@pytest.mark.parametrize("name,expected_key", EXPECTED_CAPABILITY_KEYS.items())
def test_skill_grants_correct_capability(name, expected_key):
    fm, _ = _read_skill(name)
    caps = fm.get("capabilities") or {}
    assert expected_key in caps, (
        f"{name} should grant {expected_key} but only has: {list(caps)}"
    )
    grants = caps[expected_key]
    assert "exec" in grants, (
        f"{name}'s {expected_key} should include 'exec' target"
    )


@pytest.mark.parametrize("name", NEW_SKILLS)
def test_skill_capability_set_loads(name):
    """Pin: skills.py's CapabilitySet.from_dict accepts the
    frontmatter shape and produces a working set."""
    from janus.tools.capabilities import CapabilitySet
    fm, _ = _read_skill(name)
    caps = CapabilitySet.from_dict(fm.get("capabilities") or {})
    # Each new skill grants exec on its specific external_cli verb
    expected_verb = EXPECTED_CAPABILITY_KEYS[name].split(".", 1)[1]
    assert caps.grants("external_cli", expected_verb, "exec") is True
    # And does NOT grant the other 3 wrappers
    others = {"claude_code", "aider", "codex_cli", "gemini_cli"} - {expected_verb}
    for other in others:
        assert caps.grants("external_cli", other, "exec") is False, (
            f"{name} accidentally grants {other}"
        )


# ---------- updated delegate-to-agent ----------


def test_delegate_to_agent_grants_all_four_wrappers():
    """Pin: the multi-agent skill grants all 4 first-class wrappers
    (Devin still uses shell.exec since we don't wrap it)."""
    fm, body = _read_skill("delegate-to-agent")
    caps = fm.get("capabilities") or {}
    for verb_key in (
        "external_cli.claude_code",
        "external_cli.aider",
        "external_cli.codex_cli",
        "external_cli.gemini_cli",
    ):
        assert verb_key in caps, f"missing {verb_key}"
        assert "exec" in caps[verb_key]


def test_delegate_to_agent_keeps_devin_shell_grant():
    fm, _ = _read_skill("delegate-to-agent")
    caps = fm.get("capabilities") or {}
    assert "shell.exec" in caps
    # Devin grant retained
    assert any("devin" in g.lower() for g in caps["shell.exec"])


def test_delegate_to_agent_body_mentions_first_class_tools():
    """Pin: skill body refs the new tool names so the model knows
    to prefer them over shell.exec for these CLIs."""
    _, body = _read_skill("delegate-to-agent")
    body_lower = body.lower()
    for tool in ("claude_code", "aider", "codex_cli", "gemini_cli"):
        assert tool in body_lower, f"body should mention {tool}"


# ---------- version ----------


def test_version_bumped_to_1_38_5():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 5)
