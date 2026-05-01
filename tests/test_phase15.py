"""Tests for Phase 15 — custom commands, /init, /doctor, output styles."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus import config, commands as commands_mod, doctor, init_codebase, output_styles


# ---------- custom commands ----------


def _write_command(janus_home, name, body, frontmatter=None):
    config.COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    parts = []
    if frontmatter:
        parts.append("---")
        for k, v in frontmatter.items():
            parts.append(f"{k}: {v}")
        parts.append("---")
        parts.append("")
    parts.append(body)
    p = config.COMMANDS_DIR / f"{name}.md"
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


def test_load_no_commands_returns_empty(janus_home):
    assert commands_mod.load_all() == {}


def test_load_one_command_uses_filename_stem(janus_home):
    _write_command(janus_home, "refactor", "Refactor this:\n\n{args}")
    cmds = commands_mod.load_all()
    assert "refactor" in cmds
    assert "Refactor this:" in cmds["refactor"].body


def test_load_command_with_frontmatter_overrides_name(janus_home):
    _write_command(janus_home, "x", "body",
                   frontmatter={"name": "do-it", "description": "does the thing"})
    cmds = commands_mod.load_all()
    assert "do-it" in cmds
    assert cmds["do-it"].description == "does the thing"


def test_render_substitutes_args(janus_home):
    _write_command(janus_home, "say", "Say: {args}")
    cmd = commands_mod.load_all()["say"]
    assert cmd.render("hello world") == "Say: hello world"


def test_render_handles_uppercase_placeholder(janus_home):
    _write_command(janus_home, "x", "Hi {ARGS}!")
    cmd = commands_mod.load_all()["x"]
    assert cmd.render("Sam") == "Hi Sam!"


def test_workspace_command_overrides_home(janus_home):
    # Home: name=foo, body="HOME"
    _write_command(janus_home, "foo", "HOME")
    # Workspace: name=foo, body="WORKSPACE"
    ws_dir = Path(config.WORKSPACE) / ".janus" / "commands"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "foo.md").write_text("WORKSPACE", encoding="utf-8")
    cmds = commands_mod.load_all()
    assert cmds["foo"].body.strip() == "WORKSPACE"


# ---------- doctor ----------


def test_doctor_runs_all_checks(janus_home):
    results = doctor.run_all()
    assert len(results) >= 10
    statuses = {r.status for r in results}
    assert statuses.issubset({"pass", "warn", "fail"})


def test_doctor_passes_api_key_when_set(janus_home, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key-1234567890")
    results = doctor.run_all()
    api = next(r for r in results if r.name == "API key")
    assert api.status == "pass"


def test_doctor_fails_api_key_when_unset(janus_home, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    results = doctor.run_all()
    api = next(r for r in results if r.name == "API key")
    assert api.status == "fail"


def test_doctor_render_includes_summary(janus_home):
    results = doctor.run_all()
    out = doctor.render(results, color=False)
    assert "pass" in out
    assert "fail" in out or "warn" in out  # at least the legend appears


# ---------- init_codebase ----------


def test_scan_workspace_returns_top_level(janus_home):
    Path(config.WORKSPACE, "README.md").write_text("# Demo\nA demo.", encoding="utf-8")
    Path(config.WORKSPACE, "src").mkdir()
    ctx = init_codebase.scan_workspace()
    names = [t["name"] for t in ctx["top_level"]]
    assert "README.md" in names
    assert "src" in names
    assert ctx["readme"].startswith("# Demo")


def test_scan_workspace_picks_up_package_files(janus_home):
    Path(config.WORKSPACE, "pyproject.toml").write_text(
        '[project]\nname = "x"\n', encoding="utf-8",
    )
    ctx = init_codebase.scan_workspace()
    assert "pyproject.toml" in ctx["package_files"]


def test_propose_returns_dict_with_two_keys(janus_home, fake_llm):
    fake_llm.append({"content": json.dumps({
        "user_md_additions": [{"section": "Identity", "text": "Sam"}],
        "skill_proposals": [{
            "name": "demo", "description": "x",
            "capabilities": {"fs.read": ["**"]},
            "body": "do it",
        }],
    })})
    out = init_codebase.propose()
    assert "user_md_additions" in out
    assert "skill_proposals" in out
    assert out["user_md_additions"][0]["section"] == "Identity"


def test_propose_handles_unparseable_llm(janus_home, fake_llm):
    fake_llm.append({"content": "not json"})
    out = init_codebase.propose()
    assert "error" in out


def test_apply_user_md_creates_sections(janus_home):
    n = init_codebase.apply_user_md([
        {"section": "Identity", "text": "Sam — solo dev"},
        {"section": "Active projects", "text": "janus"},
    ])
    assert n == 2
    txt = config.USER_MODEL_FILE.read_text(encoding="utf-8")
    assert "Sam" in txt and "Active projects" in txt


def test_apply_skill_writes_quarantined(janus_home):
    p = init_codebase.apply_skill({
        "name": "demo", "description": "demo skill",
        "capabilities": {"fs.read": ["**"]},
        "body": "step 1: do thing",
    })
    assert p is not None
    text = p.read_text(encoding="utf-8")
    assert "state: quarantined" in text


# ---------- output_styles ----------


def test_output_style_plain_passthrough():
    assert output_styles.render("hello\nworld", "plain") == "hello\nworld"


def test_output_style_terse_takes_first_paragraph():
    out = output_styles.render(
        "first paragraph.\n\nsecond paragraph.", "terse",
    )
    assert out == "first paragraph."


def test_output_style_json_wraps():
    out = output_styles.render("hi", "json")
    assert json.loads(out) == {"output": "hi"}


def test_output_style_unknown_falls_back_to_default():
    # An unknown style normalizes to "markdown" (default), which passes through.
    assert output_styles.render("x", "lasagna") == "x"


def test_output_style_normalize_lowers_and_validates():
    assert output_styles.normalize("MARKDOWN") == "markdown"
    assert output_styles.normalize("nope") == "markdown"  # falls back
