from janus import skills


SKILL_TEXT = """---
name: git-pr
description: Create a PR from current branch.
state: quarantined
capabilities:
  shell.exec:
    - "git *"
    - "gh pr *"
  fs.read:
    - "**"
created: 2026-04-30T00:00:00Z
last-promoted: null
runs: 0
---

You are running git-pr.

Steps:
1. git status
2. draft PR
"""


def _write_skill(janus_home, name, text):
    d = janus_home / "skills"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name + ".md")
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_frontmatter():
    fm, body = skills.parse_frontmatter(SKILL_TEXT)
    assert fm["name"] == "git-pr"
    assert fm["state"] == "quarantined"
    assert fm["capabilities"]["shell.exec"] == ["git *", "gh pr *"]
    assert "git status" in body


def test_load_save_roundtrip(janus_home):
    _write_skill(janus_home, "git-pr", SKILL_TEXT)
    s = skills.load("git-pr")
    assert s is not None
    assert s.name == "git-pr"
    assert s.capabilities.grants("shell", "exec", "git status")
    assert not s.capabilities.grants("shell", "exec", "rm -rf /")
    s.runs = 7
    skills.save(s)
    s2 = skills.load("git-pr")
    assert s2.runs == 7


def test_promotion(janus_home):
    _write_skill(janus_home, "git-pr", SKILL_TEXT)
    s = skills.promote("git-pr", "trusted-supervised")
    assert s.state == "trusted-supervised"
    assert s.last_promoted is not None


def test_promotion_invalid(janus_home):
    _write_skill(janus_home, "git-pr", SKILL_TEXT)
    try:
        skills.promote("git-pr", "nonsense")
    except skills.PromotionError:
        return
    raise AssertionError("expected PromotionError")


def test_match_ranks_by_overlap(janus_home):
    _write_skill(janus_home, "git-pr", SKILL_TEXT)
    other = SKILL_TEXT.replace("git-pr", "py-test").replace(
        "Create a PR from current branch.",
        "Run pytest and report failures.",
    )
    _write_skill(janus_home, "py-test", other)

    matches = skills.match("create a PR for the current branch")
    assert matches
    assert matches[0].name == "git-pr"


def test_write_draft_avoids_overwrite(janus_home):
    draft = {
        "name": "demo",
        "description": "x",
        "capabilities": {"fs.read": ["**"]},
        "body": "step 1: do thing",
    }
    p1 = skills.write_draft(draft)
    p2 = skills.write_draft(draft)
    assert p1 != p2
    assert p1.exists() and p2.exists()
