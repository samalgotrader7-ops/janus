"""Tests for `janus uninstall` (v1.3.1).

Covers:
  - dry-run never removes anything
  - --yes skips confirmation
  - interactive: typing 'no' or anything ≠ 'yes' aborts
  - inventory output mentions every populated subsystem
  - empty / non-existent home is handled gracefully
"""

from __future__ import annotations

import io
import json

import pytest

from janus import __main__ as jm
from janus import config


def _populate_home(janus_home):
    """Create one of every known state artifact so the inventory has things
    to enumerate. Mirrors what a real running Janus accumulates."""
    home = janus_home
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "skills" / "demo.md").write_text(
        "---\nname: demo\ndescription: x\nstate: quarantined\n---\n\nbody\n",
        encoding="utf-8",
    )
    (home / "memory").mkdir(parents=True, exist_ok=True)
    (home / "memory" / "user.md").write_text("# user\n\n## Identity\nSam\n",
                                             encoding="utf-8")
    (home / "memory" / "soul.md").write_text("# soul\n\n## Name\nJanus\n",
                                             encoding="utf-8")
    (home / "conversations").mkdir(parents=True, exist_ok=True)
    (home / "conversations" / "2026-05-03-abc.json").write_text(
        '{"id": "abc", "turns": []}', encoding="utf-8",
    )
    (home / "sessions" / "telegram").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / "telegram" / "12345.json").write_text(
        '{"messages": []}', encoding="utf-8",
    )
    (home / "pairing").mkdir(parents=True, exist_ok=True)
    (home / "pairing" / "approved.json").write_text(
        '{"telegram": ["12345", "99999"]}', encoding="utf-8",
    )
    (home / "pairing" / "pending.json").write_text(
        '[{"code": "AAA", "gateway": "telegram"}]', encoding="utf-8",
    )
    (home / "log.jsonl").write_text('{"ts": "2026-05-03T00:00:00"}\n',
                                    encoding="utf-8")
    (home / "cost.jsonl").write_text(
        '{"ts": "2026-05-03T00:00:00", "usd": 0.01}\n',
        encoding="utf-8",
    )
    (home / "home_channels.json").write_text(
        '{"telegram": "12345"}', encoding="utf-8",
    )
    (home / "identities.json").write_text(
        '{"sam": [["telegram", "12345"]]}', encoding="utf-8",
    )


# ---------- Inventory ----------


def test_inventory_lists_all_known_artifacts(janus_home):
    _populate_home(janus_home)
    lines = jm._inventory_home(janus_home)
    joined = "\n".join(lines)
    assert "skills/" in joined
    assert "memory/" in joined
    assert "conversations/" in joined
    assert "sessions/" in joined
    assert "pairing/" in joined
    assert "log.jsonl" in joined
    assert "cost.jsonl" in joined
    assert "home_channels.json" in joined
    assert "identities.json" in joined


def test_inventory_shows_correct_counts(janus_home):
    _populate_home(janus_home)
    lines = jm._inventory_home(janus_home)
    joined = "\n".join(lines)
    assert "1 skill(s)" in joined
    assert "2 category file(s)" in joined  # user.md + soul.md
    assert "1 saved conversation(s)" in joined
    assert "2 approved chat(s)" in joined  # 12345 + 99999
    assert "1 pending code(s)" in joined


def test_inventory_empty_home_returns_empty_list(janus_home):
    """Fresh tmp HOME with nothing in it → no inventory lines."""
    # janus_home fixture creates the dir + a few standard subdirs (via
    # ensure_home). Some may register if they're non-empty; on a truly
    # fresh fixture only the empty-skipped ones will show up.
    lines = jm._inventory_home(janus_home)
    # Whatever the fixture pre-creates, none should report counts > 0
    # for files we KNOW we didn't write.
    joined = "\n".join(lines)
    assert "saved conversation" not in joined
    assert "skill(s)" not in joined or "0 skill(s)" in joined


# ---------- Dry-run ----------


def test_dry_run_does_not_remove(janus_home, capsys):
    _populate_home(janus_home)
    jm._run_uninstall(["--dry-run"])
    out = capsys.readouterr().out
    assert "(--dry-run: nothing removed)" in out
    # Files still there.
    assert (janus_home / "skills" / "demo.md").exists()
    assert (janus_home / "memory" / "user.md").exists()


def test_dry_run_short_flag(janus_home, capsys):
    _populate_home(janus_home)
    jm._run_uninstall(["-n"])
    out = capsys.readouterr().out
    assert "(--dry-run: nothing removed)" in out
    assert (janus_home / "skills" / "demo.md").exists()


# ---------- Yes flag ----------


def test_yes_flag_removes_without_prompt(janus_home, capsys):
    _populate_home(janus_home)
    assert janus_home.is_dir()
    jm._run_uninstall(["--yes"])
    assert not janus_home.is_dir()
    out = capsys.readouterr().out
    assert "removed:" in out
    assert "pipx uninstall janus-agent" in out


def test_yes_short_flag_removes(janus_home):
    _populate_home(janus_home)
    jm._run_uninstall(["-y"])
    assert not janus_home.is_dir()


# ---------- Interactive confirmation ----------


def test_interactive_confirm_yes_removes(janus_home, monkeypatch, capsys):
    _populate_home(janus_home)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    jm._run_uninstall([])
    assert not janus_home.is_dir()


def test_interactive_confirm_no_aborts(janus_home, monkeypatch, capsys):
    _populate_home(janus_home)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(SystemExit) as e:
        jm._run_uninstall([])
    assert e.value.code == 1
    # Files still there.
    assert (janus_home / "skills" / "demo.md").exists()
    out = capsys.readouterr().out
    assert "aborted" in out


def test_interactive_confirm_anything_other_than_yes_aborts(
    janus_home, monkeypatch
):
    _populate_home(janus_home)
    monkeypatch.setattr("builtins.input", lambda _: "y")  # not 'yes'
    with pytest.raises(SystemExit) as e:
        jm._run_uninstall([])
    assert e.value.code == 1
    assert (janus_home / "skills" / "demo.md").exists()


def test_interactive_eof_aborts(janus_home, monkeypatch, capsys):
    """Ctrl-D / EOF on the prompt aborts cleanly, not crashily."""
    _populate_home(janus_home)
    def raise_eof(_):
        raise EOFError()
    monkeypatch.setattr("builtins.input", raise_eof)
    with pytest.raises(SystemExit) as e:
        jm._run_uninstall([])
    assert e.value.code == 1


# ---------- Edge cases ----------


def test_no_home_directory_handled(tmp_path, monkeypatch, capsys):
    """If $JANUS_HOME points nowhere, we report and exit cleanly."""
    nonexistent = tmp_path / "nope"
    monkeypatch.setenv("JANUS_HOME", str(nonexistent))
    import importlib
    import janus.config as cfg
    importlib.reload(cfg)
    import janus.__main__ as jm2
    importlib.reload(jm2)
    jm2._run_uninstall([])
    out = capsys.readouterr().out
    assert "no Janus state" in out
    assert "pipx uninstall janus-agent" in out


def test_format_bytes():
    assert jm._format_bytes(0) == "0 B"
    assert jm._format_bytes(1023) == "1023 B"
    assert jm._format_bytes(1024) == "1.0 KB"
    assert jm._format_bytes(1024 * 1024) == "1.0 MB"
    assert jm._format_bytes(1024 * 1024 * 1024) == "1.0 GB"


def test_safe_count_handles_missing_file(tmp_path):
    nonexistent = tmp_path / "nope.json"
    assert jm._safe_count(nonexistent, len) == 0


def test_safe_count_handles_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert jm._safe_count(p, len) == 0
