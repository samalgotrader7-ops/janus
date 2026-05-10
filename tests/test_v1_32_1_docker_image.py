"""Tests for v1.32.1 — Docker image + GHCR publish workflow (Phase 5.2).

WHAT THIS SHIPS:
  * Dockerfile (single-stage, python:3.12-slim base)
  * docker-compose.yml with three services sharing a volume
  * .github/workflows/publish-docker.yml — auto-publish on tag push

DESIGN INVARIANTS PINNED:
  * Dockerfile uses a slim Python base (small image)
  * Installs the [all] extras (web + telegram + browser + tui + rich)
  * PYTHONUNBUFFERED=1 (no stdout buffering inside the container)
  * Exposes 8765 (the web UI port)
  * VOLUME on /root/.janus for persistence
  * ENTRYPOINT=janus + CMD=web (overrides via docker run cmd)
  * docker-compose has the three services + shared named volume
  * compose.healthcheck on web service
  * Workflow uses GITHUB_TOKEN (no extra secret setup)
  * Workflow tags: full version + major.minor + latest
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-docker.yml"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# -------------------- Dockerfile pins --------------------


def test_dockerfile_exists():
    assert DOCKERFILE.exists()


def test_dockerfile_uses_slim_python_base(dockerfile_text):
    """Slim base — small image, fast pulls. Avoid full python:3.12
    (~1GB) or alpine (musl breaks some deps)."""
    assert "FROM python:3.12-slim" in dockerfile_text


def test_dockerfile_sets_pythonunbuffered(dockerfile_text):
    """Same redirected-stdout block-buffering issue v1.31.15+v1.31.17
    fixed for nohup users — Docker's log driver wants line-by-line
    output, so set this at the env level."""
    assert "PYTHONUNBUFFERED=1" in dockerfile_text


def test_dockerfile_installs_all_extras(dockerfile_text):
    """Image must work for web / telegram / daemon — installing
    only the core would leave [telegram] / [web] subcommands
    failing with 'extra not installed' errors."""
    assert "'.[all]'" in dockerfile_text or '".[all]"' in dockerfile_text


def test_dockerfile_exposes_web_port(dockerfile_text):
    """Web UI default port — must be EXPOSED for compose to map it
    automatically."""
    assert "EXPOSE 8765" in dockerfile_text


def test_dockerfile_declares_state_volume(dockerfile_text):
    """VOLUME makes the persistent state path explicit. Without it,
    docker run overlay layers swallow ~/.janus/ on container removal."""
    assert 'VOLUME ["/root/.janus"]' in dockerfile_text or \
        'VOLUME /root/.janus' in dockerfile_text


def test_dockerfile_entrypoint_is_janus(dockerfile_text):
    """ENTRYPOINT janus + CMD web means `docker run image`
    defaults to web; `docker run image telegram` overrides."""
    assert 'ENTRYPOINT ["janus"]' in dockerfile_text
    assert 'CMD ["web"]' in dockerfile_text


def test_dockerfile_layer_caching_split(dockerfile_text):
    """Two-step COPY: pyproject + metadata first (rarely changes),
    janus/ source second (changes often). Lets docker reuse the
    dep-install layer across normal source edits."""
    assert "COPY pyproject.toml" in dockerfile_text
    assert "COPY janus/" in dockerfile_text
    # Order matters
    pyproject_idx = dockerfile_text.index("COPY pyproject.toml")
    janus_idx = dockerfile_text.index("COPY janus/")
    assert pyproject_idx < janus_idx


def test_dockerfile_has_oci_labels(dockerfile_text):
    """OCI image labels populate registry landing pages (ghcr.io
    shows description + source link)."""
    assert "org.opencontainers.image.title" in dockerfile_text
    assert "org.opencontainers.image.source" in dockerfile_text
    assert "org.opencontainers.image.licenses" in dockerfile_text


# -------------------- docker-compose pins --------------------


def test_compose_file_exists():
    assert COMPOSE_FILE.exists()


def test_compose_has_three_services(compose_text):
    """All three managed services match the systemd-units approach
    install_services.sh sets up: web + telegram + daemon."""
    assert "janus-web:" in compose_text
    assert "janus-telegram:" in compose_text
    assert "janus-daemon:" in compose_text


def test_compose_uses_named_volume(compose_text):
    """Named volume `janus-data` is shared by all three services
    so memory / skills / conversations are visible across them."""
    assert "janus-data:" in compose_text
    assert ":/root/.janus" in compose_text


def test_compose_has_healthcheck_on_web(compose_text):
    """Web is the user-facing entry point — healthcheck lets
    compose-aware orchestrators (Portainer, Swarm) detect failures."""
    assert "healthcheck:" in compose_text
    # Should curl the login endpoint (no auth needed for the page itself)
    assert "/login" in compose_text


def test_compose_uses_env_file(compose_text):
    """Env file pattern matches the systemd EnvironmentFile setup —
    so users can copy the same .env between bare-metal and docker
    deployments."""
    assert "env_file:" in compose_text
    assert ".env" in compose_text


def test_compose_image_points_to_ghcr(compose_text):
    """Image tag matches the GHCR namespace this workflow publishes
    to. Pin so the tag doesn't drift."""
    assert "ghcr.io/samalgotrader7-ops/janus" in compose_text


def test_compose_restart_policy(compose_text):
    """unless-stopped means containers restart on host reboot but
    NOT after explicit `docker compose stop` — matches systemd
    Restart=on-failure semantics."""
    assert "restart: unless-stopped" in compose_text


# -------------------- Docker workflow pins --------------------


def test_docker_workflow_exists():
    assert WORKFLOW.exists()


def test_docker_workflow_triggers_on_tags(workflow_text):
    """Same trigger pattern as the PyPI workflow — auto-publish
    on every release tag."""
    assert "tags:" in workflow_text
    assert "'v*.*.*'" in workflow_text or '"v*.*.*"' in workflow_text


def test_docker_workflow_uses_pinned_actions(workflow_text):
    """Pin all actions to specific majors — supply-chain protection."""
    assert "actions/checkout@v4" in workflow_text
    assert "docker/setup-buildx-action@v3" in workflow_text
    assert "docker/login-action@v3" in workflow_text
    assert "docker/metadata-action@v5" in workflow_text
    assert "docker/build-push-action@v5" in workflow_text


def test_docker_workflow_publishes_to_ghcr(workflow_text):
    """ghcr.io target uses GITHUB_TOKEN — no extra secret setup."""
    assert "ghcr.io" in workflow_text
    assert "${{ secrets.GITHUB_TOKEN }}" in workflow_text
    # Image name uses the repo to support fork-friendliness
    assert "ghcr.io/${{ github.repository }}" in workflow_text


def test_docker_workflow_tags_full_version_major_minor_latest(workflow_text):
    """metadata-action emits all three tag flavors so users can pin
    by exact version, by major.minor (auto-receives patch updates),
    or just track latest."""
    assert "type=semver,pattern={{version}}" in workflow_text
    assert "type=semver,pattern={{major}}.{{minor}}" in workflow_text
    assert "type=raw,value=latest" in workflow_text


def test_docker_workflow_uses_packages_write_permission(workflow_text):
    """ghcr.io push needs the `packages: write` permission — without
    it the push step fails with a 403."""
    assert "packages: write" in workflow_text


def test_docker_workflow_uses_gha_cache(workflow_text):
    """Docker layer cache via GitHub Actions — speeds up builds
    on repeated tags by reusing unchanged layers."""
    assert "cache-from: type=gha" in workflow_text
    assert "cache-to: type=gha,mode=max" in workflow_text


# -------------------- Version pin --------------------


def test_version_bumped_to_1_32_1_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 1)
