"""skill_catalog.py — install bundled skills into ~/.janus/skills/.

The "bundled catalog" is a set of .md skill files (and optional
directory-form skills with SKILL.md + scripts/) shipped inside the
Janus package at janus/skills_bundled/. On first run (or via the
/skills install-bundled command), these are copied into the user's
~/.janus/skills/ so they have a starter catalog they can /promote.

INVARIANT (P4): every installed skill lands as `state: quarantined`,
regardless of what the bundled SKILL.md says. We re-enforce on copy
to defend against typos in the catalog source.

Idempotent: re-running install_bundled() does NOT clobber files the
user has edited or promoted — copies only happen when the target
does not already exist.

Filter helper: list_skills() output can grow past 30+ entries once
the catalog is shipped, so filter_skills() pairs with a `/skills <q>`
listing to keep the TUI scannable.
"""

from __future__ import annotations
import shutil
from pathlib import Path
from typing import Iterable

from . import config, skills, skills_market


# Marker file under ~/.janus/ that records "first-run install ran".
# Once present, the first-run hook in __main__.py won't re-trigger.
_INSTALLED_MARKER_NAME = "bundled_skills_installed.txt"


def bundled_dir() -> Path:
    """Path to the in-repo bundled-skills directory."""
    return Path(__file__).resolve().parent / "skills_bundled"


def iter_bundled_sources() -> list[Path]:
    """Every bundled skill source: top-level .md or directory-form (SKILL.md inside).

    Sorted by name for stable install ordering.
    """
    base = bundled_dir()
    if not base.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_file() and entry.suffix.lower() == ".md":
            out.append(entry)
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            out.append(entry)
    return out


def install_bundled(*, force: bool = False) -> dict:
    """Copy every bundled skill into ~/.janus/skills/, skipping ones
    that already exist (unless force=True).

    Returns {"installed": [names], "skipped": [names], "errors": [(name, msg)]}.

    Quarantine is re-enforced on copy via skills_market._force_quarantine,
    so a typo in a bundled SKILL.md cannot ship a pre-promoted skill.
    """
    config.ensure_home()
    target_root = config.SKILLS_DIR
    target_root.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []

    for src in iter_bundled_sources():
        name = _name_from_source(src)
        try:
            if src.is_file():
                _install_file(src, target_root, force=force, installed=installed,
                              skipped=skipped, name=name)
            else:
                _install_directory(src, target_root, force=force, installed=installed,
                                   skipped=skipped, name=name)
        except Exception as e:  # P8: errors are observations
            errors.append((name, f"{type(e).__name__}: {e}"))

    if not errors:
        _mark_installed()
    return {"installed": installed, "skipped": skipped, "errors": errors}


def _install_file(src: Path, target_root: Path, *, force: bool,
                  installed: list[str], skipped: list[str], name: str) -> None:
    target = target_root / src.name
    if target.exists() and not force:
        skipped.append(name)
        return
    rendered, _ = skills_market._force_quarantine(
        src.read_text(encoding="utf-8")
    )
    target.write_text(rendered, encoding="utf-8")
    installed.append(name)


def _install_directory(src: Path, target_root: Path, *, force: bool,
                       installed: list[str], skipped: list[str], name: str) -> None:
    target = target_root / src.name
    if target.exists() and not force:
        skipped.append(name)
        return
    if target.exists() and force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name == "SKILL.md":
            continue
        dst = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dst, dirs_exist_ok=False)
        else:
            shutil.copy2(entry, dst)
    rendered, _ = skills_market._force_quarantine(
        (src / "SKILL.md").read_text(encoding="utf-8")
    )
    (target / "SKILL.md").write_text(rendered, encoding="utf-8")
    installed.append(name)


def _name_from_source(src: Path) -> str:
    return src.stem if src.is_file() else src.name


# ---------- First-run detection ----------


def _marker_path() -> Path:
    return config.HOME / _INSTALLED_MARKER_NAME


def has_been_installed() -> bool:
    return _marker_path().is_file()


def _mark_installed() -> None:
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        _marker_path().write_text("ok\n", encoding="utf-8")
    except OSError:
        pass


def is_first_run() -> bool:
    """True when no marker AND the user has no skills.

    Treats a deleted ~/.janus/skills/ with the marker still present as
    "the user knows what they're doing — leave it empty."
    """
    if has_been_installed():
        return False
    config.ensure_home()
    return not any(config.SKILLS_DIR.rglob("*.md"))


# ---------- Filter (for /skills <query>) ----------


def filter_skills(items: Iterable, query: str) -> list:
    """Case-insensitive substring filter on name + description.

    Used by /skills <query> to keep the TUI listing scannable past
    30+ entries. Empty query returns all.
    """
    q = (query or "").strip().lower()
    if not q:
        return list(items)
    out = []
    for s in items:
        hay = (str(getattr(s, "name", "")) + " " +
               str(getattr(s, "description", ""))).lower()
        if q in hay:
            out.append(s)
    return out
