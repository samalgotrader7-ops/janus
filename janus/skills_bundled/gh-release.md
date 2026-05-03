---
name: gh-release
description: Cut a GitHub release — version bump, tag, changelog, and release notes from commit history.
state: quarantined
capabilities:
  shell.exec:
    - "git log*"
    - "git tag*"
    - "git push*"
    - "gh release *"
    - "gh api *"
  fs.read:
    - "**"
  fs.write:
    - "CHANGELOG*"
    - "VERSION*"
    - "**/__init__.py"
    - "package.json"
    - "Cargo.toml"
    - "pyproject.toml"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running gh-release.

You prepare and ship a versioned release: choose the version, update
the version string in canonical files, write release notes from the
commit log, tag, and publish.

Steps:
1. `git tag --sort=-version:refname | head -5` — find the latest tag.
2. `git log <latest-tag>..HEAD --oneline` — read every commit since the
   last release. Group by category (feat, fix, refactor, docs, chore).
3. Propose a version bump (semver: breaking → major, feature → minor,
   fix → patch). Confirm with the user if any breaking change is present.
4. Update the version string in the canonical file (pyproject.toml /
   package.json / Cargo.toml / __init__.py — read first, edit second).
5. Update CHANGELOG.md with the grouped commit log under the new version.
6. Commit: `git commit -m "release X.Y.Z"`. Tag: `git tag vX.Y.Z`.
   Push: `git push && git push --tags`.
7. `gh release create vX.Y.Z --title "X.Y.Z" --notes-file <changelog-section>`.

Never release with uncommitted changes. Never re-tag an existing version.
Never `--force` push tags. Surface security-relevant fixes prominently in
the release notes.
