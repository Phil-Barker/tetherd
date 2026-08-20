"""An in-memory stand-in for the Docker daemon.

Faithful enough for the parts Tetherd depends on: container identity, name
uniqueness, run state, start-time ordering, and the create round trip. Modelling
the create body as an inspect payload is what makes the rebuild tests meaningful,
because it is the same transformation the real daemon performs and the same one
that must preserve Unraid's labels.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from tetherd.docker_api import ContainerOperationError

_ZERO_TIME = "0001-01-01T00:00:00Z"


class FakeClock:
    """Issues strictly increasing Docker-style timestamps.

    Start-time ordering is the only signal that reveals a stale namespace, so a
    fake that cannot advance time cannot exercise the interesting cases.
    """

    def __init__(self) -> None:
        self._now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)

    def tick(self) -> str:
        self._now += timedelta(seconds=1)
        return self._now.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


class FakeDocker:
    """A minimal, mutable container registry with the DockerApi surface."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.containers: dict[str, dict[str, Any]] = {}
        self.operations: list[str] = []
        #: Operation name mapped to the failure detail it should raise.
        self.fail_on: dict[str, str] = {}
        #: Names whose restart should not refresh the start time, modelling a
        #: container that restarts without recovering.
        self.restart_is_ineffective: set[str] = set()
        #: Binary name mapped to the (exit code, output) it should report.
        self.exec_results: dict[str, tuple[int, str]] = {}
        #: Binaries absent from the image, which the daemon reports distinctly
        #: from a command that ran and failed.
        self.missing_binaries: set[str] = set()
        self.exec_commands: list[list[str]] = []
        self._ids = (f"{index:064x}" for index in itertools.count(1))

    # -- test setup --------------------------------------------------------

    def add(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        stored = dict(payload)
        self.containers[str(stored["Id"])] = stored
        return stored

    def name_of(self, container_id: str) -> str:
        return str(self.containers[container_id].get("Name", "")).lstrip("/")

    def by_name(self, name: str) -> dict[str, Any] | None:
        resolved = self._resolve(name)
        return self.containers.get(resolved) if resolved else None

    def names(self) -> list[str]:
        return sorted(self.name_of(container_id) for container_id in self.containers)

    # -- DockerApi surface -------------------------------------------------

    def inspect(self, ref: str) -> Mapping[str, Any] | None:
        resolved = self._resolve(ref)
        return self.containers.get(resolved) if resolved else None

    def exists(self, ref: str) -> bool:
        return self._resolve(ref) is not None

    def start(self, ref: str) -> None:
        container = self._require("start", ref)
        container["State"] = self._started_state(container)

    def stop(self, ref: str, timeout: int = 30) -> None:
        del timeout
        container = self._require("stop", ref)
        container["State"] = {**container.get("State", {}), "Running": False}

    def restart(self, ref: str, timeout: int = 30) -> None:
        del timeout
        container = self._require("restart", ref)
        name = str(container.get("Name", "")).lstrip("/")
        if name in self.restart_is_ineffective:
            container["State"] = {**container.get("State", {}), "Running": True}
            return
        container["State"] = self._started_state(container)

    def _started_state(self, container: Mapping[str, Any]) -> dict[str, Any]:
        """Fresh run state, with health reset the way the daemon resets it.

        A restarted container re-enters its healthcheck start period rather than
        carrying its previous verdict forward.
        """
        state: dict[str, Any] = {"Running": True, "StartedAt": self.clock.tick()}
        if "Health" in (container.get("State") or {}):
            state["Health"] = {"Status": "starting", "Log": []}
        return state

    def rename(self, ref: str, new_name: str) -> None:
        container = self._require("rename", ref)
        if self.exists(new_name):
            raise ContainerOperationError("rename", ref, f"name {new_name} is already in use")
        container["Name"] = f"/{new_name}"

    def remove(self, ref: str, force: bool = False) -> None:
        del force
        self._require("remove", ref)
        resolved = self._resolve(ref)
        assert resolved is not None
        del self.containers[resolved]

    def create(self, name: str, body: Mapping[str, Any]) -> str:
        self._maybe_fail("create", name)
        if self.exists(name):
            raise ContainerOperationError("create", name, f"name {name} is already in use")

        config = {key: value for key, value in body.items() if key != "HostConfig"}
        container_id = next(self._ids)
        self.containers[container_id] = {
            "Id": container_id,
            "Name": f"/{name}",
            "State": {"Running": False, "StartedAt": _ZERO_TIME},
            "Config": config,
            "HostConfig": dict(body.get("HostConfig") or {}),
            "NetworkSettings": {"SandboxKey": "", "Networks": {}},
        }
        self.operations.append(f"create:{name}")
        return container_id

    def exec_probe(self, ref: str, command: list[str], timeout: float) -> tuple[int, str]:
        del timeout
        self._maybe_fail("exec", ref)
        self._require_exists("probe", ref)
        self.exec_commands.append(list(command))

        binary = command[0]
        if binary in self.missing_binaries:
            raise ContainerOperationError(
                "probe",
                ref,
                f'OCI runtime exec failed: exec: "{binary}": executable file not found in $PATH',
            )
        return self.exec_results.get(binary, (0, ""))

    def find_by_name_suffix(self, suffix: str) -> list[tuple[str, str]]:
        return [
            (container_id, self.name_of(container_id))
            for container_id in sorted(self.containers)
            if self.name_of(container_id).endswith(suffix)
        ]

    def find_by_name_prefix(self, prefix: str) -> list[str]:
        return [
            container_id
            for container_id in sorted(self.containers)
            if self.name_of(container_id).startswith(prefix)
        ]

    # -- internals ---------------------------------------------------------

    def _resolve(self, ref: str) -> str | None:
        if ref in self.containers:
            return ref
        for container_id, container in self.containers.items():
            if str(container.get("Name", "")).lstrip("/") == ref:
                return container_id
        return None

    def _require(self, operation: str, ref: str) -> dict[str, Any]:
        self._maybe_fail(operation, ref)
        container = self._require_exists(operation, ref)
        self.operations.append(f"{operation}:{str(container['Name']).lstrip('/')}")
        return container

    def _require_exists(self, operation: str, ref: str) -> dict[str, Any]:
        resolved = self._resolve(ref)
        if resolved is None:
            raise ContainerOperationError(operation, ref, "no such container")
        return self.containers[resolved]

    def _maybe_fail(self, operation: str, ref: str) -> None:
        detail = self.fail_on.get(operation)
        if detail is not None:
            raise ContainerOperationError(operation, ref, detail)
