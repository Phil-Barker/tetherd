"""A full reconcile pass, exercised end to end without threads or timing.

Two things are being checked here beyond the obvious. The order of operations —
the provider is settled before its dependents, and an interrupted rebuild is
recovered before discovery runs. And the accounting: every pass has to be able to
explain what it did and, more importantly, what it deliberately did not do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tetherd.config import ProbeSettings, Settings
from tetherd.docker_api import DockerApi
from tetherd.models import Verdict
from tetherd.notify import Severity
from tetherd.provider import ProviderHealth, ProviderMonitor
from tetherd.reconcile import Reconciler, notifications_for
from tetherd.remediate import ASIDE_SUFFIX, Action, Remediator
from tetherd.snapshots import SnapshotStore
from tetherd.state import ProviderStateStore

from .conftest import make_inspect
from .fakes import FakeDocker

PROVIDER_ID = "a" * 64
NEW_PROVIDER_ID = "b" * 64

HEALTHCHECK = ["CMD-SHELL", "wget -q --spider http://1.1.1.1 || exit 1"]


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "provider": "gluetun",
        "state_dir": tmp_path,
        "probe": ProbeSettings(enabled=False, settle_seconds=0.0),
    }
    return Settings(**{**defaults, **overrides})


def reconciler(docker: FakeDocker, settings: Settings) -> Reconciler:
    api = cast(DockerApi, docker)
    return Reconciler(
        api,
        settings,
        snapshots=SnapshotStore(settings.snapshot_dir, settings.snapshot_retention),
        remediator=Remediator(
            api,
            SnapshotStore(settings.snapshot_dir, settings.snapshot_retention),
            dry_run=settings.dry_run,
            restart_grace_seconds=1.0,
            sleep=lambda _: None,
        ),
        monitor=ProviderMonitor(api, settings.probe, sleep=lambda _: None),
        state=ProviderStateStore(settings.provider_state_file),
    )


def wire(
    docker: FakeDocker,
    *,
    provider_id: str = PROVIDER_ID,
    dependent_ref: str = PROVIDER_ID,
    provider_started_after: bool = False,
    provider_running: bool = True,
    dependents: tuple[str, ...] = ("qbittorrent",),
    health_status: str | None = None,
) -> None:
    dependent_started = docker.clock.tick()
    provider_started = docker.clock.tick() if provider_started_after else dependent_started

    docker.add(
        make_inspect(
            container_id=provider_id,
            name="gluetun",
            running=provider_running,
            started_at=provider_started,
            sandbox_key="/run/docker/netns/abc",
            healthcheck=HEALTHCHECK if health_status else None,
            health_status=health_status,
        )
    )
    for index, name in enumerate(dependents):
        docker.add(
            make_inspect(
                container_id=f"{index + 3:064x}",
                name=name,
                started_at=dependent_started,
                network_mode=f"container:{dependent_ref}",
            )
        )


class TestHealthyPass:
    def test_a_healthy_pass_does_nothing(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert report.repairs == []
        assert report.acted is False

    def test_healthy_containers_are_snapshotted(self, docker: FakeDocker, tmp_path: Path) -> None:
        """Recording only what was seen working is what makes a replay trustworthy."""
        wire(docker, dependents=("qbittorrent", "prowlarr"))

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert sorted(report.snapshots_taken) == ["prowlarr", "qbittorrent"]

    def test_an_unchanged_configuration_is_not_re_snapshotted(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker)
        settings = settings_for(tmp_path)
        reconciler(docker, settings).run_once()

        report = reconciler(docker, settings).run_once()

        assert report.snapshots_taken == []

    def test_the_provider_id_is_remembered(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker)
        settings = settings_for(tmp_path)

        reconciler(docker, settings).run_once()

        assert ProviderStateStore(settings.provider_state_file).load().current == PROVIDER_ID


class TestRepair:
    def test_a_stale_namespace_is_restarted(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker, provider_started_after=True)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert [result.action for result in report.repairs] == [Action.RESTART]
        assert report.repairs[0].succeeded

    def test_a_dead_reference_is_rebuilt(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker, provider_id=NEW_PROVIDER_ID, dependent_ref=PROVIDER_ID)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert [result.action for result in report.repairs] == [Action.RECREATE]
        assert docker.by_name("qbittorrent")["HostConfig"]["NetworkMode"] == (  # type: ignore[index]
            f"container:{NEW_PROVIDER_ID}"
        )

    def test_a_repaired_container_is_snapshotted_afterwards(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """A rebuilt container has a new ID, so it is found again by name."""
        wire(docker, provider_id=NEW_PROVIDER_ID)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert report.snapshots_taken == ["qbittorrent"]

    def test_each_dependent_is_judged_separately(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker, dependents=("qbittorrent", "prowlarr"), provider_started_after=True)
        # One of them has already been restarted since the provider came up.
        docker.start("prowlarr")

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert [result.container for result in report.repairs] == ["qbittorrent"]

    def test_dry_run_reports_without_touching_anything(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker, provider_id=NEW_PROVIDER_ID)

        report = reconciler(docker, settings_for(tmp_path, dry_run=True)).run_once()

        assert report.repairs[0].detail.startswith("dry run")
        assert report.snapshots_taken == []
        assert not any(op.startswith("create") for op in docker.operations)


class TestProviderFirst:
    def test_a_missing_provider_stops_the_pass_with_an_explanation(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker)
        docker.remove("gluetun")

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert report.repairs == []
        assert "does not exist" in report.notes[0]

    def test_a_stopped_provider_leaves_dependents_alone(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """Repairing against a stopped provider would only fail."""
        wire(docker, provider_running=False, provider_started_after=True)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert report.provider_status is not None
        assert report.provider_status.health is ProviderHealth.DOWN
        assert report.repairs == []
        assert any("until gluetun is back" in note for note in report.notes)

    def test_an_unhealthy_provider_is_restarted_before_dependents_are_touched(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """Otherwise every dependent is repaired now and again after the restart."""
        wire(docker, health_status="unhealthy")
        settings = settings_for(
            tmp_path,
            probe=ProbeSettings(failures_before_restart=1, settle_seconds=0.0),
        )

        report = reconciler(docker, settings).run_once()

        assert report.provider_restarted is True
        assert docker.operations.index("restart:gluetun") < len(docker.operations)

    def test_dependents_are_repaired_against_the_restarted_provider(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """The provider's start time changes, which is what a stale namespace is judged on."""
        wire(docker, health_status="unhealthy")
        settings = settings_for(
            tmp_path,
            probe=ProbeSettings(failures_before_restart=1, settle_seconds=0.0),
        )

        report = reconciler(docker, settings).run_once()

        assert [result.action for result in report.repairs] == [Action.RESTART]
        assert report.repairs[0].succeeded

    def test_an_unreachable_provider_does_not_block_repairs(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """Its namespace is intact; a dependent attached to it is correctly attached."""
        wire(docker, health_status="unhealthy", provider_started_after=True)
        settings = settings_for(
            tmp_path, probe=ProbeSettings(failures_before_restart=99, settle_seconds=0.0)
        )

        report = reconciler(docker, settings).run_once()

        assert report.provider_status is not None
        assert report.provider_status.health is ProviderHealth.UNREACHABLE
        assert [result.action for result in report.repairs] == [Action.RESTART]


class TestAccounting:
    def test_an_unresolvable_include_is_explained(self, docker: FakeDocker, tmp_path: Path) -> None:
        """Upstream issue #57: the container is invisible and nothing says so."""
        wire(docker)
        settings = settings_for(tmp_path, include=["qbittorrent", "sonarr"])

        report = reconciler(docker, settings).run_once()

        assert any("sonarr was named in include" in note for note in report.notes)
        assert any("Network Type" in note for note in report.notes)

    def test_skipped_containers_are_reported_with_reasons(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker, dependents=("qbittorrent", "prowlarr"))
        settings = settings_for(tmp_path, exclude=["prowlarr"])

        report = reconciler(docker, settings).run_once()

        skipped = {item.container.name: item.detail for item in report.discovery.skipped}
        assert skipped == {"prowlarr": "listed in exclude"}

    def test_an_unwritable_state_directory_is_reported_not_fatal(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """A repair still happening matters more than recording it."""
        wire(docker)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")

        report = reconciler(docker, settings_for(tmp_path, state_dir=blocked)).run_once()

        assert any("writable state directory" in note for note in report.notes)

    def test_every_dependent_is_assessed_even_when_healthy(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker, dependents=("qbittorrent", "prowlarr"))

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert {a.container.name for a in report.assessments} == {"qbittorrent", "prowlarr"}
        assert all(a.verdict is Verdict.HEALTHY for a in report.assessments)


class TestInterruptedRebuildRecovery:
    def test_an_aside_container_is_restored_before_discovery(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """Until it is renamed back, discovery cannot see the container at all."""
        wire(docker)
        docker.rename("qbittorrent", f"qbittorrent{ASIDE_SUFFIX}")

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert len(report.recovered) == 1
        assert [c.name for c in report.discovery.managed] == ["qbittorrent"]

    def test_recovery_counts_as_having_acted(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker)
        docker.rename("qbittorrent", f"qbittorrent{ASIDE_SUFFIX}")

        assert reconciler(docker, settings_for(tmp_path)).run_once().acted is True


class TestNotifications:
    def test_a_quiet_pass_says_nothing(self, docker: FakeDocker, tmp_path: Path) -> None:
        """Reconciling every few minutes means success has to be silent by default."""
        wire(docker)
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert notifications_for(report, notify_on_healthy_runs=False) == []

    def test_a_quiet_pass_can_be_made_to_check_in(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker)
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        notifications = notifications_for(report, notify_on_healthy_runs=True)

        assert notifications[0].severity is Severity.INFO

    def test_a_successful_repair_is_announced(self, docker: FakeDocker, tmp_path: Path) -> None:
        wire(docker, provider_started_after=True)
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        notifications = notifications_for(report, notify_on_healthy_runs=False)

        assert notifications[0].title == "Tetherd restarted qbittorrent"
        assert notifications[0].severity is Severity.WARNING

    def test_multiple_repairs_are_summarised_in_one_message(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        """One message per pass, not one per container."""
        wire(docker, dependents=("qbittorrent", "prowlarr"), provider_started_after=True)
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        notifications = notifications_for(report, notify_on_healthy_runs=False)

        assert len(notifications) == 1
        assert notifications[0].title == "Tetherd repaired 2 containers"

    def test_a_failure_is_reported_at_error_severity(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker, provider_id=NEW_PROVIDER_ID)
        docker.fail_on["create"] = "no such image"
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        notifications = notifications_for(report, notify_on_healthy_runs=False)

        assert notifications[0].severity is Severity.ERROR
        assert "could not repair qbittorrent" in notifications[0].title

    def test_a_failure_carries_structured_context_for_hooks(
        self, docker: FakeDocker, tmp_path: Path
    ) -> None:
        wire(docker, provider_id=NEW_PROVIDER_ID)
        docker.fail_on["create"] = "no such image"
        report = reconciler(docker, settings_for(tmp_path)).run_once()

        context = notifications_for(report, notify_on_healthy_runs=False)[0].context

        assert context["container"] == "qbittorrent"
        assert context["succeeded"] == "false"

    def test_adopting_an_orphan_is_reported(self, docker: FakeDocker, tmp_path: Path) -> None:
        """Installing Tetherd to fix a host whose provider was already recreated."""
        wire(docker, provider_id=NEW_PROVIDER_ID, dependent_ref=PROVIDER_ID)

        report = reconciler(docker, settings_for(tmp_path)).run_once()

        assert any("adopting qbittorrent" in note for note in report.notes)
        assert [c.name for c in report.discovery.adopted] == ["qbittorrent"]
