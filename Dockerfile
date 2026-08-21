# syntax=docker/dockerfile:1

# Multi-stage so the final image has Python and the project, not a compiler
# and not uv. The lockfile is the source of truth: a lockfile/pyproject change
# rebuilds the dependency layer, a source-only change does not.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.2

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-trixie AS builder
COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1

WORKDIR /app

# Dependencies first, so a code-only change does not reinstall the world.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable

COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

# --no-editable installs a real wheel, so the final stage can copy only .venv
# and still have a working `tetherd` console script.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:${PYTHON_VERSION}-slim-trixie

# tini so SIGTERM from `docker stop` reaches the daemon rather than being
# swallowed by a shell. tzdata so TZ=Europe/London actually means that.
# ca-certificates so Apprise can talk HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# The process talks to the host Docker socket. That socket is typically
# root:docker 660, and Unraid users will not remember --group-add. Running as
# root here is the same trade-off Watchtower and Rebuild-DNDC made: the socket
# *is* the product. Do not "improve" this into a non-root user without also
# shipping a documented way for Unraid to grant that user socket access.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TETHERD_STATE_DIR=/config \
    TETHERD_LOG_FORMAT=console

VOLUME ["/config"]

# Unraid and Compose send SIGTERM on stop; the daemon already handles it.
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD ["tetherd", "--version"]

ENTRYPOINT ["tini", "--"]
CMD ["tetherd", "run"]

LABEL org.opencontainers.image.title="Tetherd" \
      org.opencontainers.image.description="Keeps containers attached to the network of the container they route through." \
      org.opencontainers.image.source="https://github.com/Phil-Barker/tetherd" \
      org.opencontainers.image.licenses="MIT"
