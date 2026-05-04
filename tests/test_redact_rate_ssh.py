"""tests/test_redact_rate_ssh.py — v1.11.0 (Tier B kickoff)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from janus import config, redact, rate_limit
from janus.tools.ssh_exec import SshExec


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()


# ============================================================
# Redaction
# ============================================================


def test_redact_off_passes_through(monkeypatch):
    monkeypatch.setenv("JANUS_REDACT", "off")
    secret = "my key is sk-1234567890abcdefghijklmnopqrstuv"
    assert redact.redact(secret) == secret


def test_redact_openai_key():
    out = redact.redact("hi sk-1234567890abcdefghijklmnopqrst extra")
    assert "<REDACTED:openai_key>" in out
    assert "sk-1234567890" not in out


def test_redact_anthropic_key():
    out = redact.redact("API: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890")
    assert "<REDACTED:anthropic_key>" in out


def test_redact_github_token():
    out = redact.redact("token: ghp_abcdefghij1234567890ABCDEFGHIJabcd1234")
    assert "<REDACTED:github_token>" in out


def test_redact_aws_access_key():
    out = redact.redact("AKIAIOSFODNN7EXAMPLE in env")
    assert "<REDACTED:aws_access_key>" in out


def test_redact_telegram_bot_token():
    # Real Telegram bot tokens have ~35 chars after the colon.
    out = redact.redact("bot 1234567890:ABCDEFGhijklmnopQRSTUVwxyz1234567890ABCD")
    assert "<REDACTED:telegram_bot_token>" in out


def test_redact_authorization_header():
    out = redact.redact("Authorization: Bearer abcdef1234567890ghijklmnop")
    # Prefix preserved, value redacted
    assert "Authorization: Bearer" in out
    assert "<REDACTED:auth_header>" in out
    assert "abcdef1234567890ghijklmnop" not in out


def test_redact_generic_secret_assignment():
    out = redact.redact('config: api_key="abcdefghijklmnopqrstuvwxyz123"')
    assert "<REDACTED:generic_secret>" in out
    assert "api_key=" in out  # the LABEL is preserved
    assert "abcdefghijklmnopqrstuvwxyz123" not in out


def test_redact_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = redact.redact(f"token = {jwt} sent")
    assert "<REDACTED:jwt>" in out


def test_redact_private_key_block():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "more lines here\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact.redact(f"key:\n{block}\ndone")
    assert "<REDACTED:private_key>" in out
    assert "MIIEow" not in out


def test_redact_aggressive_email(monkeypatch):
    monkeypatch.setenv("JANUS_REDACT", "aggressive")
    out = redact.redact("contact me at sam@example.com today")
    assert "<REDACTED:email>" in out


def test_redact_aggressive_phone(monkeypatch):
    monkeypatch.setenv("JANUS_REDACT", "aggressive")
    out = redact.redact("call +1 555-123-4567 anytime")
    assert "<REDACTED:phone>" in out


def test_redact_aggressive_ipv4(monkeypatch):
    monkeypatch.setenv("JANUS_REDACT", "aggressive")
    out = redact.redact("server is at 192.168.1.10 internal")
    assert "<REDACTED:ipv4>" in out


def test_redact_conservative_does_not_touch_email():
    out = redact.redact("sam@example.com", level="conservative")
    assert "@example.com" in out  # not redacted at conservative


def test_redact_obj_walks_dict():
    obj = {
        "request": "Use ghp_abcdefghij1234567890ABCDEFGHIJabcd1234 please",
        "nested": {"header": "Authorization: Bearer abcdef1234567890ghijklmnop"},
        "items": ["sk-12345678901234567890123", "innocent"],
    }
    redacted = redact.redact_obj(obj)
    assert "ghp_" not in redacted["request"]
    assert "abcdef1234567890ghijklmnop" not in str(redacted["nested"])
    assert "sk-12345678901234567890123" not in redacted["items"][0]
    assert redacted["items"][1] == "innocent"


def test_redact_failure_returns_original(monkeypatch):
    """Patterns blowing up should not crash the redactor."""
    def boom(*a, **kw):
        raise RuntimeError("regex bug")
    monkeypatch.setattr(redact, "_apply_patterns", boom)
    out = redact.redact("plain text")
    assert out == "plain text"  # original returned, not crash


def test_redact_obj_passes_through_non_strings():
    assert redact.redact_obj(42) == 42
    assert redact.redact_obj(None) is None
    assert redact.redact_obj(True) is True


# ============================================================
# Rate limit
# ============================================================


def test_record_request_increments_counters(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(provider="openrouter", model="x", tokens=100, ok=True)
    summary = rate_limit.get_summary()
    assert "openrouter/x" in summary
    s = summary["openrouter/x"]
    assert s["requests_in_window"] == 1
    assert s["tokens_in_window"] == 100
    assert s["total_ok"] == 1
    assert s["total_429"] == 0


def test_record_429_persists_state(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(
        provider="anthropic", model="claude-sonnet", tokens=0,
        ok=False, status_429=True,
    )
    state_file = config.HOME / "rate_limit_state.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert "anthropic/claude-sonnet" in data
    assert data["anthropic/claude-sonnet"]["total_429"] == 1


def test_cooldown_active_within_30s(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(
        provider="p", model="m", ok=False, status_429=True,
    )
    cd = rate_limit.cooldown_seconds("p", "m")
    assert 0 < cd <= 30


def test_cooldown_zero_after_30s(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(provider="p", model="m", ok=False, status_429=True)
    # Manually rewind the timestamp 60s into the past.
    bucket = rate_limit._BUCKETS["p/m"]
    bucket.last_429_at -= 60
    assert rate_limit.cooldown_seconds("p", "m") == 0.0


def test_cooldown_none_when_no_429(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(provider="p", model="m", tokens=100, ok=True)
    assert rate_limit.cooldown_seconds("p", "m") == 0.0


def test_render_summary_empty(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    out = rate_limit.render_summary({})
    assert "no rate-limit data" in out


def test_render_summary_includes_table(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(provider="p", model="m", tokens=42, ok=True)
    out = rate_limit.render_summary(rate_limit.get_summary())
    assert "Rate limits" in out
    assert "p/m" in out
    assert "42" in out


def test_persistent_state_round_trips(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    rate_limit.reset()
    rate_limit.record_request(provider="p", model="m", ok=False, status_429=True)
    # Wipe in-memory; force reload from disk.
    rate_limit._BUCKETS.clear()
    rate_limit._load_state()
    assert "p/m" in rate_limit._BUCKETS
    assert rate_limit._BUCKETS["p/m"].total_429 == 1


# ============================================================
# Logger redaction wiring
# ============================================================


def test_logger_redacts_secrets_before_write(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_REDACT", "conservative")
    from janus import logger
    logger.write({"req": "key sk-1234567890abcdefghij12345"})
    text = config.LOG_FILE.read_text(encoding="utf-8")
    assert "<REDACTED:openai_key>" in text
    assert "sk-1234567890" not in text


def test_logger_passthrough_when_redact_off(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setenv("JANUS_REDACT", "off")
    from janus import logger
    logger.write({"req": "key sk-1234567890abcdefghij12345"})
    text = config.LOG_FILE.read_text(encoding="utf-8")
    assert "sk-1234567890abcdefghij12345" in text


# ============================================================
# ssh_exec tool
# ============================================================


def _fake_completed(stdout="", stderr="", returncode=0):
    class _R:
        pass
    r = _R()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def test_ssh_exec_rejects_empty_host():
    out = SshExec().run({"host": "", "command": "uptime"}, _approve)
    assert out.startswith("error: host required")


def test_ssh_exec_rejects_empty_command():
    out = SshExec().run({"host": "x", "command": ""}, _approve)
    assert out.startswith("error: command required")


def test_ssh_exec_rejects_bad_host_shape():
    """No shell metacharacters in host — prevents 'host' = '-oProxyCommand=...'."""
    out = SshExec().run(
        {"host": "; rm -rf /", "command": "x"}, _approve,
    )
    assert "disallowed characters" in out


def test_ssh_exec_rejects_long_host():
    out = SshExec().run({"host": "a" * 300, "command": "x"}, _approve)
    assert "host too long" in out


def test_ssh_exec_rejects_long_command():
    out = SshExec().run({"host": "x", "command": "y" * 5000}, _approve)
    assert "command too long" in out


def test_ssh_exec_clamps_timeout():
    """Same lesson as v1.1.1 SHELL_TIMEOUT_MAX — model passes massive
    timeout, we cap it. Test that the subprocess call uses the clamped value."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        return _fake_completed(stdout="ok")

    with patch("subprocess.run", fake_run):
        SshExec().run(
            {"host": "host1", "command": "uptime", "timeout": 99999},
            _approve,
        )
    from janus.tools.ssh_exec import SSH_TIMEOUT_MAX
    assert captured["timeout"] == SSH_TIMEOUT_MAX


def test_ssh_exec_passes_BatchMode_yes():
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _fake_completed()

    with patch("subprocess.run", fake_run):
        SshExec().run({"host": "host1", "command": "uptime"}, _approve)
    assert "BatchMode=yes" in " ".join(captured["cmd"])
    assert "StrictHostKeyChecking=accept-new" in " ".join(captured["cmd"])


def test_ssh_exec_returns_stdout_on_success():
    with patch("subprocess.run", lambda *a, **k: _fake_completed(stdout="hello\n")):
        out = SshExec().run({"host": "host1", "command": "echo hi"}, _approve)
    assert out.startswith("exit 0")
    assert "hello" in out


def test_ssh_exec_surfaces_stderr_on_nonzero():
    with patch(
        "subprocess.run",
        lambda *a, **k: _fake_completed(stdout="partial", stderr="oops", returncode=1),
    ):
        out = SshExec().run({"host": "host1", "command": "x"}, _approve)
    assert "exit 1" in out
    assert "STDOUT:" in out and "partial" in out
    assert "STDERR:" in out and "oops" in out


def test_ssh_exec_truncates_huge_stdout():
    huge = "X" * 100_000
    with patch("subprocess.run", lambda *a, **k: _fake_completed(stdout=huge)):
        out = SshExec().run({"host": "host1", "command": "x"}, _approve)
    assert "[stdout truncated]" in out
    assert len(out) < len(huge) + 100  # roughly capped


def test_ssh_exec_handles_timeout_exception():
    import subprocess as sp

    def fake_run(*a, **k):
        raise sp.TimeoutExpired(cmd="ssh", timeout=10)

    with patch("subprocess.run", fake_run):
        out = SshExec().run({"host": "host1", "command": "x"}, _approve)
    assert out.startswith("error: ssh timeout")


def test_ssh_exec_handles_missing_binary():
    def fake_run(*a, **k):
        raise FileNotFoundError("ssh: not found")

    with patch("subprocess.run", fake_run):
        out = SshExec().run({"host": "host1", "command": "x"}, _approve)
    assert "ssh binary not found" in out


def test_ssh_exec_refusal():
    out = SshExec().run({"host": "host1", "command": "x"}, _deny)
    assert out.startswith("refused: ssh_exec(host1)")


def test_ssh_exec_working_dir_quoted():
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _fake_completed()

    with patch("subprocess.run", fake_run):
        SshExec().run(
            {"host": "host1", "command": "ls", "working_dir": "/opt/app spaces"},
            _approve,
        )
    full = captured["cmd"][-1]
    # working_dir should be shell-quoted (preserve spaces); `cd` then `&&`.
    assert "cd " in full
    assert "/opt/app spaces" in full
    assert "&& (ls)" in full


def test_ssh_exec_in_default_registry():
    from janus.tools import default_registry
    reg = default_registry()
    assert "ssh_exec" in reg.names()


# ============================================================
# Auto-mode + guardrail integration with ssh_exec
# ============================================================


def test_auto_mode_blocks_destructive_ssh_command():
    from janus.auto_mode import analyze_call
    verdict = analyze_call(
        "ssh_exec", {"host": "prod", "command": "rm -rf /"},
    )
    assert not verdict.allowed
    assert "block pattern" in verdict.reason


def test_auto_mode_allows_safe_ssh_command():
    from janus.auto_mode import analyze_call
    verdict = analyze_call(
        "ssh_exec", {"host": "prod", "command": "uptime"},
    )
    assert verdict.allowed


def test_guardrail_warns_on_remote_force_push():
    from janus import tool_guardrails
    out = tool_guardrails.check(
        "ssh_exec", {"host": "deploy@vps", "command": "git push --force"},
    )
    assert out
    assert "remote" in out
    assert "deploy@vps" in out
    assert "force-push" in out


def test_guardrail_silent_on_safe_remote_command():
    from janus import tool_guardrails
    out = tool_guardrails.check(
        "ssh_exec", {"host": "host1", "command": "df -h"},
    )
    assert out == ""
