"""tests/test_memory_state.py — v1.7.0 state introspection + memory search."""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import config, memory, memory_state
from janus.tools.agent import AgentCreate


def _approve(*a, **kw):
    return True


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


# ---------- state_block ----------


def test_state_block_empty_on_fresh_install(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    assert memory_state.state_block() == ""


def test_state_block_lists_installed_agents(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "newsbot", "purpose": "fetch AI news",
        "schedule": "every 4 hours", "deliver_to": "telegram:123456789",
    }, _approve)
    block = memory_state.state_block()
    assert "Janus state right now" in block
    assert "newsbot" in block
    assert "every 4h" in block
    assert "123456789" in block
    assert "DO NOT GREP" in block  # the explicit instruction to the model


def test_state_block_only_pairs_skill_with_trigger(tmp_path, monkeypatch):
    """A trigger without a matching skill is NOT an agent — it's a raw
    pre-v1.6 trigger. Don't surface it as installed agent."""
    _isolate_home(tmp_path, monkeypatch)
    config.TRIGGERS_DIR.mkdir(parents=True, exist_ok=True)
    (config.TRIGGERS_DIR / "raw.yaml").write_text(
        "name: raw\nkind: interval\nwhen: \"60\"\nskill: missing\n"
        "request: x\nenabled: true\n",
        encoding="utf-8",
    )
    block = memory_state.state_block()
    assert "raw" not in block
    assert block == ""  # nothing else either


def test_state_block_lists_swarm_specs(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.SWARM_SPECS_DIR.mkdir(parents=True, exist_ok=True)
    (config.SWARM_SPECS_DIR / "research.md").write_text(
        "---\nname: research\ndescription: parallel research\nversion: 1\n---\n",
        encoding="utf-8",
    )
    block = memory_state.state_block()
    assert "research" in block
    assert "parallel research" in block


def test_state_block_lists_skill_counts(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    (config.SKILLS_DIR / "a.md").write_text(
        "---\nname: a\nstate: trusted-supervised\ncapabilities: {}\n"
        "created: x\nlast-promoted: null\nruns: 0\nsuccess: 0\nfail: 0\n---\nbody\n",
        encoding="utf-8",
    )
    (config.SKILLS_DIR / "b.md").write_text(
        "---\nname: b\nstate: quarantined\ncapabilities: {}\n"
        "created: x\nlast-promoted: null\nruns: 0\nsuccess: 0\nfail: 0\n---\nbody\n",
        encoding="utf-8",
    )
    block = memory_state.state_block()
    assert "Installed skills" in block
    assert "2 total" in block
    assert "1 trusted-supervised" in block
    assert "1 quarantined" in block


def test_state_block_lists_recent_fires(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    archive = config.HOME / "cron" / "output" / "newsbot"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "2026-05-04T12-00-00+00-00.md").write_text(
        "---\ntrigger: newsbot\nfired_at: 2026-05-04T12:00:00\n---\n\n"
        "Today's headlines:\n- AI news 1",
        encoding="utf-8",
    )
    # State block won't show fires alone (no agent installed) — wire the
    # full setup so the section path triggers via recent_fires().
    AgentCreate().run({
        "name": "newsbot", "purpose": "fetch", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    block = memory_state.state_block()
    assert "Recent agent fires" in block
    assert "newsbot" in block
    assert "Today's headlines" in block


# ---------- prepend_for_prompt integration ----------


def test_prepend_for_prompt_includes_state_block_when_agents_present(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "x", "purpose": "do x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    out = memory.prepend_for_prompt()
    assert "Janus state right now" in out
    assert "x" in out


def test_prepend_for_prompt_empty_on_fresh_install(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    assert memory.prepend_for_prompt() == ""


def test_prepend_for_prompt_combines_memory_and_state(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    # Add some memory.
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEMORY_DIR / "user.md").write_text(
        "# user\n\n## about\n\nSam, solo dev.\n",
        encoding="utf-8",
    )
    AgentCreate().run({
        "name": "y", "purpose": "z", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    out = memory.prepend_for_prompt()
    assert "Sam, solo dev" in out
    assert "Janus state right now" in out
    assert "y" in out


# ---------- search_memory ----------


def test_search_memory_finds_term_in_category(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEMORY_DIR / "project.md").write_text(
        "# project\n\n## current\n\nWorking on Hermes parity migration.\n",
        encoding="utf-8",
    )
    hits = memory_state.search_memory("hermes")
    assert hits
    assert any("Hermes" in h["line"] for h in hits)
    assert hits[0]["category"] == "project"
    assert hits[0]["section"] == "current"


def test_search_memory_returns_empty_for_no_match(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEMORY_DIR / "user.md").write_text("# user\n\nplain text\n", encoding="utf-8")
    assert memory_state.search_memory("nonexistent") == []


def test_search_memory_searches_audit_log(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    audit = config.MEMORY_DIR / "_audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "2026-05-04T12-00-00__newsbot.md").write_text(
        "# diff\n\n## ops\n\n[append] project.md ## news\n  Latest AI breakthrough discovered.",
        encoding="utf-8",
    )
    hits = memory_state.search_memory("breakthrough")
    assert hits
    assert hits[0]["category"].startswith("_audit/")


def test_search_memory_respects_top_k(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    body = "match\n" * 50
    (config.MEMORY_DIR / "user.md").write_text(
        f"# user\n\n## section\n\n{body}",
        encoding="utf-8",
    )
    hits = memory_state.search_memory("match", top_k=3)
    assert len(hits) == 3
