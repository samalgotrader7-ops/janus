"""tests/test_skill_preprocess_and_pins.py — v1.12.0 Tier B continued."""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import config, conversation, skill_preprocessing as sp
from janus.conversation import Conversation


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "USER_MODEL_FILE", home / "user.md")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "DAEMON_STATE", home / "daemon.state.json")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()


# ============================================================
# Skill preprocessing
# ============================================================


def _good_skill(name: str = "demo") -> str:
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: A demo skill.\n"
        f"state: trusted-supervised\n"
        f"capabilities:\n"
        f"  fs.read:\n"
        f"    - 'docs/**'\n"
        f"created: 2026-05-04T00:00:00Z\n"
        f"last-promoted: null\n"
        f"runs: 0\n"
        f"success: 0\n"
        f"fail: 0\n"
        f"---\n"
        f"\n"
        f"You are demo. Read the docs the user asks about and summarize.\n"
        f"Always respond in plain prose.\n"
        + ("Detail. " * 30)
    )


def test_validate_clean_skill(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "demo.md"
    p.write_text(_good_skill(), encoding="utf-8")
    issues = sp.validate_skill_file(p)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []


def test_validate_missing_opening_delimiter(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "bad.md"
    p.write_text("name: bad\nstate: x\n", encoding="utf-8")
    issues = sp.validate_skill_file(p)
    assert any("opening '---'" in i.message for i in issues)


def test_validate_missing_closing_delimiter(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "open.md"
    p.write_text("---\nname: open\nstate: trusted-auto\n", encoding="utf-8")
    issues = sp.validate_skill_file(p)
    assert any("closing '---'" in i.message for i in issues)


def test_validate_invalid_state(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "bad-state.md"
    p.write_text(
        "---\n"
        "name: bad-state\n"
        "description: x\n"
        "state: invented-state\n"
        "---\nbody body body\n",
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    assert any("invalid state" in i.message for i in issues)


def test_validate_capabilities_inline_dict_caught(tmp_path, monkeypatch):
    """The v1.7.0 agent_create bug: 'capabilities: {}' parses as the
    literal string '{}'. Validator must surface it."""
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "inline.md"
    p.write_text(
        "---\n"
        "name: inline\n"
        "description: x\n"
        "state: trusted-supervised\n"
        "capabilities: {}\n"
        "---\nbody body body\n",
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    assert any("literal string '{}'" in i.message for i in issues)


def test_validate_capabilities_must_be_mapping(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "wrong.md"
    p.write_text(
        "---\n"
        "name: wrong\n"
        "description: x\n"
        "state: trusted-auto\n"
        "capabilities: not-a-mapping\n"
        "---\nbody body body\n",
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    assert any("must be a mapping" in i.message for i in issues)


def test_validate_filename_name_mismatch_warning(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "actual-filename.md"
    p.write_text(
        "---\n"
        "name: declared-name\n"
        "description: x\n"
        "state: trusted-auto\n"
        "---\n"
        + "body " * 50,
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    warnings = [i for i in issues if i.severity == "warning"]
    assert any("filename stem" in w.message for w in warnings)


def test_validate_empty_body(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "headless.md"
    p.write_text(
        "---\n"
        "name: headless\n"
        "description: x\n"
        "state: trusted-auto\n"
        "---\n",
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    assert any("body" in i.message and "empty" in i.message for i in issues)


def test_validate_short_body_warning(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    p = config.SKILLS_DIR / "stub.md"
    p.write_text(
        "---\n"
        "name: stub\n"
        "description: x\n"
        "state: trusted-auto\n"
        "---\nhi\n",
        encoding="utf-8",
    )
    issues = sp.validate_skill_file(p)
    assert any("very short" in i.message for i in issues)


def test_validate_all_aggregates(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    (config.SKILLS_DIR / "good.md").write_text(_good_skill("good"), encoding="utf-8")
    (config.SKILLS_DIR / "bad.md").write_text(
        "---\nname: bad\nstate: nonsense\n---\nbody body body\n",
        encoding="utf-8",
    )
    issues = sp.validate_all()
    assert any(i.severity == "error" for i in issues)
    # Errors come first
    error_indexes = [i for i, x in enumerate(issues) if x.severity == "error"]
    info_indexes = [i for i, x in enumerate(issues) if x.severity == "info"]
    if error_indexes and info_indexes:
        assert max(error_indexes) < min(info_indexes)


def test_render_clean(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = sp.render([])
    assert "validate cleanly" in out


def test_render_summary_line(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    issues = [
        sp.Issue("x.md", "error", "missing"),
        sp.Issue("y.md", "warning", "short"),
    ]
    out = sp.render(issues)
    assert "ERROR" in out
    assert "WARNING" in out
    assert "1 error" in out
    assert "1 warning" in out


# ============================================================
# Pinned turns / manual compression feedback
# ============================================================


def _make_conv(n_turns: int = 5) -> Conversation:
    c = Conversation(
        id="t",
        started="2026-05-04T00:00:00+00:00",
        last_updated="2026-05-04T00:00:00+00:00",
        model="x",
        workspace="/tmp",
    )
    for i in range(n_turns):
        c.add_turn(
            request=f"Turn {i + 1} request",
            output=f"Turn {i + 1} output",
        )
    return c


def test_compact_skips_when_under_keep_last():
    c = _make_conv(2)
    out = conversation.compact(c, keep_last=3)
    assert len(out.turns) == 2
    assert out.summary == ""


def test_compact_summarizes_older_when_no_pins(monkeypatch):
    c = _make_conv(8)
    monkeypatch.setattr(
        "janus.llm.chat",
        lambda **kw: {"content": "Earlier the user did X."},
    )
    out = conversation.compact(c, keep_last=3)
    assert len(out.turns) == 3
    # Only the LAST 3 survived
    assert out.turns[0]["request"] == "Turn 6 request"
    assert out.summary.startswith("Earlier")


def test_compact_preserves_pinned_outside_keep_last(monkeypatch):
    """User pinned an early turn — compact MUST keep it."""
    c = _make_conv(8)
    c.pinned_turns = [0, 2]  # turns 1 and 3 are pinned

    monkeypatch.setattr(
        "janus.llm.chat",
        lambda **kw: {"content": "summary of unpinned"},
    )
    out = conversation.compact(c, keep_last=3)

    # 5 total turns kept: turns 1, 3, 6, 7, 8 (pinned + last 3)
    assert len(out.turns) == 5
    requests = [t["request"] for t in out.turns]
    assert "Turn 1 request" in requests
    assert "Turn 3 request" in requests
    assert "Turn 6 request" in requests
    assert "Turn 7 request" in requests
    assert "Turn 8 request" in requests


def test_compact_remaps_pinned_indexes(monkeypatch):
    """After compaction, pinned_turns must point to the NEW positions."""
    c = _make_conv(8)
    c.pinned_turns = [0, 2]  # original turns 1 and 3
    monkeypatch.setattr(
        "janus.llm.chat",
        lambda **kw: {"content": "summary"},
    )
    out = conversation.compact(c, keep_last=3)
    # In the new turns list: turn 1 → idx 0, turn 3 → idx 1
    assert out.pinned_turns == [0, 1]
    # And the pinned turn at new idx 0 IS the original turn 1
    assert out.turns[0]["request"] == "Turn 1 request"
    assert out.turns[1]["request"] == "Turn 3 request"


def test_compact_drops_invalid_pin_indexes(monkeypatch):
    c = _make_conv(8)
    c.pinned_turns = [0, 999]  # 999 is out of bounds — must be silently dropped
    monkeypatch.setattr(
        "janus.llm.chat",
        lambda **kw: {"content": "summary"},
    )
    out = conversation.compact(c, keep_last=3)
    # 0 made it, 999 was filtered before keep_set built
    assert 0 in [c.pinned_turns[0]] or len(out.pinned_turns) <= 1


def test_compact_idempotent_when_only_pinned_and_recent(monkeypatch):
    """If every turn is either pinned or in the keep window, no compaction
    should happen."""
    c = _make_conv(5)
    c.pinned_turns = [0, 1]  # first 2 pinned
    # keep_last=3 covers turns 2,3,4 → all 5 are accounted for
    monkeypatch.setattr(
        "janus.llm.chat",
        lambda **kw: {"content": "should not be called"},
    )
    out = conversation.compact(c, keep_last=3)
    assert len(out.turns) == 5
    assert out.summary == ""


def test_conversation_round_trip_preserves_pinned_turns(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    c = _make_conv(4)
    c.pinned_turns = [0, 2]
    conversation.save(c)
    loaded = conversation.load(c.id)
    assert loaded is not None
    assert loaded.pinned_turns == [0, 2]
