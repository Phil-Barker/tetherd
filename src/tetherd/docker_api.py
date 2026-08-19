"""A narrow, typed surface over the Docker Engine API.

Everything the rest of Tetherd knows about talking to Docker lives here, which
keeps the domain logic testable against fixtures rather than a daemon.

One deliberate choice needs explaining: containers are created by posting an
assembled ContainerCreate body rather than by calling the SDK's
``create_container`` helper. The helper takes keyword arguments and builds the
body itself, so replaying a captured configuration through it would mean mapping
every inspect field onto a keyword argument and silently losing anything not yet
mapped. The API's create body is, by contrast, almost exactly the shape that
inspect returns, so posting it directly is what makes a faithful rebuild
possible. See snapshots.py and payload.py.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Self

import docker
from docker.errors import APIError, DockerException, NotFound

from .models import CONTAINER_NETWORK_PREFIX

if TYPE_CHECKING:
    from types import TracebackType


class DockerUnavailableError(RuntimeError):
    """The daemon could not be reached at all."""


class ContainerOperationError(RuntimeError):
    """A specific container operation failed.

    Carries the daemon's own message, because those messages are what users
    search for when something goes wrong.
    """

    def __init__(self, operation: str, container: str, detail: str) -> None:
        super().__init__(f"failed to {operation} {container}: {detail}")
        self.operation = operation
        self.container = container
        self.detail = detail


class DockerApi:
    """Thin wrapper over the Engine API."""

    def __init__(self, host: str | None = None, timeout: int = 60) -> None:
        try:
            self._client = (
                docker.DockerClient(base_url=host, timeout=timeout)
                if host
                else docker.from_env(timeout=timeout)
            )
        except DockerException as exc:
            raise DockerUnavailableError(
                f"cannot reach the Docker daemon: {exc}. Is /var/run/docker.sock mounted?"
            ) from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except DockerException:
            return False

    def version(self) -> str:
        try:
            return str(self._client.version().get("Version", "unknown"))
        except DockerException:
            return "unknown"

    # -- reading ----------------------------------------------------------

    def list_network_borrowers(self) -> list[str]:
        """IDs of all containers, running or not, that borrow another's network.

        Uses the list endpoint, which reports HostConfig.NetworkMode but only a
        coarse State string, so callers still inspect the ones they care about.
        This keeps a full inspect off the hot path for hosts with many
        containers.
        """
        try:
            summaries: list[Mapping[str, Any]] = self._client.api.containers(all=True)
        except DockerException as exc:
            raise DockerUnavailableError(f"cannot list containers: {exc}") from exc

        return [
            str(summary["Id"])
            for summary in summaries
            if str((summary.get("HostConfig") or {}).get("NetworkMode", "")).startswith(
                CONTAINER_NETWORK_PREFIX
            )
        ]

    def inspect(self, ref: str) -> Mapping[str, Any] | None:
        """Full inspect payload, or None if there is no such container."""
        try:
            payload: Mapping[str, Any] = self._client.api.inspect_container(ref)
        except NotFound:
            return None
        except DockerException as exc:
            raise ContainerOperationError("inspect", ref, str(exc)) from exc
        return payload

    def exists(self, ref: str) -> bool:
        return self.inspect(ref) is not None

    # -- lifecycle --------------------------------------------------------

    def start(self, ref: str) -> None:
        self._do("start", ref, lambda: self._client.api.start(ref))

    def stop(self, ref: str, timeout: int = 30) -> None:
        self._do("stop", ref, lambda: self._client.api.stop(ref, timeout=timeout))

    def restart(self, ref: str, timeout: int = 30) -> None:
        self._do("restart", ref, lambda: self._client.api.restart(ref, timeout=timeout))

    def rename(self, ref: str, new_name: str) -> None:
        self._do("rename", ref, lambda: self._client.api.rename(ref, new_name))

    def remove(self, ref: str, force: bool = False) -> None:
        self._do("remove", ref, lambda: self._client.api.remove_container(ref, force=force))

    def create(self, name: str, body: Mapping[str, Any]) -> str:
        """Create a container from an assembled ContainerCreate body.

        Returns the new container's ID. Any warnings the daemon reports are
        folded into the exception message only on failure; on success they are
        the caller's business to log.
        """
        api = self._client.api
        url = api._url("/containers/create")
        try:
            response = api._post_json(url, data=dict(body), params={"name": name})
            api._raise_for_status(response)
            created: Mapping[str, Any] = response.json()
        except APIError as exc:
            raise ContainerOperationError("create", name, str(exc.explanation or exc)) from exc
        except DockerException as exc:
            raise ContainerOperationError("create", name, str(exc)) from exc
        return str(created["Id"])

    def exec_probe(self, ref: str, command: list[str], timeout: float) -> tuple[int, str]:
        """Run a command inside a container, returning its exit code and output.

        Used for connectivity probes, so a non-zero exit is an expected outcome
        rather than an error.
        """
        api = self._client.api
        try:
            created = api.exec_create(ref, command, stdout=True, stderr=True)
            output = api.exec_start(created["Id"], stream=False)
            exit_code = int(api.exec_inspect(created["Id"]).get("ExitCode") or 0)
        except DockerException as exc:
            raise ContainerOperationError("probe", ref, str(exc)) from exc

        text = (
            output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        )
        return exit_code, text.strip()

    # -- events -----------------------------------------------------------

    def container_events(self, actions: set[str]) -> Iterator[Mapping[str, Any]]:
        """Stream container events, filtered to the given actions.

        Yields until the caller stops consuming or the connection drops; the
        caller is responsible for reconnecting.
        """
        stream = self._client.events(
            decode=True, filters={"type": "container", "event": sorted(actions)}
        )
        try:
            for event in stream:
                if isinstance(event, dict):
                    yield event
        finally:
            stream.close()

    def _do(self, operation: str, ref: str, action: Any) -> None:
        try:
            action()
        except NotFound as exc:
            raise ContainerOperationError(operation, ref, "no such container") from exc
        except APIError as exc:
            raise ContainerOperationError(operation, ref, str(exc.explanation or exc)) from exc
        except DockerException as exc:
            raise ContainerOperationError(operation, ref, str(exc)) from exc
