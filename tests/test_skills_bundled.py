"""Tests for the bundled-skill catalog distribution mechanism.

Two layers:

1. Mechanism tests (synthetic bundled dir via monkeypatch) — verify
   install_bundled, is_first_run, filter_skills behave correctly
   without depending on any real skills shipping yet.

2. Catalog tests (against the real janus/skills_bundled/) — every
   shipped .md must parse, declare a name + description, and land
   `state: quarantined` after install (P4 enforcement).
"""

from __future__ import annotations
import textwrap
from pathlib import Path

import pytest

from janus import skill_catalog, skills, config


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _example_skill_md(name: str, *, state: str = "quarantined") -> str:
    return textwrap.dedent(f"""\
        ---
        name: {name}
        description: Example {name} skill for tests.
        state: {state}
        capabilities:
          fs.read:
            - "**"
        created: 2026-05-03T00:00:00Z
        last-promoted: null
        runs: 0
        success: 0
        fail: 0
        ---

        You are running {name}.
        """)


@pytest.fixture
def fake_bundled(tmp_path, monkeypatch):
    """Replace bundled_dir() with a tmp dir we populate per-test."""
    fake = tmp_path / "fake_bundled"
    fake.mkdir()
    monkeypatch.setattr(skill_catalog, "bundled_dir", lambda: fake)
    return fake


# ----------------------------------------------------------------------
# Mechanism tests
# ----------------------------------------------------------------------


def test_install_bundled_no_op_on_empty_source(janus_home, fake_bundled):
    result = skill_catalog.install_bundled()
    assert result == {"installed": [], "skipped": [], "errors": []}


def test_install_bundled_copies_single_file(janus_home, fake_bundled):
    (fake_bundled / "demo-skill.md").write_text(_example_skill_md("demo-skill"),
                                                encoding="utf-8")
    result = skill_catalog.install_bundled()
    assert result["installed"] == ["demo-skill"]
    assert result["skipped"] == []
    assert result["errors"] == []
    s = skills.load("demo-skill")
    assert s is not None
    assert s.state == "quarantined"


def test_install_bundled_skips_existing(janus_home, fake_bundled):
    (fake_bundled / "demo.md").write_text(_example_skill_md("demo"),
                                          encoding="utf-8")
    skill_catalog.install_bundled()
    # Mutate the user's copy.
    target = config.SKILLS_DIR / "demo.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n# user edit\n",
                      encoding="utf-8")
    user_text = target.read_text(encoding="utf-8")
    # Re-install — must not clobber.
    result = skill_catalog.install_bundled()
    assert result["installed"] == []
    assert result["skipped"] == ["demo"]
    assert target.read_text(encoding="utf-8") == user_text


def test_install_bundled_force_overwrites(janus_home, fake_bundled):
    (fake_bundled / "demo.md").write_text(_example_skill_md("demo"),
                                          encoding="utf-8")
    skill_catalog.install_bundled()
    target = config.SKILLS_DIR / "demo.md"
    target.write_text("garbage", encoding="utf-8")
    result = skill_catalog.install_bundled(force=True)
    assert result["installed"] == ["demo"]
    s = skills.load("demo")
    assert s is not None
    assert s.state == "quarantined"


def test_install_bundled_force_quarantine_on_misconfigured_source(
    janus_home, fake_bundled
):
    """A bundled skill that wrongly declares trusted-auto MUST be re-quarantined."""
    (fake_bundled / "rogue.md").write_text(
        _example_skill_md("rogue", state="trusted-auto"),
        encoding="utf-8",
    )
    skill_catalog.install_bundled()
    s = skills.load("rogue")
    assert s is not None
    assert s.state == "quarantined", (
        "P4: bundled skills must land quarantined regardless of source state"
    )


def test_install_bundled_directory_form(janus_home, fake_bundled):
    skill_dir = fake_bundled / "dir-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(_example_skill_md("dir-skill"),
                                        encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "helper.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    result = skill_catalog.install_bundled()
    assert result["installed"] == ["dir-skill"]
    target = config.SKILLS_DIR / "dir-skill"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "helper.sh").is_file()


def test_install_bundled_records_marker(janus_home, fake_bundled):
    (fake_bundled / "demo.md").write_text(_example_skill_md("demo"),
                                          encoding="utf-8")
    assert skill_catalog.is_first_run() is True
    skill_catalog.install_bundled()
    assert skill_catalog.has_been_installed() is True
    assert skill_catalog.is_first_run() is False


def test_is_first_run_false_when_user_has_skills(janus_home, fake_bundled):
    # User dropped a skill themselves; never auto-install over them.
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    (config.SKILLS_DIR / "user-made.md").write_text(
        _example_skill_md("user-made"), encoding="utf-8"
    )
    assert skill_catalog.is_first_run() is False


def test_filter_skills_substring_match(janus_home):
    class S:
        def __init__(self, name, desc):
            self.name = name
            self.description = desc
    items = [
        S("git-pr", "Open a pull request"),
        S("flake-hunter", "Re-run failing tests to detect flakes"),
        S("cost-cartographer", "Map per-task LLM cost"),
    ]
    out = skill_catalog.filter_skills(items, "cost")
    assert [s.name for s in out] == ["cost-cartographer"]
    out2 = skill_catalog.filter_skills(items, "PR")
    assert [s.name for s in out2] == ["git-pr"]
    # Empty query passes through everything.
    out3 = skill_catalog.filter_skills(items, "")
    assert len(out3) == 3
    # No match returns empty.
    assert skill_catalog.filter_skills(items, "zzzzz") == []


def test_iter_bundled_sources_orders_alphabetically(janus_home, fake_bundled):
    (fake_bundled / "z-last.md").write_text(_example_skill_md("z-last"),
                                            encoding="utf-8")
    (fake_bundled / "a-first.md").write_text(_example_skill_md("a-first"),
                                             encoding="utf-8")
    (fake_bundled / "m-middle.md").write_text(_example_skill_md("m-middle"),
                                              encoding="utf-8")
    sources = skill_catalog.iter_bundled_sources()
    assert [p.stem for p in sources] == ["a-first", "m-middle", "z-last"]


def test_iter_bundled_skips_pycache_and_dotfiles(janus_home, fake_bundled):
    (fake_bundled / "__pycache__").mkdir()
    (fake_bundled / ".hidden.md").write_text(_example_skill_md(".hidden"),
                                             encoding="utf-8")
    (fake_bundled / "real.md").write_text(_example_skill_md("real"),
                                          encoding="utf-8")
    sources = skill_catalog.iter_bundled_sources()
    assert [p.name for p in sources] == ["real.md"]


# ----------------------------------------------------------------------
# Catalog tests — run against the REAL janus/skills_bundled/
# ----------------------------------------------------------------------


def _real_bundled_skills() -> list[Path]:
    """Every source file/dir under the real bundled dir."""
    return skill_catalog.iter_bundled_sources()


@pytest.mark.parametrize("source", _real_bundled_skills(),
                         ids=lambda p: p.name)
def test_real_bundled_skill_parses(source: Path):
    """Every shipped skill must parse + declare name + description."""
    if source.is_file():
        text = source.read_text(encoding="utf-8")
    else:
        text = (source / "SKILL.md").read_text(encoding="utf-8")
    fm, body = skills.parse_frontmatter(text)
    assert fm, f"{source.name} has no frontmatter"
    assert fm.get("name"), f"{source.name} missing 'name' field"
    assert fm.get("description"), f"{source.name} missing 'description' field"
    assert body.strip(), f"{source.name} has empty body"


@pytest.mark.parametrize("source", _real_bundled_skills(),
                         ids=lambda p: p.name)
def test_real_bundled_skill_declares_quarantined(source: Path):
    """P4 enforcement at the source: catalog must declare quarantined.

    Even though install_bundled re-quarantines on copy, declaring the
    correct state at the source is part of the catalog contract.
    """
    if source.is_file():
        text = source.read_text(encoding="utf-8")
    else:
        text = (source / "SKILL.md").read_text(encoding="utf-8")
    fm, _ = skills.parse_frontmatter(text)
    assert fm.get("state") == "quarantined", (
        f"{source.name}: bundled skills must declare state=quarantined "
        f"(got {fm.get('state')!r})"
    )


def test_install_real_catalog_into_janus_home(janus_home):
    """Smoke-install the real catalog and verify everything lands quarantined."""
    result = skill_catalog.install_bundled()
    # No errors regardless of catalog size.
    assert result["errors"] == [], f"install errors: {result['errors']}"
    # Everything we installed must be loadable + quarantined.
    for name in result["installed"]:
        # Directory-form vs file-form: try .md first, then dir/SKILL.md.
        s = skills.load(name)
        if s is None:
            dir_path = config.SKILLS_DIR / name / "SKILL.md"
            assert dir_path.is_file(), f"installed '{name}' not loadable"
            s = skills.load_path(dir_path)
        assert s.state == "quarantined", (
            f"{name} did not land quarantined (got {s.state!r})"
        )
