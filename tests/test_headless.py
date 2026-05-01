"""Tests for Phase 16 — headless mode + JSON output + exit codes."""
from __future__ import annotations
import json
import subprocess
import sys

import pytest

from janus import headless


def _approve(*a, **kw):
    return True


# ---------- in-process tests (use fake_llm) ----------


def test_headless_run_text_format(janus_home, fake_llm, capsys):
    fake_llm.append({"content": json.dumps({"interpretations": [
        {"label": "do x", "action": "do x", "risk": "low"},
    ]})})
    fake_llm.append({"content": "result text", "tool_calls": []})
    rc = headless.run(prompt="please do x")
    out = capsys.readouterr().out
    assert rc == headless.EXIT_OK
    assert "result text" in out


def test_headless_run_json_format_envelope(janus_home, fake_llm, capsys):
    fake_llm.append({"content": json.dumps({"interpretations": [
        {"label": "do x", "action": "do x", "risk": "low"},
    ]})})
    fake_llm.append({"content": "ok", "tool_calls": []})
    rc = headless.run(prompt="x", output_format="json")
    out = capsys.readouterr().out
    assert rc == headless.EXIT_OK
    envelope = json.loads(out.strip())
    assert envelope["request"] == "x"
    assert envelope["output"] == "ok"
    assert envelope["choice"] == "auto-first"
    assert "tokens" in envelope
    assert "trace" in envelope


def test_headless_run_jsonl_format_one_line_per_step(janus_home, fake_llm, capsys):
    fake_llm.append({"content": json.dumps({"interpretations": [
        {"label": "do x", "action": "do x", "risk": "low"},
    ]})})
    fake_llm.append({"content": "done", "tool_calls": []})
    rc = headless.run(prompt="x", output_format="jsonl")
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert rc == headless.EXIT_OK
    # Each line is valid JSON.
    parsed = [json.loads(l) for l in lines]
    types = [p.get("type") for p in parsed]
    # Final line is the output envelope.
    assert types[-1] == "output"
    assert parsed[-1]["text"] == "done"


def test_headless_run_empty_prompt_returns_usage_error(janus_home, capsys):
    rc = headless.run(prompt="")
    err = capsys.readouterr().err
    assert rc == headless.EXIT_USAGE
    assert "empty" in err


def test_headless_run_unknown_format_is_usage_error(janus_home, capsys):
    rc = headless.run(prompt="x", output_format="xml")
    err = capsys.readouterr().err
    assert rc == headless.EXIT_USAGE
    assert "output-format" in err


def test_headless_run_interpreter_failure_is_runtime_error(
    janus_home, fake_llm, capsys,
):
    # Empty queue → fake_llm raises RuntimeError when interpreter calls it.
    rc = headless.run(prompt="x")
    err = capsys.readouterr().err
    assert rc == headless.EXIT_RUNTIME_ERROR
    assert "interpreter error" in err


def test_headless_no_color_strips_ansi(janus_home, fake_llm, capsys):
    fake_llm.append({"content": json.dumps({"interpretations": [
        {"label": "x", "action": "x", "risk": "low"},
    ]})})
    fake_llm.append({"content": "\033[32mgreen\033[0m text", "tool_calls": []})
    rc = headless.run(prompt="x", no_color=True)
    out = capsys.readouterr().out
    assert rc == headless.EXIT_OK
    assert "\033[" not in out
    assert "green text" in out


def test_headless_persists_conversation(janus_home, fake_llm):
    from janus import conversation
    fake_llm.append({"content": json.dumps({"interpretations": [
        {"label": "x", "action": "x", "risk": "low"},
    ]})})
    fake_llm.append({"content": "done", "tool_calls": []})
    headless.run(prompt="hi")
    items = conversation.list_all()
    assert len(items) == 1
    assert items[0]["turns"] == 1


# ---------- subprocess test for stdin pipe + exit code propagation ----------


def test_subprocess_stdin_pipe_returns_usage_error_on_empty(janus_home, monkeypatch):
    """Sanity: empty stdin via subprocess yields exit 2 and no crash."""
    import os
    env = dict(os.environ)
    env["JANUS_API_KEY"] = "test"
    env["JANUS_HOME"] = str(janus_home)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "janus", "-p"],
        input="", capture_output=True, text=True, env=env,
        encoding="utf-8",
    )
    assert proc.returncode == headless.EXIT_USAGE


def test_help_includes_headless_flags(janus_home):
    """Smoke: --help mentions the new flags so users discover them."""
    import os
    env = dict(os.environ)
    env["JANUS_API_KEY"] = "test"
    env["JANUS_HOME"] = str(janus_home)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "janus", "--help"],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )
    assert "-p" in proc.stdout
    assert "--output-format" in proc.stdout
    assert "json" in proc.stdout
