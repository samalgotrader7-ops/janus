"""
backup.py — janus backup / restore (v1.33.1, Phase 6.2).

WHY THIS EXISTS:
A production VPS instance accumulates valuable state in ~/.janus/:
memory cards, conversations, skills, MCP config, persistent
grants. Pre-v1.33.1 the only way to back it up was a manual `tar`
command. v1.33.1 ships `janus backup` and `janus restore` so the
backup is one command and lives next to the install.

USAGE:
  janus backup                           # writes ~/.janus/backups/<ts>.tar.gz
  janus backup --output /backups/x.tgz   # custom path
  janus backup --include-logs            # include cost/log (large)
  janus restore /backups/x.tgz           # restore from archive
  janus restore /backups/x.tgz --force   # overwrite existing files

WHAT'S BACKED UP (default):
  ~/.janus/  except for these excluded paths (large + reproducible):
    cost.jsonl       — cost log (regenerated as you use Janus)
    log.jsonl        — interaction log (audit trail; regenerated)
    backups/         — don't recursively back up backups
    sessions.db      — FTS5 search index (rebuilt on demand)
    shells/          — temp shell sessions
    uploads/         — temp file uploads

  --include-logs adds cost.jsonl + log.jsonl to the archive.

WHAT'S NOT IN SCOPE for v1.33.1:
  * Cloud upload (S3 / B2) — landing in a future v1.33.x point release
  * Encryption at rest — use age / gpg externally
  * Incremental backups — full snapshot only

P5 (plain-text state): the archive is a standard tar.gz, openable
with `tar -tzvf` so you can see exactly what's inside. No magic.
"""

from __future__ import annotations

import os
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import config


# ---------- Default exclusion list ----------

# Paths within HOME excluded from backup by default. Either a
# literal filename (matched against entry.name basename) OR a
# directory name (matched against any path component).
DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    # Reproducible / large logs
    "cost.jsonl",
    "log.jsonl",
    # Backup directory itself (avoid recursive backup-of-backups)
    "backups",
    # Indexes that rebuild from source
    "sessions.db",
    # Temp dirs
    "shells",
    "uploads",
)

# When --include-logs is passed, these come back in.
LOG_EXCLUSIONS: tuple[str, ...] = ("cost.jsonl", "log.jsonl")


@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    file_count: int
    total_bytes: int
    skipped_count: int


def _should_exclude(rel_path: Path, exclusions: set[str]) -> bool:
    """Return True if any path component or the basename is in the
    exclusion set."""
    if rel_path.name in exclusions:
        return True
    for part in rel_path.parts:
        if part in exclusions:
            return True
    return False


def make_backup(
    *,
    home: Path | None = None,
    output: Path | None = None,
    include_logs: bool = False,
    timestamp: str | None = None,
) -> BackupResult:
    """Create a tar.gz of HOME under output (default: HOME/backups/
    <timestamp>.tar.gz). Returns a BackupResult with metadata."""
    if home is None:
        home = config.HOME
    home = Path(home)
    if not home.exists():
        raise FileNotFoundError(f"home directory not found: {home}")

    # Build the exclusion set.
    exclusions = set(DEFAULT_EXCLUSIONS)
    if include_logs:
        exclusions -= set(LOG_EXCLUSIONS)

    # Resolve output path.
    if output is None:
        ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
        backups_dir = home / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        output = backups_dir / f"janus_backup_{ts}.tar.gz"
    else:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

    file_count = 0
    skipped_count = 0
    total_bytes = 0

    with tarfile.open(output, "w:gz") as tar:
        for path in sorted(home.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(home)
            if _should_exclude(rel, exclusions):
                skipped_count += 1
                continue
            # Sanity: don't back up the archive we're writing
            try:
                if path.resolve() == output.resolve():
                    continue
            except OSError:
                pass
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total_bytes += size
            file_count += 1
            tar.add(path, arcname=str(rel))

    return BackupResult(
        archive_path=output,
        file_count=file_count,
        total_bytes=total_bytes,
        skipped_count=skipped_count,
    )


def restore_backup(
    archive: Path,
    *,
    home: Path | None = None,
    force: bool = False,
) -> int:
    """Extract `archive` into HOME. Returns the count of extracted
    files. Refuses to overwrite existing files unless force=True."""
    if home is None:
        home = config.HOME
    home = Path(home)
    archive = Path(archive)
    if not archive.exists():
        raise FileNotFoundError(f"archive not found: {archive}")
    home.mkdir(parents=True, exist_ok=True)

    extracted = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = list(tar.getmembers())
        if not force:
            # Pre-check: refuse if any member would overwrite an
            # existing file (avoid clobbering live state).
            conflicts: list[str] = []
            for m in members:
                target = home / m.name
                if target.exists() and target.is_file():
                    conflicts.append(m.name)
                    if len(conflicts) >= 10:
                        break
            if conflicts:
                raise FileExistsError(
                    f"refused to overwrite {len(conflicts)} file(s) "
                    f"(showing first 10): {', '.join(conflicts)}. "
                    f"Pass --force to overwrite."
                )
        for m in members:
            # Path-traversal defense: refuse anything that resolves
            # outside HOME.
            target = (home / m.name).resolve()
            try:
                target.relative_to(home.resolve())
            except ValueError:
                continue  # silently skip path-traversal attempts
            tar.extract(m, path=str(home))
            extracted += 1
    return extracted


# ---------- CLI dispatch ----------


def cmd_backup(args: list[str]) -> int:
    """`janus backup [--output PATH] [--include-logs]`"""
    output_path: Path | None = None
    include_logs = False
    i = 0
    while i < len(args):
        flag = args[i]
        if flag == "--output":
            try:
                output_path = Path(args[i + 1])
                i += 2
            except IndexError:
                sys.stderr.write("error: --output requires a value\n")
                return 2
        elif flag == "--include-logs":
            include_logs = True
            i += 1
        elif flag in ("-h", "--help"):
            sys.stdout.write(
                "usage: janus backup [--output PATH] [--include-logs]\n"
                "  Creates a tar.gz of ~/.janus/ excluding cost/log/cache.\n"
            )
            return 0
        else:
            sys.stderr.write(f"error: unknown flag {flag!r}\n")
            return 2
    try:
        result = make_backup(output=output_path, include_logs=include_logs)
    except Exception as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1
    mb = result.total_bytes / (1024 * 1024)
    sys.stdout.write(
        f"wrote {result.archive_path}\n"
        f"  {result.file_count} file(s), "
        f"{mb:.1f} MB uncompressed, "
        f"{result.skipped_count} skipped\n"
    )
    return 0


def cmd_restore(args: list[str]) -> int:
    """`janus restore <archive> [--force]`"""
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(
            "usage: janus restore <archive> [--force]\n"
            "  Extract a janus backup tar.gz into ~/.janus/.\n"
            "  --force overwrites existing files.\n"
        )
        return 0 if args and args[0] in ("-h", "--help") else 2
    archive = Path(args[0])
    force = "--force" in args[1:]
    try:
        n = restore_backup(archive, force=force)
    except FileExistsError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1
    sys.stdout.write(f"restored {n} file(s) from {archive}\n")
    return 0
