"""Fixtures for tests that need a real Docker daemon.

These are the tests that matter most: the whole point of Tetherd is behaving
correctly against a daemon whose exact semantics are the thing that broke its
predecessor.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress

import pytest

from tetherd.docker_api import ContainerOperationError, DockerApi, DockerUnavailableError

TEST_IMAGE = "alpine:3.22"
SLEEP_FOREVER = ["sh", "-c", "while true; do sleep 30; done"]
NAME_PREFIX = "tetherd-it-"


@pytest.fixture(scope="session")
def docker_api() -> Iterator[DockerApi]:
    try:
        api = DockerApi()
    except DockerUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no Docker daemon available: {exc}")

    if not api.ping():  # pragma: no cover - environment dependent
        api.close()
        pytest.skip("Docker daemon did not respond to a ping")

    with api:
        yield api


@pytest.fixture
def container_factory(docker_api: DockerApi) -> Iterator[object]:
    """Create test containers and guarantee they are cleaned up."""
    created: list[str] = []

    def create(name: str, body: dict[str, object], *, start: bool = True) -> str:
        full_name = f"{NAME_PREFIX}{name}"
        if docker_api.exists(full_name):
            docker_api.remove(full_name, force=True)
        container_id = docker_api.create(full_name, {"Image": TEST_IMAGE, **body})
        created.append(container_id)
        if start:
            docker_api.start(container_id)
        return container_id

    yield create

    for container_id in reversed(created):
        # Cleanup must never fail a test run, and a container the test removed
        # itself is the normal case here.
        with suppress(ContainerOperationError):
            docker_api.remove(container_id, force=True)
