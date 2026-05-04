"""tests/test_tool_agent.py — v1.6.0 agent lifecycle tools."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from janus import config
from janus.tools.agent import (
    AgentCreate, AgentList, AgentRunNow, AgentDelete, AgentSetEnabled,
    parse_schedule, ParsedSchedule, _validate_name, _validate_deliver_to,
    _build_skill_md, _build_trigger_yaml,
)
from janus.triggers.base import list_triggers, load_triggers


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


# ---------- Schedule parser ----------


@pytest.mark.parametrize("spec, expected_kind, expected_when", [
    ("every 4 hours", "interval", "14400"),
    ("every 30 minutes", "interval", "1800"),
    ("every 30 min", "interval", "1800"),
    ("every 10 sec", "interval", "10"),
    ("every 2 days", "interval", "172800"),
    ("hourly", "interval", "3600"),
    ("interval:7200", "interval", "7200"),
    ("cron:0 7 * * *", "cron", "0 7 * * *"),
    ("daily", "cron", "0 7 * * *"),
])
def test_parse_schedule_known_forms(spec, expected_kind, expected_when):
    p = parse_schedule(spec)
    assert p.kind == expected_kind
    assert p.when == expected_when


def test_parse_schedule_morning_default():
    p = parse_schedule("every morning")
    assert p == ParsedSchedule("cron", "0 7 * * *")


def test_parse_schedule_morning_at_time():
    p = parse_schedule("every morning at 6am")
    assert p == ParsedSchedule("cron", "0 6 * * *")


def test_parse_schedule_evening_pm():
    p = parse_schedule("every evening at 8:30pm")
    assert p == ParsedSchedule("cron", "30 20 * * *")


def test_parse_schedule_weekday():
    p = parse_schedule("every monday at 9am")
    assert p == ParsedSchedule("cron", "0 9 * * 1")


def test_parse_schedule_weekday_pm_with_minute():
    p = parse_schedule("every friday at 5:45pm")
    assert p == ParsedSchedule("cron", "45 17 * * 5")


def test_parse_schedule_rejects_garbage():
    with pytest.raises(ValueError):
        parse_schedule("whenever the model feels like it")


def test_parse_schedule_rejects_bad_cron():
    with pytest.raises(ValueError):
        parse_schedule("cron:not 5 fields")


def test_parse_schedule_rejects_bad_interval():
    with pytest.raises(ValueError):
        parse_schedule("interval:abc")
    with pytest.raises(ValueError):
        parse_schedule("interval:0")


# ---------- Name + deliver_to validators ----------


@pytest.mark.parametrize("name, ok", [
    ("samoul", True),
    ("samoul-news", True),
    ("samoul_news", True),
    ("a1", True),
    ("Samoul", False),       # uppercase
    ("-leading-dash", False),
    ("with space", False),
    ("", False),
    ("a" * 42, False),       # 42 > 41
])
def test_validate_name(name, ok):
    err = _validate_name(name)
    if ok:
        assert err is None
    else:
        assert err is not None


@pytest.mark.parametrize("d, ok", [
    ("log", True),
    ("telegram:123456789", True),
    ("telegram:-100123456", True),  # group chats are negative
    ("telegram:@channelname", True),
    ("telegram:", False),
    ("telegram:not-a-number", False),
    ("slack:foo", False),
])
def test_validate_deliver_to(d, ok):
    err = _validate_deliver_to(d)
    if ok:
        assert err is None
    else:
        assert err is not None


# ---------- agent_create end-to-end ----------


def test_agent_create_writes_skill_and_trigger(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "samoul",
        "purpose": "Fetch latest AI news every cycle and summarize.",
        "schedule": "every 4 hours",
        "deliver_to": "telegram:123456789",
        "tool_names": ["web_search", "web_fetch"],
        "capabilities": {"web.fetch": ["news.google.com/*"]},
    }, _approve)
    assert "created agent 'samoul'" in out

    skill_p = config.SKILLS_DIR / "samoul.md"
    trig_p = config.TRIGGERS_DIR / "samoul.yaml"
    assert skill_p.is_file()
    assert trig_p.is_file()

    skill_text = skill_p.read_text()
    assert "name: samoul" in skill_text
    assert "Fetch latest AI news" in skill_text
    assert "tool_names:" in skill_text
    assert "- web_search" in skill_text
    assert "web.fetch:" in skill_text

    trig_text = trig_p.read_text()
    assert "kind: interval" in trig_text
    assert "when: \"14400\"" in trig_text or "when: 14400" in trig_text
    assert "deliver_to:" in trig_text
    assert "123456789" in trig_text
    assert "enabled: true" in trig_text


def test_agent_create_refuses_duplicate(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    args = dict(name="dup", purpose="x", schedule="hourly", deliver_to="log")
    out1 = AgentCreate().run(args, _approve)
    assert out1.startswith("created")
    out2 = AgentCreate().run(args, _approve)
    assert "already exists" in out2


def test_agent_create_returns_daemon_hint_when_not_running(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "needs-daemon", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    assert "daemon" in out.lower()


def test_agent_create_rejects_bad_schedule(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "bad", "purpose": "x",
        "schedule": "lol whenever",  # unparseable
        "deliver_to": "log",
    }, _approve)
    assert out.startswith("error:")


def test_agent_create_rejects_bad_deliver_to(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "bad", "purpose": "x", "schedule": "hourly",
        "deliver_to": "discord:123",
    }, _approve)
    assert out.startswith("error:")


def test_agent_create_refusal_when_approver_denies(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "no", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _deny)
    assert out.startswith("refused:")
    assert not (config.SKILLS_DIR / "no.md").exists()
    assert not (config.TRIGGERS_DIR / "no.yaml").exists()


# ---------- agent_list ----------


def test_agent_list_empty(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentList().run({}, _approve)
    assert "no agents installed" in out


def test_agent_list_shows_created_agent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "lister", "purpose": "demo agent",
        "schedule": "every 6 hours", "deliver_to": "log",
    }, _approve)
    out = AgentList().run({}, _approve)
    assert "lister" in out
    assert "every 6h" in out
    assert "enabled" in out
    assert "delivers" in out


def test_agent_list_skips_pure_triggers(tmp_path, monkeypatch):
    """Trigger files without a matching skill don't appear in agent_list —
    they're pre-v1.6 raw triggers, not agents."""
    _isolate_home(tmp_path, monkeypatch)
    config.TRIGGERS_DIR.mkdir(parents=True, exist_ok=True)
    (config.TRIGGERS_DIR / "raw.yaml").write_text(
        "name: raw\nkind: interval\nwhen: \"60\"\nskill: missing\n"
        "request: x\nenabled: true\n",
        encoding="utf-8",
    )
    out = AgentList().run({}, _approve)
    assert "raw" not in out  # not surfaced; not an "agent"


# ---------- agent_set_enabled ----------


def test_agent_set_enabled_pauses_and_resumes(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "toggler", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    trig_p = config.TRIGGERS_DIR / "toggler.yaml"
    assert "enabled: true" in trig_p.read_text()

    out = AgentSetEnabled().run({"name": "toggler", "enabled": False}, _approve)
    assert "paused" in out
    assert "enabled: false" in trig_p.read_text()

    out = AgentSetEnabled().run({"name": "toggler", "enabled": True}, _approve)
    assert "enabled" in out and "paused" not in out
    assert "enabled: true" in trig_p.read_text()


def test_agent_set_enabled_unknown(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentSetEnabled().run({"name": "ghost", "enabled": True}, _approve)
    assert out.startswith("error:")


# ---------- agent_delete ----------


def test_agent_delete_removes_both(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "doomed", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    skill_p = config.SKILLS_DIR / "doomed.md"
    trig_p = config.TRIGGERS_DIR / "doomed.yaml"
    assert skill_p.exists() and trig_p.exists()

    out = AgentDelete().run({"name": "doomed"}, _approve)
    assert "deleted agent 'doomed'" in out
    assert not skill_p.exists()
    assert not trig_p.exists()


def test_agent_delete_refusal(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "untouched", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    out = AgentDelete().run({"name": "untouched"}, _deny)
    assert out.startswith("refused:")
    assert (config.SKILLS_DIR / "untouched.md").exists()


def test_agent_delete_unknown(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentDelete().run({"name": "ghost"}, _approve)
    assert out.startswith("error:")


# ---------- agent_run_now (mocked fire_once) ----------


def test_agent_run_now_invokes_fire_once(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "runnable", "purpose": "demo", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)

    fired: list[str] = []

    def _fake_fire_once(t, detail=None):
        fired.append(t.name)
        return f"fired-output-from-{t.name}"

    monkeypatch.setattr("janus.triggers.runtime.fire_once", _fake_fire_once)

    out = AgentRunNow().run({"name": "runnable"}, _approve)
    assert "fired 'runnable'" in out
    assert "fired-output-from-runnable" in out
    assert fired == ["runnable"]


def test_agent_run_now_unknown(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentRunNow().run({"name": "ghost"}, _approve)
    assert out.startswith("error:")


def test_agent_run_now_truncates_huge_output(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "loud", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    monkeypatch.setattr(
        "janus.triggers.runtime.fire_once",
        lambda t, detail=None: "X" * 6000,
    )
    out = AgentRunNow().run({"name": "loud"}, _approve)
    assert "more chars" in out
    assert len(out) < 6000  # truncated


# ---------- Trigger YAML round-trips through the parser ----------


def test_trigger_yaml_loads_back_via_list_triggers(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "roundtrip", "purpose": "demo", "schedule": "every 2 hours",
        "deliver_to": "telegram:123456789",
    }, _approve)
    triggers = load_triggers()
    assert "roundtrip" in triggers
    t = triggers["roundtrip"]
    assert t.kind == "interval"
    assert t.when == "7200"
    assert t.deliver_to == "telegram:123456789"
    assert t.skill == "roundtrip"
    assert t.enabled is True


# ---------- Per-trigger deliver_to dispatcher ----------


def test_notify_per_trigger_log_only(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    from janus.triggers.runtime import _notify_per_trigger
    from janus.triggers.base import FireEvent

    fn = _notify_per_trigger("log")
    ev = FireEvent(trigger="t", request="r", skill="s", fired_at="2026-01-01T00:00:00")
    # Just exercises the path without raising
    fn(ev, "hello")
    # log file should have been written
    assert config.LOG_FILE.is_file()


def test_notify_per_trigger_telegram_calls_send(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    from janus.triggers.runtime import _notify_per_trigger
    from janus.triggers.base import FireEvent

    sent: list[tuple[list[str], str]] = []

    def _fake_send(chat_ids, event, output):
        sent.append((chat_ids, output))

    monkeypatch.setattr("janus.triggers.runtime._send_telegram", _fake_send)

    fn = _notify_per_trigger("telegram:123456789,12345")
    ev = FireEvent(trigger="t", request="r", skill="s", fired_at="2026-01-01T00:00:00")
    fn(ev, "agent output")
    assert sent == [(["123456789", "12345"], "agent output")]


def test_notify_per_trigger_unknown_falls_back_to_log(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    from janus.triggers.runtime import _notify_per_trigger
    from janus.triggers.base import FireEvent

    fn = _notify_per_trigger("slack:#general")
    ev = FireEvent(trigger="t", request="r", skill="s", fired_at="2026-01-01T00:00:00")
    fn(ev, "hi")
    assert "unknown deliver_to" in config.LOG_FILE.read_text()


# ---------- Tool registration ----------


def test_agent_tools_in_default_registry():
    """Sanity: the five new tools should appear in the bundled registry."""
    from janus.tools import default_registry
    reg = default_registry()
    names = {t["function"]["name"] for t in reg.schemas()}
    assert "agent_create" in names
    assert "agent_list" in names
    assert "agent_run_now" in names
    assert "agent_delete" in names
    assert "agent_set_enabled" in names


# ---------- v1.6.1: unattended-mode preamble ----------


def test_default_body_includes_unattended_preamble(tmp_path, monkeypatch):
    """Bug J31 — agent fired and asked the user 'Please confirm…'.
    Default body must explicitly tell the agent it runs unattended."""
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "unattended", "purpose": "fetch news",
        "schedule": "hourly", "deliver_to": "log",
    }, _approve)
    body = (config.SKILLS_DIR / "unattended.md").read_text()
    assert "YOU RUN UNATTENDED" in body
    assert "Never ask for confirmation" in body
    assert "NO HUMAN IS WATCHING" in body


def test_custom_system_prompt_still_gets_unattended_preamble(tmp_path, monkeypatch):
    """User-supplied system_prompt MUST also be wrapped — without this,
    custom prompts inherit chat-mode 'ask the user' reflex and break
    when fired (the J31 root cause)."""
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "custom", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
        "system_prompt": "You are a wise sage. Speak in haiku.",
    }, _approve)
    body = (config.SKILLS_DIR / "custom.md").read_text()
    assert "YOU RUN UNATTENDED" in body
    assert "wise sage" in body  # custom prompt preserved
    assert "Speak in haiku" in body


# ---------- v1.6.1: cron output archive ----------


def test_fire_archives_output_to_cron_output_dir(tmp_path, monkeypatch):
    """Each fire should land in ~/.janus/cron/output/<agent>/<ts>.md
    (Hermes-compatible layout for future migrations)."""
    _isolate_home(tmp_path, monkeypatch)
    from janus.triggers.runtime import _archive_fire_output
    from janus.triggers.base import FireEvent

    ev = FireEvent(
        trigger="newsbot",
        request="get the news",
        skill="newsbot",
        fired_at="2026-05-04T12:34:56+00:00",
    )
    _archive_fire_output(ev, "## Today's news\n\n- Item 1\n- Item 2\n")

    archive_dir = config.HOME / "cron" / "output" / "newsbot"
    assert archive_dir.is_dir()
    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "trigger: newsbot" in text
    assert "fired_at: 2026-05-04T12:34:56+00:00" in text
    assert "Today's news" in text


def test_archive_failure_does_not_crash_fire(tmp_path, monkeypatch):
    """If the archive write fails (e.g. disk full), fire continues."""
    _isolate_home(tmp_path, monkeypatch)
    from janus.triggers.runtime import _archive_fire_output
    from janus.triggers.base import FireEvent

    # Force an OSError by making the parent path a regular file.
    blocker = config.HOME / "cron"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")

    ev = FireEvent(
        trigger="x", request="x", skill="x", fired_at="2026-01-01T00:00:00",
    )
    # Should NOT raise.
    _archive_fire_output(ev, "output")


# ---------- Helpers ----------


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point all ~/.janus/ paths into tmp_path so tests don't pollute real state."""
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "DAEMON_STATE", home / "daemon.state.json")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()
