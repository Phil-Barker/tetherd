"""Prove a rebuild reproduces a container exactly, against a real daemon.

This is the claim the whole project rests on. Rebuild-DNDC rebuilt containers by
re-parsing an Unraid XML template, which is how it came to pick the wrong
template (#77, #75) and to silently drop volumes, ports and environment
variables when Unraid changed that template's format (#59). Tetherd replays the
container's own recorded configuration instead, so there is nothing to
misinterpret — but only if the replay really is lossless, which is what this
verifies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tetherd.docker_api import ContainerOperationError, DockerApi
from tetherd.payload import build_create_request

from .conftest import SLEEP_FOREVER

pytestmark = pytest.mark.integration

ContainerFactory = Callable[..., str]

# Fields spanning the things an Unraid template can express. If a rebuild
# preserves all of these, it preserves a real user's container.
CHECKED_FIELDS = {
    "Config": [
        "Env",
        "Cmd",
        "Entrypoint",
        "Labels",
        "WorkingDir",
        "User",
        "Tty",
        "StopSignal",
        "Healthcheck",
        "Volumes",
    ],
    "HostConfig": [
        "Binds",
        "Mounts",
        "RestartPolicy",
        "CapAdd",
        "CapDrop",
        "Privileged",
        "Memory",
        "MemoryReservation",
        "NanoCpus",
        "CpusetCpus",
        "CpuShares",
        "Devices",
        "Sysctls",
        "LogConfig",
        "Ulimits",
        "GroupAdd",
        "SecurityOpt",
        "ReadonlyRootfs",
        "ShmSize",
        "AutoRemove",
        "OomScoreAdj",
    ],
}


def richly_configured(provider_id: str) -> dict[str, Any]:
    return {
        "Cmd": SLEEP_FOREVER,
        # A quoted value with spaces: the predecessor built a shell command
        # string and ran it through eval, which mangled exactly this.
        "Env": ["TZ=Europe/London", "PUID=99", "PGID=100", 'MOTD=hello "world" now'],
        "Labels": {
            "net.unraid.docker.managed": "dockerman",
            "net.unraid.docker.template": "/boot/config/plugins/dockerMan/my-app.xml",
            "tetherd.enable": "true",
        },
        "WorkingDir": "/tmp",
        "User": "0:0",
        "StopSignal": "SIGTERM",
        "Healthcheck": {"Test": ["CMD-SHELL", "true"], "Interval": 30_000_000_000},
        "HostConfig": {
            "NetworkMode": f"container:{provider_id}",
            "Binds": ["/tmp:/hostmp:ro"],
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "CapAdd": ["NET_ADMIN"],
            "CapDrop": ["MKNOD"],
            "Memory": 536_870_912,
            "MemoryReservation": 268_435_456,
            "NanoCpus": 1_500_000_000,
            "CpusetCpus": "0",
            "Sysctls": {"net.ipv4.ip_forward": "1"},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m"}},
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
            "GroupAdd": ["100"],
            "ShmSize": 67_108_864,
            "OomScoreAdj": 100,
        },
    }


@pytest.fixture
def provider(container_factory: ContainerFactory) -> str:
    return container_factory("provider", {"Cmd": SLEEP_FOREVER})


def test_a_rebuild_preserves_every_configured_field(
    docker_api: DockerApi, container_factory: ContainerFactory, provider: str
) -> None:
    name = container_factory("rich", richly_configured(provider))
    before = docker_api.inspect(name)
    assert before is not None

    request = build_create_request(before, provider_id=provider)
    docker_api.remove(name, force=True)
    rebuilt = docker_api.create(request.name, request.body)
    docker_api.start(rebuilt)

    after = docker_api.inspect(rebuilt)
    assert after is not None

    drift = {
        f"{section}.{field}": (
            (before[section] or {}).get(field),
            (after[section] or {}).get(field),
        )
        for section, fields in CHECKED_FIELDS.items()
        for field in fields
        if (before[section] or {}).get(field) != (after[section] or {}).get(field)
    }
    assert drift == {}, f"configuration drifted across rebuild: {drift}"

    assert rebuilt != name, "a rebuild produces a new container"
    assert after["Name"] == before["Name"], "under the original name"
    assert after["HostConfig"]["NetworkMode"] == f"container:{provider}"
    assert after["State"]["Running"] is True


def test_a_rebuild_repoints_a_dependent_at_a_recreated_provider(
    docker_api: DockerApi, container_factory: ContainerFactory, provider: str
) -> None:
    """The failure mode a restart cannot fix."""
    name = container_factory(
        "dependent", {"Cmd": SLEEP_FOREVER, "HostConfig": {"NetworkMode": f"container:{provider}"}}
    )
    captured = docker_api.inspect(name)
    assert captured is not None

    docker_api.remove(provider, force=True)
    replacement = container_factory("provider-2", {"Cmd": SLEEP_FOREVER})

    with pytest.raises(ContainerOperationError):
        docker_api.restart(name)

    request = build_create_request(captured, provider_id=replacement)
    docker_api.remove(name, force=True)
    rebuilt = docker_api.create(request.name, request.body)
    docker_api.start(rebuilt)

    after = docker_api.inspect(rebuilt)
    assert after is not None
    assert after["HostConfig"]["NetworkMode"] == f"container:{replacement}"
    assert after["State"]["Running"] is True


def test_a_template_carrying_port_mappings_still_rebuilds(
    docker_api: DockerApi, container_factory: ContainerFactory, provider: str
) -> None:
    """Upstream issues #80, #69 and #65, reproduced and then fixed.

    A container is created on a bridge network with published ports, then
    switched to borrow the provider's network — which is precisely the state an
    Unraid user ends up in after setting Network Type to None and adding
    --network container:... to Extra Parameters without removing the port
    mappings from the template. Replaying that configuration unsanitised is
    rejected by the daemon, and upstream's teardown-first ordering means the
    container is already gone by then.
    """
    name = container_factory(
        "ported",
        {
            "Cmd": SLEEP_FOREVER,
            "ExposedPorts": {"8080/tcp": {}},
            "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "18080"}]}},
        },
    )
    captured = dict(docker_api.inspect(name) or {})
    captured["HostConfig"] = {
        **captured["HostConfig"],
        "NetworkMode": f"container:{provider}",
    }

    # Unsanitised, this is the error users are left stranded by. The daemon
    # reports whichever conflict it notices first, so match the family rather
    # than one message.
    with pytest.raises(ContainerOperationError, match="conflicting options"):
        docker_api.create(
            f"{request_name(captured)}-unsanitised",
            captured["Config"] | {"HostConfig": captured["HostConfig"]},
        )

    request = build_create_request(captured, provider_id=provider)
    assert {s.path for s in request.stripped} >= {
        "HostConfig.PortBindings",
        "Config.ExposedPorts",
    }

    docker_api.remove(name, force=True)
    rebuilt = docker_api.create(request.name, request.body)
    docker_api.start(rebuilt)

    after = docker_api.inspect(rebuilt)
    assert after is not None
    assert after["State"]["Running"] is True
    assert not after["HostConfig"]["PortBindings"]


def request_name(payload: dict[str, Any]) -> str:
    return str(payload.get("Name", "")).lstrip("/")
