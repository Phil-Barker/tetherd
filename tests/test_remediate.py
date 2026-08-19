"""Tiered remediation: restart when that is enough, rebuild safely when it is not.

The property under test throughout is that a failed repair leaves the host as it
was found. Upstream's remediation removes a container before knowing whether a
replacement can be created, which is how issues #80, #69 and #65 end with a
container that simply no longer exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tetherd.assess import assess
from tetherd.docker_api import DockerApi
from tetherd.models import ContainerInfo, Verdict
from tetherd.remediate import ASIDE_SUFFIX, Action, Remediator
from tetherd.snapshots import SnapshotStore
from tetherd.unraid import LABEL_ICON, LABEL_MANAGED, LABEL_WEBUI

from .conftest import make_inspect
from .fakes import FakeDocker

PROVIDER_OLD = "a" * 64
PROVIDER_NEW = "c" * 64
DEPENDENT = "d" * 64

UNRAID_LABELS = {
    LABEL_MANAGED: "dockerman",
    LABEL_ICON: "https://example.invalid/qbittorrent.png",
    LABEL_WEBUI: "http://[IP]:[PORT:8080]",
}


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "snapshots")


def build(
    docker: FakeDocker, store: SnapshotStore, *, dry_run: bool = False, grace: float = 1.0
) -> Remediator:
    return Remediator(
        cast(DockerApi, docker),
        store,
        dry_run=dry_run,
        restart_grace_seconds=grace,
        sleep=lambda _: None,
    )


def info(payload: Any) -> ContainerInfo:
    return ContainerInfo.from_inspect(payload)


def scenario(
    docker: FakeDocker,
    *,
    provider_id: str = PROVIDER_OLD,
    dependent_ref: str = PROVIDER_OLD,
    dependent_running: bool = True,
    provider_started_after: bool = False,
    labels: dict[str, str] | None = None,
    extra_host_config: dict[str, Any] | None = None,
) -> tuple[ContainerInfo, ContainerInfo]:
    """A provider and one dependent, wired for a specific failure mode."""
    dependent_started = docker.clock.tick()
    provider_started = docker.clock.tick() if provider_started_after else dependent_started

    provider = docker.add(
        make_inspect(
            container_id=provider_id,
            name="gluetun",
            started_at=provider_started,
            sandbox_key="/run/docker/netns/abc123",
        )
    )
    dependent = docker.add(
        make_inspect(
            container_id=DEPENDENT,
            name="qbittorrent",
            running=dependent_running,
            started_at=dependent_started,
            network_mode=f"container:{dependent_ref}",
            labels=labels if labels is not None else dict(UNRAID_LABELS),
            extra_host_config=extra_host_config,
        )
    )
    return info(dependent), info(provider)


class TestNothingToDo:
    def test_a_healthy_container_is_left_alone(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker)

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.action is Action.NONE
        assert result.succeeded
        assert docker.operations == []

    def test_a_down_provider_is_waited_for_not_worked_around(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """Repairing a dependent while its provider is down would just fail."""
        dependent, provider = scenario(docker)
        docker.by_name("gluetun")["State"]["Running"] = False  # type: ignore[index]
        provider = info(docker.by_name("gluetun"))

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.verdict is Verdict.PROVIDER_DOWN
        assert result.action is Action.NONE
        assert docker.operations == []


class TestRestartTier:
    """A stale namespace needs a restart, not a rebuild.

    Upstream recreates the container in this case too, discarding and rebuilding
    something whose configuration was never at fault.
    """

    def test_a_stale_namespace_is_repaired_by_restarting(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_started_after=True)

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.action is Action.RESTART
        assert result.succeeded
        assert "restarted" in result.detail

    def test_restarting_does_not_replace_the_container(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_started_after=True)

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]
        assert not any(operation.startswith("create") for operation in docker.operations)

    def test_a_restart_that_does_not_recover_escalates_to_a_rebuild(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """Whatever the timestamps implied, the recorded network mode is unusable."""
        dependent, provider = scenario(docker, provider_started_after=True)
        docker.restart_is_ineffective.add("qbittorrent")

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.escalated
        assert result.action is Action.RECREATE
        assert "escalated to a rebuild" in result.detail

    def test_a_failing_restart_escalates_rather_than_giving_up(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_started_after=True)
        docker.fail_on["restart"] = "container is in a bad state"

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.escalated
        assert "container is in a bad state" in result.detail


class TestRebuildTier:
    def test_a_dead_provider_reference_is_rebuilt_against_the_current_provider(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW, dependent_ref=PROVIDER_OLD)

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.action is Action.RECREATE
        assert result.succeeded, result.detail
        rebuilt = docker.by_name("qbittorrent")
        assert rebuilt is not None
        assert rebuilt["HostConfig"]["NetworkMode"] == f"container:{PROVIDER_NEW}"

    def test_the_rebuilt_container_is_a_new_container(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.by_name("qbittorrent")["Id"] != DEPENDENT  # type: ignore[index]

    def test_unraid_labels_survive_a_rebuild(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """The defect behind upstream issue #77's symptom.

        Rebuilding from a template cannot preserve these, because they are not in
        the template. A container that loses them shows as "3rd Party" in the
        Docker tab with no icon and no WebUI link.
        """
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)

        build(docker, store).remediate(assess(dependent, provider), provider)

        rebuilt = docker.by_name("qbittorrent")
        assert rebuilt is not None
        assert rebuilt["Config"]["Labels"] == UNRAID_LABELS

    def test_a_running_container_is_running_again_afterwards(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW, dependent_running=True)

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.by_name("qbittorrent")["State"]["Running"] is True  # type: ignore[index]

    def test_a_stopped_container_is_rebuilt_but_left_stopped(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """A container the user deliberately stopped must not be started by a repair."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW, dependent_running=False)

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.succeeded, result.detail
        assert docker.by_name("qbittorrent")["State"]["Running"] is False  # type: ignore[index]

    def test_port_bindings_are_stripped_and_reported(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """The failure in upstream issues #80, #69 and #65, reported rather than fatal."""
        dependent, provider = scenario(
            docker,
            provider_id=PROVIDER_NEW,
            extra_host_config={"PortBindings": {"8080/tcp": [{"HostPort": "8080"}]}},
        )

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.succeeded, result.detail
        assert any("PortBindings" in str(item) for item in result.stripped)

    def test_the_original_is_removed_only_after_success(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.names() == ["gluetun", "qbittorrent"]
        assert DEPENDENT not in docker.containers


class TestRollback:
    """A failed rebuild must leave the host exactly as it was found."""

    def test_a_container_that_cannot_be_created_is_put_back(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.fail_on["create"] = "no such image: lscr.io/linuxserver/qbittorrent"

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert not result.succeeded
        assert result.rolled_back
        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]

    def test_a_restored_container_is_running_again(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW, dependent_running=True)
        docker.fail_on["create"] = "no such image"

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.by_name("qbittorrent")["State"]["Running"] is True  # type: ignore[index]

    def test_the_failure_reason_reaches_the_user(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """The daemon's own words are what users search for."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.fail_on["create"] = "no such image: lscr.io/linuxserver/qbittorrent"

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert "no such image" in result.detail
        assert "put back as it was found" in result.detail

    def test_no_aside_container_is_left_behind_after_a_rollback(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.fail_on["create"] = "no such image"

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.names() == ["gluetun", "qbittorrent"]

    def test_a_replacement_that_cannot_be_started_is_discarded(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """Creation succeeding is not success; the container has to come up."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.fail_on["start"] = "cannot join network namespace"

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert not result.succeeded
        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]


class TestMissingConfiguration:
    def test_a_container_that_no_longer_exists_is_rebuilt_from_its_snapshot(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """The case Unraid cannot handle: no container left to read a config from."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        store.capture(dict(docker.by_name("qbittorrent") or {}))
        docker.remove("qbittorrent")

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.succeeded, result.detail
        assert "recorded" in result.detail
        rebuilt = docker.by_name("qbittorrent")
        assert rebuilt is not None
        assert rebuilt["Config"]["Labels"] == UNRAID_LABELS

    def test_an_unknown_container_fails_with_an_explanation(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.remove("qbittorrent")

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert not result.succeeded
        assert "no configuration is available" in result.detail

    def test_a_live_configuration_is_preferred_over_a_snapshot(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """A snapshot can predate a change the user made through the Unraid UI."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        store.capture(dict(docker.by_name("qbittorrent") or {}))
        docker.by_name("qbittorrent")["Config"]["Env"] = ["PUID=99"]  # type: ignore[index]

        result = build(docker, store).remediate(assess(dependent, provider), provider)

        assert result.succeeded, result.detail
        assert docker.by_name("qbittorrent")["Config"]["Env"] == ["PUID=99"]  # type: ignore[index]


class TestDryRun:
    def test_a_planned_restart_changes_nothing(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_started_after=True)

        result = build(docker, store, dry_run=True).remediate(assess(dependent, provider), provider)

        assert result.action is Action.RESTART
        assert result.detail.startswith("dry run: would restart")
        assert docker.operations == []

    def test_a_planned_rebuild_changes_nothing(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)

        result = build(docker, store, dry_run=True).remediate(assess(dependent, provider), provider)

        assert result.action is Action.RECREATE
        assert "would rebuild" in result.detail
        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]

    def test_a_planned_rebuild_still_reports_what_would_be_stripped(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """So a user can fix a template before letting Tetherd act."""
        dependent, provider = scenario(
            docker,
            provider_id=PROVIDER_NEW,
            extra_host_config={"PortBindings": {"8080/tcp": [{"HostPort": "8080"}]}},
        )

        result = build(docker, store, dry_run=True).remediate(assess(dependent, provider), provider)

        assert any("PortBindings" in str(item) for item in result.stripped)


class TestCrashRecovery:
    """A rebuild interrupted by a reboot or an OOM kill must be recoverable.

    The rename-aside is the checkpoint: a container can only be missing while its
    aside copy exists if Tetherd was killed between the two.
    """

    def test_a_container_left_renamed_aside_is_restored(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        scenario(docker)
        docker.rename("qbittorrent", f"qbittorrent{ASIDE_SUFFIX}")

        results = build(docker, store).recover_interrupted()

        assert len(results) == 1
        assert results[0].succeeded
        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]

    def test_a_superseded_aside_copy_is_swept_up(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """If the original name is taken, the rebuild finished and this is litter."""
        scenario(docker)
        docker.add(make_inspect(container_id="e" * 64, name=f"qbittorrent{ASIDE_SUFFIX}"))

        results = build(docker, store).recover_interrupted()

        assert results[0].succeeded
        assert "removed" in results[0].detail
        assert docker.names() == ["gluetun", "qbittorrent"]

    def test_nothing_to_recover_is_silent(self, docker: FakeDocker, store: SnapshotStore) -> None:
        scenario(docker)

        assert build(docker, store).recover_interrupted() == []

    def test_recovery_reports_without_acting_in_a_dry_run(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        scenario(docker)
        docker.rename("qbittorrent", f"qbittorrent{ASIDE_SUFFIX}")

        results = build(docker, store, dry_run=True).recover_interrupted()

        assert "would restore" in results[0].detail
        assert docker.by_name("qbittorrent") is None

    def test_an_occupied_aside_name_does_not_overwrite_the_earlier_copy(
        self, docker: FakeDocker, store: SnapshotStore
    ) -> None:
        """That copy may be the only surviving record of a container's configuration."""
        dependent, provider = scenario(docker, provider_id=PROVIDER_NEW)
        docker.add(make_inspect(container_id="e" * 64, name=f"qbittorrent{ASIDE_SUFFIX}"))
        docker.fail_on["create"] = "no such image"

        build(docker, store).remediate(assess(dependent, provider), provider)

        assert docker.by_name(f"qbittorrent{ASIDE_SUFFIX}")["Id"] == "e" * 64  # type: ignore[index]
        assert docker.by_name("qbittorrent")["Id"] == DEPENDENT  # type: ignore[index]
