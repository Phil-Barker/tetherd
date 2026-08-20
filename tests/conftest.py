"""Fixtures shaped after real inspect payloads captured by scripts/spike-netns.sh."""

from __future__ import annotations

from typing import Any

import pytest

PROVIDER_ID = "255f6fa34e90d67271a12bd054f3ff7467f965ef0a5e7fcb3f32727e84d6bb58"
DEPENDENT_ID = "078ede30f6633500cb9567fd01d7bf09e0db36856f060af8736744a0fc65ef10"


def make_inspect(
    *,
    container_id: str,
    name: str,
    running: bool = True,
    started_at: str | None = "2026-08-19T09:53:09.868937429Z",
    network_mode: str = "bridge",
    sandbox_key: str = "",
    labels: dict[str, str] | None = None,
    image: str = "alpine:3.22",
    extra_host_config: dict[str, Any] | None = None,
    healthcheck: list[str] | None = None,
    health_status: str | None = None,
    health_output: str | None = None,
) -> dict[str, Any]:
    """Build a minimal but structurally faithful inspect payload."""
    state: dict[str, Any] = {
        "Running": running,
        "StartedAt": started_at or "0001-01-01T00:00:00Z",
    }
    if health_status is not None:
        state["Health"] = {
            "Status": health_status,
            "Log": [{"Output": health_output}] if health_output is not None else [],
        }

    config: dict[str, Any] = {
        "Image": image,
        "Labels": labels or {},
        "Cmd": ["sh", "-c", "while true; do sleep 30; done"],
        "Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"],
    }
    if healthcheck is not None:
        config["Healthcheck"] = {"Test": healthcheck}

    return {
        "Id": container_id,
        "Name": f"/{name}",
        "State": state,
        "Config": config,
        "HostConfig": {
            "NetworkMode": network_mode,
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            **(extra_host_config or {}),
        },
        # A dependent has no network metadata of its own: SandboxKey is empty,
        # Networks is empty, and EndpointID is absent entirely.
        "NetworkSettings": {"SandboxKey": sandbox_key, "Networks": {}},
    }


@pytest.fixture
def provider_payload() -> dict[str, Any]:
    return make_inspect(
        container_id=PROVIDER_ID,
        name="gluetun",
        started_at="2026-08-19T09:53:21.336885338Z",
        network_mode="bridge",
        sandbox_key="/run/docker/netns/3d4a5763d155",
    )


@pytest.fixture
def dependent_payload() -> dict[str, Any]:
    return make_inspect(
        container_id=DEPENDENT_ID,
        name="qbittorrent",
        started_at="2026-08-19T09:53:09.868937429Z",
        network_mode=f"container:{PROVIDER_ID}",
    )
