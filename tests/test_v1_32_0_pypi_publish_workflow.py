"""Tests for v1.32.0 — PyPI publish workflow (Phase 5.1).

WHAT THIS SHIPS:
A GitHub Actions workflow at .github/workflows/publish-pypi.yml
that builds sdist + wheel on every tag push and uploads to PyPI
when PYPI_API_TOKEN is configured. The workflow is dormant
(builds and validates only) without the secret — never errors out
loudly when the token is missing.

WHY THIS IS PHASE 5 ITEM 1:
Distribution. Janus currently installs via `pipx install
git+https://github.com/...`. Phase 5.1 changes that to
`pipx install janus-agent`.

DESIGN INVARIANTS PINNED:
  * Workflow file exists at .github/workflows/publish-pypi.yml
  * Triggers on tag push (v*.*.*) and workflow_dispatch
  * Build job validates tag matches pyproject.toml version
  * Build runs `python -m build` and `twine check`
  * Publish job is conditional — only on tag push, not dispatch
  * Publish step gracefully no-ops without PYPI_API_TOKEN
  * Uses --skip-existing on twine upload (idempotent retries)
  * Pinned action versions (no @latest, no @main)
  * Local `python -m build` works against the current pyproject.toml
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


WORKFLOW_PATH = (
    Path(__file__).parent.parent / ".github" / "workflows" / "publish-pypi.yml"
)


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow_yaml():
    """Parsed YAML — fails the test suite if YAML doesn't parse."""
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


# -------------------- File-level pins --------------------


def test_workflow_file_exists():
    assert WORKFLOW_PATH.exists()


def test_workflow_yaml_parses(workflow_yaml):
    """The file is valid YAML. (importorskip on yaml above means the
    test self-skips when PyYAML isn't installed, which is fine for
    contributors without the dev extras.)"""
    assert isinstance(workflow_yaml, dict)
    assert "jobs" in workflow_yaml


def test_workflow_has_build_and_publish_jobs(workflow_yaml):
    """Two-job split: build always runs (gives early visibility on
    bad pyproject), publish only fires on tag push."""
    assert "build" in workflow_yaml["jobs"]
    assert "publish" in workflow_yaml["jobs"]


def test_workflow_triggers_on_version_tags(workflow_yaml):
    """Triggers on v*.*.* tag pushes. Workflow_dispatch added for
    manual dry-runs."""
    # `on:` is a special key; pyyaml may parse `on` as bool True if
    # not quoted in source. Handle both shapes.
    triggers = workflow_yaml.get("on") or workflow_yaml.get(True)
    assert triggers is not None, "workflow has no triggers"
    assert "push" in triggers
    assert "tags" in triggers["push"]
    tags = triggers["push"]["tags"]
    assert any("v*" in t for t in tags)


def test_workflow_supports_manual_dispatch(workflow_yaml):
    """workflow_dispatch lets the user dry-run from the Actions UI."""
    triggers = workflow_yaml.get("on") or workflow_yaml.get(True)
    assert "workflow_dispatch" in triggers


# -------------------- Build job pins --------------------


def test_build_job_uses_pinned_actions(workflow_text):
    """Pin to @v4 not @main / @latest — supply chain protection."""
    assert "actions/checkout@v4" in workflow_text
    assert "actions/setup-python@v5" in workflow_text
    assert "actions/upload-artifact@v4" in workflow_text


def test_build_job_validates_tag_matches_version(workflow_text):
    """Catch the foot-gun where Sam tags v1.32.0 but forgets to
    bump pyproject — would otherwise upload a wheel with the wrong
    version forever."""
    # The validation block should reference both pyproject.toml
    # and the GITHUB_REF tag.
    assert "pyproject.toml" in workflow_text
    assert "GITHUB_REF" in workflow_text
    assert "Tag '" in workflow_text or "Tag $" in workflow_text


def test_build_job_runs_python_minus_m_build(workflow_text):
    """Standard build invocation — produces both sdist and wheel."""
    assert "python -m build" in workflow_text


def test_build_job_runs_twine_check(workflow_text):
    """twine check catches malformed METADATA before upload."""
    assert "twine check dist/*" in workflow_text


# -------------------- Publish job pins --------------------


def test_publish_job_only_on_tag_push(workflow_yaml):
    """Manual workflow_dispatch should NEVER auto-publish — that'd
    publish whatever is on main, which may not be a tagged release."""
    publish = workflow_yaml["jobs"]["publish"]
    assert "if" in publish
    if_expr = publish["if"]
    assert "tags" in if_expr or "refs/tags" in if_expr
    assert "push" in if_expr


def test_publish_handles_missing_secret_gracefully(workflow_text):
    """Without PYPI_API_TOKEN, the publish step prints a warning
    and exits 0 — workflow stays green, just dormant. This lets
    the workflow ship before Sam configures the secret."""
    # The shell script checks if TWINE_PASSWORD is empty.
    assert "TWINE_PASSWORD" in workflow_text
    # And exits 0 (success) when missing — not 1 (failure).
    # Look for the actual step (anchor on "- name: Publish to PyPI"
    # — the dash prefix distinguishes it from the workflow's top-level
    # `name:` field on line 1).
    step_idx = workflow_text.index("- name: Publish to PyPI")
    step_block = workflow_text[step_idx: step_idx + 2000]
    assert "exit 0" in step_block
    assert "DORMANT" in step_block or "dormant" in step_block.lower()


def test_publish_uses_skip_existing(workflow_text):
    """--skip-existing means a re-run on the same version doesn't
    400-error against an already-uploaded distribution. Important
    for retry safety."""
    assert "--skip-existing" in workflow_text


def test_publish_uses_token_username(workflow_text):
    """PyPI API tokens require username='__token__'."""
    assert "__token__" in workflow_text
    assert "PYPI_API_TOKEN" in workflow_text


# -------------------- Documentation pins --------------------


def test_workflow_documents_secret_setup(workflow_text):
    """Comments explain how to enable real publishing — Sam can
    follow the steps without external lookup."""
    assert "PYPI_API_TOKEN" in workflow_text
    assert "Settings" in workflow_text or "secret" in workflow_text.lower()
    assert "pypi.org" in workflow_text.lower()


# -------------------- Behavioral pin: build actually works --------------------


def test_pyproject_buildable_locally(tmp_path):
    """Source-pin: the package can be built RIGHT NOW with
    `python -m build`. This test BUILDS the package into a temp
    dir to prove the workflow won't fail on an actual tag push.

    Note: this test is a bit slower (~5s) but catches pyproject
    breakage that source-pin tests can't see."""
    import subprocess
    import sys

    # Build into the temp dir (not tampering with the repo's dist/).
    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path), str(repo_root)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"`python -m build` failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    # Both sdist and wheel produced.
    products = list(tmp_path.iterdir())
    assert any(p.name.endswith(".whl") for p in products), (
        f"no wheel produced; got: {[p.name for p in products]}"
    )
    assert any(p.name.endswith(".tar.gz") for p in products), (
        f"no sdist produced; got: {[p.name for p in products]}"
    )


def test_version_bumped_to_1_32_0_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 0)
