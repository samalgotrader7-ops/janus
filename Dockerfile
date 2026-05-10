# Janus — Dockerfile (Phase 5.2 — Distribution).
#
# Single-stage image based on python:3.12-slim. Installs Janus with
# the [all] extras so any subcommand (web / telegram / daemon / TUI)
# works out of the box. Final image size ~500 MB.
#
# USAGE:
#   docker run --rm -it -p 8765:8765 \
#     -v janus-data:/root/.janus \
#     --env-file .env \
#     ghcr.io/samalgotrader7-ops/janus:latest web
#
#   docker run --rm -it \
#     -v janus-data:/root/.janus \
#     --env-file .env \
#     ghcr.io/samalgotrader7-ops/janus:latest telegram
#
#   See docker-compose.yml for the recommended 3-service deployment.
#
# PERSISTENT STATE: bind / volume-mount /root/.janus so memory,
# skills, conversations, and cost log survive across container
# restarts. Without the mount, every restart starts fresh.
#
# CONFIG: pass --env-file or individual -e flags. The container
# reads the same JANUS_* env vars as a local install. Write to
# /root/.janus/.env if you prefer file-based config (the runtime
# loads .env from that path automatically).

FROM python:3.12-slim AS runtime

# Build metadata — surfaced via `docker inspect` and image landing
# pages on registries that render OCI labels.
LABEL org.opencontainers.image.title="Janus"
LABEL org.opencontainers.image.description="Claude Code's UX, on any model, with plain-text state and a learning loop."
LABEL org.opencontainers.image.source="https://github.com/samalgotrader7-ops/janus"
LABEL org.opencontainers.image.licenses="MIT"

# Don't write .pyc to the image — wastes space + creates churn in
# layer caches across rebuilds. Don't buffer stdout — solves the
# same redirected-stdout block-buffering issue v1.31.15+v1.31.17
# fixed for nohup users; Docker's log driver wants line-by-line.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Minimal OS packages — git is needed for some MCP servers, curl for
# health probes / debug. Keep this list small to keep the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/janus

# Two-step COPY: bring metadata first so changes to src/ don't
# invalidate the dep-install cache layer.
COPY pyproject.toml README.md LICENSE /opt/janus/
COPY janus/ /opt/janus/janus/

# Install Janus + ALL optional extras (web / telegram / browser /
# tui / rich) so any subcommand the user picks works without an
# extra `pip install` step. The [all] extra is defined in
# pyproject.toml.
RUN pip install --no-cache-dir '.[all]'

# Web UI port. Set JANUS_WEB_HOST=0.0.0.0 + JANUS_WEB_HOST_OK=1 in
# the env to actually bind it externally — the runtime refuses
# non-localhost binds without the OK flag (safety gate).
EXPOSE 8765

# State directory — mount a volume here for persistence.
VOLUME ["/root/.janus"]

# `janus` is the entry; users override the subcommand via the
# docker `cmd` arg (default: web).
ENTRYPOINT ["janus"]
CMD ["web"]
