"""Tests for v1.33.1 — janus backup / restore (Phase 6.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import backup


@pytest.fixture
def fake_home(tmp_path):
    """Build a representative ~/.janus/ tree to back up."""
    home = tmp_path / ".janus"
    home.mkdir()
    # Files we want preserved
    (home / "memory").mkdir()
    (home / "memory" / "MEMORY.md").write_text("# memory")
    (home / "memory" / "user_role.md").write_text("---\nname: x\n---")
    (home / "skills").mkdir()
    (home / "skills" / "git-pr-review").mkdir()
    (home / "skills" / "git-pr-review" / "SKILL.md").write_text("skill body")
    (home / "conversations").mkdir()
    (home / "conversations" / "abc.json").write_text("{}")
    (home / "mcp").mkdir()
    (home / "mcp" / "servers.json").write_text('{"mcpServers":{}}')
    (home / "approvals.json").write_text('{}')
    # Files that should be excluded by default
    (home / "cost.jsonl").write_text("\n".join(["{}"] * 100))
    (home / "log.jsonl").write_text("\n".join(["{}"] * 100))
    (home / "sessions.db").write_bytes(b"\x00" * 1000)
    (home / "shells").mkdir()
    (home / "shells" / "tmp.sh").write_text("# temp")
    (home / "uploads").mkdir()
    (home / "uploads" / "blob.bin").write_bytes(b"\x00" * 100)
    return home


# -------------------- make_backup --------------------


def test_backup_creates_archive(fake_home):
    result = backup.make_backup(home=fake_home, timestamp="test")
    assert result.archive_path.exists()
    assert result.archive_path.suffix == ".gz"
    assert result.archive_path.name.endswith("test.tar.gz")


def test_backup_excludes_logs_by_default(fake_home, tmp_path):
    """cost.jsonl + log.jsonl + sessions.db + shells/ + uploads/
    excluded by default."""
    out = tmp_path / "out.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    import tarfile
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "cost.jsonl" not in names
    assert "log.jsonl" not in names
    assert "sessions.db" not in names
    assert not any(n.startswith("shells/") for n in names)
    assert not any(n.startswith("uploads/") for n in names)


def test_backup_includes_critical_state(fake_home, tmp_path):
    """memory/, skills/, conversations/, mcp/, approvals.json all
    survive the default backup."""
    out = tmp_path / "out.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    import tarfile
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "memory/MEMORY.md" in names
    assert "memory/user_role.md" in names
    assert "skills/git-pr-review/SKILL.md" in names
    assert "conversations/abc.json" in names
    assert "mcp/servers.json" in names
    assert "approvals.json" in names


def test_backup_include_logs_flag(fake_home, tmp_path):
    """--include-logs adds cost.jsonl + log.jsonl back."""
    out = tmp_path / "out.tar.gz"
    backup.make_backup(home=fake_home, output=out, include_logs=True)
    import tarfile
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "cost.jsonl" in names
    assert "log.jsonl" in names
    # sessions.db / shells / uploads still excluded
    assert "sessions.db" not in names


def test_backup_excludes_backups_dir(fake_home, tmp_path):
    """A backups/ dir inside HOME shouldn't be recursively included
    (would cause exponential growth on repeated runs)."""
    (fake_home / "backups").mkdir()
    (fake_home / "backups" / "old.tar.gz").write_bytes(b"old")
    out = tmp_path / "out.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    import tarfile
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert not any(n.startswith("backups/") for n in names)


def test_backup_default_output_in_home_backups(fake_home):
    """Without --output, archive lands in HOME/backups/."""
    result = backup.make_backup(home=fake_home, timestamp="auto")
    assert result.archive_path.parent == fake_home / "backups"
    assert "auto" in result.archive_path.name


def test_backup_records_file_count_and_bytes(fake_home, tmp_path):
    out = tmp_path / "out.tar.gz"
    result = backup.make_backup(home=fake_home, output=out)
    assert result.file_count > 0
    assert result.total_bytes > 0
    assert result.skipped_count > 0  # cost.jsonl + log.jsonl + ...


def test_backup_raises_on_missing_home(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        backup.make_backup(home=missing)


# -------------------- restore_backup --------------------


def test_restore_extracts_to_home(fake_home, tmp_path):
    """Round-trip: backup → fresh home → restore → same files."""
    out = tmp_path / "snap.tar.gz"
    backup.make_backup(home=fake_home, output=out)

    fresh = tmp_path / "restored"
    n = backup.restore_backup(out, home=fresh)
    assert n > 0
    assert (fresh / "memory" / "MEMORY.md").exists()
    assert (fresh / "skills" / "git-pr-review" / "SKILL.md").exists()
    assert (fresh / "approvals.json").exists()


def test_restore_refuses_to_overwrite_without_force(fake_home, tmp_path):
    """Existing files are protected by default — pass --force to
    overwrite."""
    out = tmp_path / "snap.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    target = tmp_path / "restored"
    target.mkdir()
    (target / "memory").mkdir()
    (target / "memory" / "MEMORY.md").write_text("DO NOT OVERWRITE")

    with pytest.raises(FileExistsError):
        backup.restore_backup(out, home=target)


def test_restore_force_overwrites(fake_home, tmp_path):
    out = tmp_path / "snap.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    target = tmp_path / "restored"
    target.mkdir()
    (target / "memory").mkdir()
    (target / "memory" / "MEMORY.md").write_text("OLD")

    n = backup.restore_backup(out, home=target, force=True)
    assert n > 0
    # Now contains backed-up content
    assert (target / "memory" / "MEMORY.md").read_text() == "# memory"


def test_restore_raises_on_missing_archive(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.restore_backup(tmp_path / "nope.tar.gz")


# -------------------- _should_exclude --------------------


def test_should_exclude_matches_basename():
    assert backup._should_exclude(Path("cost.jsonl"), {"cost.jsonl"})
    assert not backup._should_exclude(Path("cost.txt"), {"cost.jsonl"})


def test_should_exclude_matches_directory_component():
    assert backup._should_exclude(Path("backups/x.tar.gz"), {"backups"})
    assert backup._should_exclude(Path("shells/abc/log.txt"), {"shells"})


# -------------------- CLI dispatch --------------------


def test_cmd_backup_no_args_succeeds(fake_home, monkeypatch, capsys):
    """With default config.HOME redirected, cmd_backup runs end-to-end."""
    from janus import config as _cfg
    monkeypatch.setattr(_cfg, "HOME", fake_home)
    rc = backup.cmd_backup([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "wrote" in captured.out


def test_cmd_backup_unknown_flag_errors(capsys):
    rc = backup.cmd_backup(["--bogus"])
    assert rc == 2


def test_cmd_backup_help_succeeds(capsys):
    rc = backup.cmd_backup(["--help"])
    assert rc == 0


def test_cmd_restore_no_args_errors(capsys):
    rc = backup.cmd_restore([])
    assert rc == 2


def test_cmd_restore_help_succeeds(capsys):
    rc = backup.cmd_restore(["--help"])
    assert rc == 0


def test_cmd_restore_round_trip(fake_home, monkeypatch, tmp_path, capsys):
    """End-to-end: backup → write to disk → restore via CLI."""
    out = tmp_path / "snap.tar.gz"
    backup.make_backup(home=fake_home, output=out)
    fresh = tmp_path / "fresh"
    from janus import config as _cfg
    monkeypatch.setattr(_cfg, "HOME", fresh)
    rc = backup.cmd_restore([str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "restored" in captured.out


# -------------------- __main__ wiring --------------------


def test_main_dispatches_backup_subcommand():
    main_path = Path(backup.__file__).parent / "__main__.py"
    src = main_path.read_text(encoding="utf-8")
    assert 'sub == "backup"' in src
    assert 'sub == "restore"' in src
    assert "from . import backup" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_33_1_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 1)
