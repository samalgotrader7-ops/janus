"""tests/test_origin_and_guardrails.py — v1.10.0 Tier A items 4+5."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from janus import config, session_context, tool_guardrails
from janus.tools import default_registry
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
    session_context.clear_origin()


# ---------- Session context ----------


def test_get_origin_returns_empty_when_unset():
    session_context.clear_origin()
    assert session_context.get_origin() == {}


def test_set_and_get_origin_round_trips():
    session_context.set_origin(
        platform="telegram", chat_id="123", chat_name="Sam", user="sam",
    )
    o = session_context.get_origin()
    assert o["platform"] == "telegram"
    assert o["chat_id"] == "123"
    assert o["chat_name"] == "Sam"
    assert o["user"] == "sam"
    session_context.clear_origin()


def test_origin_context_manager_restores_outer_origin():
    session_context.set_origin(platform="cli", chat_id="outer")
    with session_context.origin_context(platform="telegram", chat_id="inner"):
        assert session_context.get_origin()["chat_id"] == "inner"
    assert session_context.get_origin()["chat_id"] == "outer"
    session_context.clear_origin()


def test_origin_context_clears_when_no_outer():
    with session_context.origin_context(platform="telegram", chat_id="x"):
        assert session_context.get_origin()["chat_id"] == "x"
    assert session_context.get_origin() == {}


def test_deliver_to_default_returns_telegram_when_in_telegram_chat():
    with session_context.origin_context(platform="telegram", chat_id="999"):
        assert session_context.deliver_to_default() == "telegram:999"


def test_deliver_to_default_returns_log_when_no_origin():
    session_context.clear_origin()
    assert session_context.deliver_to_default() == "log"


def test_origin_is_per_thread():
    """Two concurrent telegram chats must not see each other's origin."""
    results = {}

    def worker(name, chat_id):
        with session_context.origin_context(platform="telegram", chat_id=chat_id):
            # Simulate work; another thread changes its own origin meanwhile.
            import time
            time.sleep(0.01)
            results[name] = session_context.get_origin()["chat_id"]

    t1 = threading.Thread(target=worker, args=("a", "111"))
    t2 = threading.Thread(target=worker, args=("b", "222"))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert results["a"] == "111"
    assert results["b"] == "222"


# ---------- agent_create reads origin ----------


def test_agent_create_uses_origin_default_when_deliver_to_omitted(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    with session_context.origin_context(platform="telegram", chat_id="123456789"):
        out = AgentCreate().run({
            "name": "originbot", "purpose": "fetch",
            "schedule": "hourly",
            # NO deliver_to — should default from origin
        }, _approve)
    assert "created agent 'originbot'" in out
    trig_yaml = (config.TRIGGERS_DIR / "originbot.yaml").read_text()
    assert "deliver_to: telegram:123456789" in trig_yaml or 'deliver_to: "telegram:123456789"' in trig_yaml


def test_agent_create_explicit_deliver_to_overrides_origin(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    with session_context.origin_context(platform="telegram", chat_id="111"):
        out = AgentCreate().run({
            "name": "explicit", "purpose": "x", "schedule": "hourly",
            "deliver_to": "log",
        }, _approve)
    assert "created" in out
    trig_yaml = (config.TRIGGERS_DIR / "explicit.yaml").read_text()
    assert "deliver_to: log" in trig_yaml


def test_agent_create_falls_back_to_log_with_no_origin(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = AgentCreate().run({
        "name": "nooriginx", "purpose": "x", "schedule": "hourly",
    }, _approve)
    assert "created" in out
    trig_yaml = (config.TRIGGERS_DIR / "nooriginx.yaml").read_text()
    assert "deliver_to: log" in trig_yaml


# ---------- Tool guardrails ----------


def test_guardrail_warns_on_overwriting_large_file(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    big = tmp_path / "big.txt"
    big.write_text("x" * 200_000, encoding="utf-8")
    out = tool_guardrails.check("fs_write", {"path": str(big), "content": "new"})
    assert out
    assert "overwriting" in out
    assert "200KB" in out or "195KB" in out


def test_guardrail_no_warn_on_creating_new_file(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = tool_guardrails.check("fs_write", {
        "path": str(tmp_path / "new.txt"), "content": "x",
    })
    assert out == ""


def test_guardrail_no_warn_on_small_overwrite(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    small = tmp_path / "small.txt"
    small.write_text("tiny", encoding="utf-8")
    out = tool_guardrails.check("fs_write", {"path": str(small), "content": "new"})
    assert out == ""


def test_guardrail_warns_on_force_push():
    out = tool_guardrails.check("shell", {"command": "git push --force origin main"})
    assert out
    assert "force-push" in out


def test_guardrail_warns_on_git_reset_hard():
    out = tool_guardrails.check("shell", {"command": "git reset --hard HEAD"})
    assert "reset --hard" in out


def test_guardrail_warns_on_terraform_destroy():
    out = tool_guardrails.check("shell", {"command": "cd infra && terraform destroy"})
    assert "terraform destroy" in out


def test_guardrail_warns_on_kubectl_delete():
    out = tool_guardrails.check("shell", {"command": "kubectl delete pod foo"})
    assert "kubectl delete" in out


def test_guardrail_warns_on_npm_publish():
    out = tool_guardrails.check("shell", {"command": "npm publish --access public"})
    assert "npm publish" in out


def test_guardrail_no_warn_on_safe_shell():
    out = tool_guardrails.check("shell", {"command": "git status"})
    assert out == ""


def test_guardrail_warns_on_agent_delete():
    out = tool_guardrails.check("agent_delete", {"name": "samoul"})
    assert "samoul" in out
    assert "irreversible" in out


def test_guardrail_check_returns_empty_for_unknown_tool():
    assert tool_guardrails.check("nonexistent", {}) == ""


def test_guardrail_check_swallows_exceptions(monkeypatch):
    """Internal failures must NEVER crash the tool path."""
    def boom(*a, **kw):
        raise RuntimeError("internal bug")
    monkeypatch.setattr(tool_guardrails, "_check_shell", boom)
    out = tool_guardrails.check("shell", {"command": "git status"})
    assert out == ""


# ---------- Registry-level guardrail integration ----------


class _NoopWriteTool:
    """Test stub: a write-class tool that returns a fixed success string."""
    name = "_noop_write"
    description = "stub"
    parameters = {"type": "object", "properties": {}}
    risk = "write"
    dangerous = False

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }}

    def run(self, args, approver):
        return "wrote ok"


def test_registry_prepends_guardrail_warning_to_success(monkeypatch):
    """Successful write/exec results get the guardrail warning prepended."""
    from janus.tools.base import Registry
    reg = Registry([_NoopWriteTool()])
    monkeypatch.setattr(
        tool_guardrails, "check",
        lambda name, args: "[guardrail] borderline thing" if name == "_noop_write" else "",
    )
    out = reg.call("_noop_write", {"x": 1}, lambda *a, **kw: True)
    assert out.startswith("[guardrail] borderline thing")
    assert "wrote ok" in out


def test_registry_skips_guardrail_on_error_result(monkeypatch):
    """error: ... outputs from a tool shouldn't get a guardrail prefix."""
    class _ErrTool(_NoopWriteTool):
        name = "_err_tool"
        def run(self, args, approver):
            return "error: not found"
    from janus.tools.base import Registry
    reg = Registry([_ErrTool()])
    monkeypatch.setattr(
        tool_guardrails, "check", lambda *a, **kw: "[guardrail] x",
    )
    out = reg.call("_err_tool", {}, lambda *a, **kw: True)
    assert out == "error: not found"


def test_registry_skips_guardrail_on_read_class():
    """Read-class tools never get guardrail wrap."""
    class _ReadTool(_NoopWriteTool):
        name = "_read_tool"
        risk = "read"
        def run(self, args, approver):
            return "read ok"
    from janus.tools.base import Registry
    reg = Registry([_ReadTool()])
    out = reg.call("_read_tool", {}, lambda *a, **kw: True)
    assert out == "read ok"
