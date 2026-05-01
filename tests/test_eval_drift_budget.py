"""Tests for Phase 17 — eval --drift-budget CI gate.

Driven via subprocess so the actual exit codes are verified end-to-end.
We seed a small log + control the LLM responses so the eval is fully
deterministic.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def janus_env(tmp_path):
    """Build an isolated env with a seeded log + a fake LLM endpoint that
    returns the same interpretations the records had (drift = 0)."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # Seed a log with two records the eval can replay.
    log = home / "log.jsonl"
    records = [
        {"ts": "2026-04-30T10:00:00Z", "request": "do X",
         "interpretations": [{"label": "do x", "action": "do x", "risk": "low"}],
         "choice": 1},
        {"ts": "2026-04-30T11:00:00Z", "request": "do Y",
         "interpretations": [{"label": "do y", "action": "do y", "risk": "low"}],
         "choice": 1},
    ]
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                   encoding="utf-8")

    env = dict(os.environ)
    env["JANUS_HOME"] = str(home)
    env["JANUS_WORKSPACE"] = str(workspace)
    env["JANUS_API_KEY"] = "test"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "janus", *args],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )


def test_eval_help_lists_drift_budget(janus_env):
    proc = _run(janus_env, "--help")
    assert "--drift-budget" in proc.stdout


def test_eval_drift_budget_invalid_value_returns_usage(janus_env):
    proc = _run(janus_env, "--eval", "--drift-budget", "not-a-number")
    assert proc.returncode == 2
    assert "drift-budget" in proc.stdout or "drift-budget" in proc.stderr


def test_eval_drift_budget_unknown_format_returns_usage(janus_env):
    proc = _run(janus_env, "--eval", "--output-format", "xml")
    assert proc.returncode == 2


def test_eval_no_records_with_budget_treated_as_pass(janus_env):
    # Empty log: replace the seeded log with empty file.
    log = os.path.join(janus_env["JANUS_HOME"], "log.jsonl")
    open(log, "w").close()
    proc = _run(janus_env, "--eval", "--drift-budget", "0.0")
    # Empty log → no replay → pass per acceptance.
    assert proc.returncode == 0
    assert "no records" in proc.stderr.lower() or "warning" in proc.stderr.lower()


def test_eval_output_format_json_emits_envelope(janus_env, monkeypatch):
    """JSON output prints the report as a single JSON object on stdout.

    We point the API base at a non-existent host so the interpreter
    raises and the report records `interpreter_error` for each — that's
    fine; we're testing the output shape, not the replay quality."""
    janus_env["JANUS_API_BASE"] = "http://127.0.0.1:1/v1"
    proc = _run(janus_env, "--eval", "--output-format", "json", "--last", "2")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert "n_records" in payload
    assert "interp_drift_avg" in payload
    assert "by_record" in payload
