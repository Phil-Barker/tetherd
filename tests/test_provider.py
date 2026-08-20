"""Watching the provider itself: the failure mode nothing else on Unraid catches.

A VPN container whose tunnel has collapsed is still running, still owns its
namespace, and still has every dependent correctly attached to it. Both Unraid and
the project Tetherd replaces only ask whether the container is running, so all of
them sit there offline indefinitely.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from tetherd.config import ProbeSettings
from tetherd.docker_api import DockerApi
from tetherd.models import ContainerInfo
from tetherd.provider import ProviderHealth, ProviderMonitor

from .conftest import make_inspect
from .fakes import FakeDocker

PROVIDER_ID = "a" * 64

HEALTHCHECK = ["CMD-SHELL", "wget -q --spider http://1.1.1.1 || exit 1"]


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def provider_container(docker: FakeDocker, **kwargs: Any) -> ContainerInfo:
    payload = docker.add(make_inspect(container_id=PROVIDER_ID, name="gluetun", **kwargs))
    return ContainerInfo.from_inspect(payload)


def set_health(docker: FakeDocker, status: str, output: str | None = None) -> ContainerInfo:
    """Change what the provider's healthcheck reports, as the daemon would."""
    payload = docker.containers[PROVIDER_ID]
    payload["State"]["Health"] = {
        "Status": status,
        "Log": [{"Output": output}] if output is not None else [],
    }
    return ContainerInfo.from_inspect(payload)


def monitor(
    docker: FakeDocker,
    clock: ManualClock,
    *,
    enabled: bool = False,
    failures_before_restart: int = 3,
    restart_provider_on_failure: bool = True,
    min_restart_interval_seconds: float = 300.0,
    targets: list[str] | None = None,
) -> ProviderMonitor:
    settings = ProbeSettings(
        enabled=enabled,
        targets=targets if targets is not None else ["1.1.1.1", "8.8.8.8"],
        failures_before_restart=failures_before_restart,
        restart_provider_on_failure=restart_provider_on_failure,
        min_restart_interval_seconds=min_restart_interval_seconds,
        settle_seconds=0.0,
    )
    return ProviderMonitor(cast(DockerApi, docker), settings, sleep=lambda _: None, monotonic=clock)


class TestDockerHealthcheck:
    """Preferred because it needs nothing from the image and cannot be second-guessed.

    Docker is already running the check on a schedule, from inside the namespace,
    with a command the image author chose. The Unraid survey found gluetun users
    adding their own through Extra Parameters too.
    """

    def test_a_healthy_provider_is_reported_healthy(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="healthy")

        status = monitor(docker, clock).check(provider)

        assert status.health is ProviderHealth.HEALTHY
        assert status.source == "docker healthcheck"

    def test_an_unhealthy_provider_is_unreachable_not_down(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """It is running and owns a valid namespace; what it cannot do is route."""
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")

        status = monitor(docker, clock).check(provider)

        assert status.health is ProviderHealth.UNREACHABLE
        assert status.can_repair_dependents

    def test_the_healthchecks_own_output_is_surfaced(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """What the check actually said is the first thing a user needs."""
        provider = provider_container(
            docker,
            healthcheck=HEALTHCHECK,
            health_status="unhealthy",
            health_output="wget: download timed out\n",
        )

        status = monitor(docker, clock).check(provider)

        assert "download timed out" in status.detail

    def test_a_starting_provider_is_not_judged_yet(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="starting")

        status = monitor(docker, clock).check(provider)

        assert status.health is ProviderHealth.STARTING
        assert status.consecutive_failures == 0

    def test_a_start_period_never_accumulates_towards_a_restart(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """Otherwise a slow-starting VPN container would be restarted for booting."""
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="starting")
        watcher = monitor(docker, clock, failures_before_restart=2)

        for _ in range(5):
            status = watcher.check(provider)

        assert status.restart_advised is False

    def test_an_explicitly_disabled_healthcheck_is_not_treated_as_one(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=["NONE"])

        status = monitor(docker, clock).check(provider)

        assert status.health is ProviderHealth.UNMONITORED

    def test_a_stopped_provider_is_down(self, docker: FakeDocker, clock: ManualClock) -> None:
        provider = provider_container(docker, running=False, healthcheck=HEALTHCHECK)

        status = monitor(docker, clock).check(provider)

        assert status.health is ProviderHealth.DOWN
        assert status.can_repair_dependents is False


class TestWithoutAHealthcheck:
    def test_no_healthcheck_and_no_probing_is_reported_not_assumed_healthy(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """Silence here is what lets a dead tunnel go unnoticed for days."""
        provider = provider_container(docker)

        status = monitor(docker, clock, enabled=False).check(provider)

        assert status.health is ProviderHealth.UNMONITORED
        assert "TETHERD_PROBE__ENABLED" in status.detail

    def test_an_unmonitored_provider_does_not_block_repairs(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker)

        assert monitor(docker, clock).check(provider).can_repair_dependents


class TestExecProbe:
    def test_a_reachable_target_is_healthy(self, docker: FakeDocker, clock: ManualClock) -> None:
        provider = provider_container(docker)
        docker.exec_results["ping"] = (0, "1 packets transmitted, 1 received")

        status = monitor(docker, clock, enabled=True).check(provider)

        assert status.health is ProviderHealth.HEALTHY

    def test_all_targets_failing_is_unreachable(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker)
        docker.exec_results["ping"] = (1, "100% packet loss")

        status = monitor(docker, clock, enabled=True).check(provider)

        assert status.health is ProviderHealth.UNREACHABLE
        assert "1.1.1.1, 8.8.8.8" in status.detail

    def test_the_probe_runs_inside_the_provider(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """Testing from outside would measure the host's connectivity, not the tunnel's."""
        provider = provider_container(docker)
        docker.exec_results["ping"] = (0, "")

        monitor(docker, clock, enabled=True).check(provider)

        assert docker.exec_commands[0] == ["ping", "-c", "1", "-W", "5", "1.1.1.1"]

    def test_a_target_with_a_port_is_probed_by_tcp_connect(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """More trustworthy than ICMP, which tunnel providers often drop outright."""
        provider = provider_container(docker)
        docker.exec_results["nc"] = (0, "")

        monitor(docker, clock, enabled=True, targets=["1.1.1.1:53"]).check(provider)

        assert docker.exec_commands[0] == ["nc", "-z", "-w", "5", "1.1.1.1", "53"]

    def test_a_missing_tool_falls_through_to_the_next(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker)
        docker.missing_binaries.add("ping")
        docker.exec_results["nc"] = (0, "")

        status = monitor(docker, clock, enabled=True).check(provider)

        assert status.health is ProviderHealth.HEALTHY
        assert [command[0] for command in docker.exec_commands][:2] == ["ping", "nc"]

    def test_no_usable_tool_is_unmonitored_never_unreachable(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """The single most dangerous confusion this module could make.

        Reading a missing binary as a dead tunnel would restart a perfectly
        working VPN container on every pass, taking every dependent down with it.
        """
        provider = provider_container(docker)
        docker.missing_binaries.update({"ping", "nc", "wget"})

        status = monitor(docker, clock, enabled=True).check(provider)

        assert status.health is ProviderHealth.UNMONITORED
        assert status.restart_advised is False
        assert "Add a healthcheck" in status.detail

    def test_probing_stops_being_attempted_once_known_impossible(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """No point re-running four execs a minute against an image that lacks them."""
        provider = provider_container(docker)
        docker.missing_binaries.update({"ping", "nc", "wget"})
        watcher = monitor(docker, clock, enabled=True)

        watcher.check(provider)
        attempts_after_first = len(docker.exec_commands)
        watcher.check(provider)

        assert len(docker.exec_commands) == attempts_after_first

    def test_an_exec_that_errors_is_not_cached_as_impossible(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """A paused or busy container is a transient condition, not a verdict."""
        provider = provider_container(docker)
        docker.fail_on["exec"] = "container is paused"
        watcher = monitor(docker, clock, enabled=True)

        first = watcher.check(provider)
        docker.fail_on.clear()
        docker.exec_results["ping"] = (0, "")
        second = watcher.check(provider)

        assert first.health is ProviderHealth.UNMONITORED
        assert second.health is ProviderHealth.HEALTHY

    def test_probing_with_no_targets_is_reported(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker)

        status = monitor(docker, clock, enabled=True, targets=[]).check(provider)

        assert status.health is ProviderHealth.UNMONITORED
        assert "no targets" in status.detail


class TestRestartDecision:
    def test_a_single_failure_does_not_restart_the_provider(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """A momentary blip must not take every dependent down with it."""
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")

        status = monitor(docker, clock, failures_before_restart=3).check(provider)

        assert status.consecutive_failures == 1
        assert status.restart_advised is False

    def test_sustained_failure_advises_a_restart(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(docker, clock, failures_before_restart=3)

        statuses = [watcher.check(provider) for _ in range(3)]

        assert [status.restart_advised for status in statuses] == [False, False, True]

    def test_recovery_resets_the_failure_count(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(docker, clock, failures_before_restart=3)
        watcher.check(provider)
        watcher.check(provider)

        watcher.check(set_health(docker, "healthy"))
        status = watcher.check(set_health(docker, "unhealthy"))

        assert status.consecutive_failures == 1
        assert status.restart_advised is False

    def test_restarting_can_be_turned_off_while_still_reporting(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """Some users want to be told and to decide themselves."""
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(
            docker, clock, failures_before_restart=1, restart_provider_on_failure=False
        )

        status = watcher.check(provider)

        assert status.health is ProviderHealth.UNREACHABLE
        assert status.restart_advised is False


class TestRestartRateLimit:
    """Nothing inside a tunnel can tell a dead tunnel from an ISP outage.

    Without a floor on restart frequency, a WAN outage becomes a loop that
    restarts the VPN container and drops every dependent on each pass.
    """

    def test_a_second_restart_is_held_off_within_the_interval(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(
            docker, clock, failures_before_restart=1, min_restart_interval_seconds=300.0
        )
        watcher.check(provider)
        watcher.restart(provider)

        clock.advance(60)
        status = watcher.check(set_health(docker, "unhealthy"))

        assert status.restart_advised is False
        assert "held off" in status.detail

    def test_a_restart_is_allowed_once_the_interval_has_passed(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(
            docker, clock, failures_before_restart=1, min_restart_interval_seconds=300.0
        )
        watcher.check(provider)
        watcher.restart(provider)

        clock.advance(301)
        status = watcher.check(set_health(docker, "unhealthy"))

        assert status.restart_advised is True

    def test_the_first_restart_is_never_delayed(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(
            docker, clock, failures_before_restart=1, min_restart_interval_seconds=300.0
        )

        assert watcher.check(provider).restart_advised is True


class TestRestarting:
    def test_restarting_the_provider_refreshes_it(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")

        succeeded, detail = monitor(docker, clock).restart(provider)

        assert succeeded
        assert "restart:gluetun" in docker.operations
        assert "stale namespace" in detail

    def test_a_failed_restart_is_reported_with_the_daemons_words(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        docker.fail_on["restart"] = "device or resource busy"

        succeeded, detail = monitor(docker, clock).restart(provider)

        assert succeeded is False
        assert "device or resource busy" in detail

    def test_restarting_clears_the_failure_count(
        self, docker: FakeDocker, clock: ManualClock
    ) -> None:
        """The next round starts from scratch rather than immediately re-restarting."""
        provider = provider_container(docker, healthcheck=HEALTHCHECK, health_status="unhealthy")
        watcher = monitor(docker, clock, failures_before_restart=2)
        watcher.check(provider)
        watcher.check(provider)

        watcher.restart(provider)

        assert watcher.check(set_health(docker, "unhealthy")).consecutive_failures == 1
