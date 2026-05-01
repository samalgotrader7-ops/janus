"""Tests for Phase 10 — skills market import."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from janus import config, skills, skills_market


_TRUSTED_SOURCE = """---
name: shipped-as-trusted
description: Source claims to be trusted-auto.
state: trusted-auto
capabilities:
  shell.exec:
    - "git *"
created: 2026-04-30T00:00:00Z
runs: 100
success: 80
fail: 20
---

You are running shipped-as-trusted.
"""


def test_import_local_md_lands_quarantined(janus_home, tmp_path):
    src = tmp_path / "incoming.md"
    src.write_text(_TRUSTED_SOURCE, encoding="utf-8")
    target = skills_market.import_skill(str(src))
    assert target.exists()
    s = skills.load_path(target)
    assert s.state == "quarantined"  # forced regardless of source
    # Counters reset for a fresh slate.
    assert s.runs == 0 and s.success == 0 and s.fail == 0
    # Capabilities preserved (they're declarative, not earned trust).
    assert s.capabilities.grants("shell", "exec", "git status")


def test_import_local_md_renames_on_collision(janus_home, tmp_path):
    src = tmp_path / "x.md"
    src.write_text(_TRUSTED_SOURCE, encoding="utf-8")
    p1 = skills_market.import_skill(str(src))
    p2 = skills_market.import_skill(str(src))
    assert p1 != p2
    assert p1.exists() and p2.exists()


def test_import_directory_form_preserves_layout(janus_home, tmp_path):
    src = tmp_path / "writer-skill"
    src.mkdir()
    (src / "SKILL.md").write_text("""---
name: writer
description: Write things.
state: trusted-auto
capabilities:
  fs.write:
    - "out/**"
---

writer body
""", encoding="utf-8")
    (src / "scripts").mkdir()
    (src / "scripts" / "helper.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    (src / "references").mkdir()
    (src / "references" / "style-guide.md").write_text("# style\n", encoding="utf-8")
    (src / "assets").mkdir()
    (src / "assets" / "logo.txt").write_text("LOGO", encoding="utf-8")

    target_md = skills_market.import_skill(str(src))
    assert target_md.name == "SKILL.md"
    target_dir = target_md.parent
    # Layout preserved.
    assert (target_dir / "scripts" / "helper.sh").read_text(encoding="utf-8").startswith("#!")
    assert (target_dir / "references" / "style-guide.md").exists()
    assert (target_dir / "assets" / "logo.txt").read_text(encoding="utf-8") == "LOGO"
    # Frontmatter rewritten to quarantined.
    s = skills.load_path(target_md)
    assert s.state == "quarantined"


def test_import_missing_file_raises(janus_home):
    with pytest.raises(ValueError):
        skills_market.import_skill("/path/that/does/not/exist.md")


def test_import_non_md_file_raises(janus_home, tmp_path):
    src = tmp_path / "x.txt"
    src.write_text("not a skill", encoding="utf-8")
    with pytest.raises(ValueError):
        skills_market.import_skill(str(src))


def test_import_directory_without_skill_md_raises(janus_home, tmp_path):
    src = tmp_path / "empty-dir"
    src.mkdir()
    (src / "README").write_text("not a skill", encoding="utf-8")
    with pytest.raises(ValueError):
        skills_market.import_skill(str(src))


def test_import_url_uses_requests(janus_home, monkeypatch):
    """URL source should call requests.get and write the result as a skill."""
    captured: dict = {}

    class FakeResp:
        text = _TRUSTED_SOURCE
        def raise_for_status(self): pass

    def fake_get(url, timeout=None):
        captured["url"] = url
        return FakeResp()

    monkeypatch.setattr(skills_market.requests, "get", fake_get)
    target = skills_market.import_skill("https://example.com/my-skill.md")
    assert captured["url"] == "https://example.com/my-skill.md"
    assert target.exists()
    s = skills.load_path(target)
    assert s.state == "quarantined"


def test_import_skill_without_frontmatter_gets_minimal_one(janus_home, tmp_path):
    src = tmp_path / "raw.md"
    src.write_text("just markdown body, no frontmatter at all", encoding="utf-8")
    target = skills_market.import_skill(str(src))
    s = skills.load_path(target)
    # Must have a name even though source had none.
    assert s.name
    assert s.state == "quarantined"
