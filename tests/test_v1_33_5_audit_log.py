"""Tests for v1.33.5 — production audit log (Phase 6.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus import audit_log


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    fake = tmp_path / ".janus"
    fake.mkdir()
    from janus import config
    monkeypatch.setattr(config, "HOME", fake)
    monkeypatch.setattr(audit_log.config, "HOME", fake)
    return fake


# -------------------- record() --------------------


def test_record_creates_file(fake_home):
    audit_log.record("test.event", foo="bar")
    audit_path = fake_home / "audit.jsonl"
    assert audit_path.exists()


def test_record_writes_jsonl(fake_home):
    audit_log.record("skill.promote", name="git-pr-review", from_state="quarantined", to_state="trusted")
    audit_log.record("mcp.connect", server="filesystem")
    lines = (fake_home / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    obj = json.loads(lines[0])
    assert obj["action"] == "skill.promote"
    assert obj["details"]["name"] == "git-pr-review"
    assert "ts" in obj


def test_record_iso_timestamp(fake_home):
    audit_log.record("test.event")
    obj = json.loads((fake_home / "audit.jsonl").read_text().strip())
    ts = obj["ts"]
    # ISO format: YYYY-MM-DDTHH:MM:SSZ
    assert "T" in ts
    assert ts.endswith("Z")


def test_record_failure_silent(monkeypatch):
    """If the audit file can't be written (e.g. read-only FS), the
    parent path must NOT raise. Consumers rely on this."""
    from janus import config
    # Point at a non-writable path (a regular file, not a directory)
    monkeypatch.setattr(audit_log.config, "HOME", Path("/dev/null/cant_write"))
    # Should not raise
    audit_log.record("test.event")


# -------------------- read_lines + filter --------------------


def test_read_lines_empty_when_no_file(fake_home):
    """audit.jsonl doesn't exist yet → empty list."""
    assert audit_log.read_lines() == []


def test_read_lines_returns_all(fake_home):
    audit_log.record("a")
    audit_log.record("b")
    audit_log.record("c")
    records = audit_log.read_lines()
    assert len(records) == 3
    assert [r["action"] for r in records] == ["a", "b", "c"]


def test_read_lines_max_lines(fake_home):
    for i in range(10):
        audit_log.record(f"event-{i}")
    records = audit_log.read_lines(max_lines=3)
    assert len(records) == 3
    assert [r["action"] for r in records] == ["event-7", "event-8", "event-9"]


def test_read_lines_skips_malformed(fake_home):
    """Manually corrupted lines don't break the reader."""
    (fake_home / "audit.jsonl").write_text(
        '{"ts":"2026-05-10T00:00:00Z","action":"good","details":{}}\n'
        'not json\n'
        '{"ts":"2026-05-10T00:00:01Z","action":"also_good","details":{}}\n'
    )
    records = audit_log.read_lines()
    assert len(records) == 2
    assert records[0]["action"] == "good"
    assert records[1]["action"] == "also_good"


def test_filter_by_action_literal(fake_home):
    audit_log.record("skill.promote")
    audit_log.record("mcp.connect")
    audit_log.record("skill.demote")
    records = audit_log.read_lines()
    promotes = list(audit_log.filter_records(records, action="skill.promote"))
    assert len(promotes) == 1
    assert promotes[0]["action"] == "skill.promote"


def test_filter_by_action_prefix(fake_home):
    """A trailing dot makes it a prefix match — `skill.` matches
    skill.promote AND skill.demote."""
    audit_log.record("skill.promote")
    audit_log.record("skill.demote")
    audit_log.record("mcp.connect")
    records = audit_log.read_lines()
    skills = list(audit_log.filter_records(records, action="skill."))
    assert len(skills) == 2


def test_filter_by_since(fake_home):
    """Records with ts < since are filtered out."""
    audit_log.record("a")
    records = audit_log.read_lines()
    # Pick an `since` that's after the record's ts
    future = "2099-01-01T00:00:00Z"
    out = list(audit_log.filter_records(records, since=future))
    assert out == []
    # And one that's before — should pass through
    past = "1970-01-01T00:00:00Z"
    out = list(audit_log.filter_records(records, since=past))
    assert len(out) == 1


# -------------------- CLI --------------------


def test_cmd_audit_no_args_tail_default(fake_home, capsys):
    audit_log.record("a")
    audit_log.record("b")
    rc = audit_log.cmd_audit([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a" in out and "b" in out


def test_cmd_audit_filter_action(fake_home, capsys):
    audit_log.record("skill.promote", name="x")
    audit_log.record("mcp.connect", server="fs")
    rc = audit_log.cmd_audit(["--action", "mcp.connect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcp.connect" in out
    assert "skill.promote" not in out


def test_cmd_audit_no_records(fake_home, capsys):
    """Empty audit log → friendly message, not blank output."""
    rc = audit_log.cmd_audit([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no matching" in out.lower() or "no records" in out.lower()


def test_cmd_audit_help(capsys):
    rc = audit_log.cmd_audit(["--help"])
    assert rc == 0


def test_cmd_audit_unknown_flag_errors(capsys):
    rc = audit_log.cmd_audit(["--bogus"])
    assert rc == 2


def test_cmd_audit_invalid_tail_errors(capsys):
    rc = audit_log.cmd_audit(["--tail", "abc"])
    assert rc == 2


# -------------------- Wire-ins --------------------


def test_mcp_register_emits_audit_event(fake_home, monkeypatch):
    """mcp.register_client → audit.jsonl gets mcp.connect."""
    from janus.mcp import client as mcp
    # Stub a fake client (don't spawn a real subprocess).
    class FakeClient:
        def close(self):
            pass
    mcp._ACTIVE_CLIENTS.clear()
    mcp.register_client("test-server", FakeClient())
    records = audit_log.read_lines()
    actions = [r["action"] for r in records]
    assert "mcp.connect" in actions
    connects = [r for r in records if r["action"] == "mcp.connect"]
    assert any(r["details"].get("server") == "test-server" for r in connects)
    mcp._ACTIVE_CLIENTS.clear()


def test_mcp_unregister_emits_audit_event(fake_home, monkeypatch):
    from janus.mcp import client as mcp
    class FakeClient:
        def close(self):
            pass
    mcp._ACTIVE_CLIENTS.clear()
    mcp.register_client("temp", FakeClient())
    mcp.unregister_client("temp")
    records = audit_log.read_lines()
    actions = [r["action"] for r in records]
    assert "mcp.disconnect" in actions
    mcp._ACTIVE_CLIENTS.clear()


# -------------------- Source pins --------------------


def test_main_dispatches_audit_subcommand():
    main_path = Path(audit_log.__file__).parent / "__main__.py"
    src = main_path.read_text(encoding="utf-8")
    assert 'sub == "audit"' in src
    assert "from . import audit_log" in src


def test_skills_promote_emits_audit():
    """Source pin: skills.promote() includes the audit_log.record
    call."""
    skills_path = Path(audit_log.__file__).parent / "skills.py"
    src = skills_path.read_text(encoding="utf-8")
    promote_idx = src.index("def promote(")
    end_idx = src.index("\ndef ", promote_idx + 1)
    block = src[promote_idx:end_idx]
    assert "audit_log" in block
    assert "skill.promote" in block


# -------------------- Version pin --------------------


def test_version_bumped_to_1_33_5_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 5)
