"""The event loop: when to reconcile, and that only one process may do it."""

from __future__ import annotations

import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from tetherd.config import Settings
from tetherd.daemon import (
    AlreadyRunningError,
    Daemon,
    InstanceLock,
    event_is_interesting,
)
from tetherd.discovery import Discovery
from tetherd.docker_api import DockerApi
from tetherd.models import Verdict
from tetherd.notify import Notifier, Severity
from tetherd.reconcile import ReconcileReport
from tetherd.remediate import Action, RemediationResult

from .conftest import make_inspect
from .fakes import FakeDocker

PROVIDER_ID = "a" * 64


class ManualClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingReconciler:
    def __init__(self, report: ReconcileReport | None = None) -> None:
        self.calls = 0
        self.report = report or ReconcileReport(discovery=Discovery(provider=None))

    def run_once(self) -> ReconcileReport:
        self.calls += 1
        return self.report


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, notification: Any) -> None:
        self.sent.append(notification)


def docker_event(action: str, name: str, actor_id: str = "") -> dict[str, Any]:
    return {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": actor_id or "f" * 64, "Attributes": {"name": name}},
    }


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "provider": "gluetun",
        "state_dir": tmp_path,
        "event_debounce_seconds": 5.0,
        "reconcile_interval_seconds": 300.0,
    }
    return Settings(**{**defaults, **overrides})


def daemon_for(
    tmp_path: Path,
    *,
    report: ReconcileReport | None = None,
    sink: RecordingSink | None = None,
    clock: ManualClock | None = None,
    **overrides: Any,
) -> tuple[Daemon, RecordingReconciler, ManualClock]:
    reconciler = RecordingReconciler(report)
    clock = clock or ManualClock()
    built = Daemon(
        settings_for(tmp_path, **overrides),
        cast(DockerApi, FakeDocker()),
        reconciler,  # type: ignore[arg-type]
        Notifier([sink] if sink else []),
        monotonic=clock,
        events=lambda: iter(()),
    )
    return built, reconciler, clock


def provider_report() -> ReconcileReport:
    provider = make_inspect(container_id=PROVIDER_ID, name="gluetun")
    dependent = make_inspect(
        container_id="b" * 64, name="qbittorrent", network_mode=f"container:{PROVIDER_ID}"
    )
    from tetherd.models import ContainerInfo

    return ReconcileReport(
        discovery=Discovery(
            provider=ContainerInfo.from_inspect(provider),
            managed=[ContainerInfo.from_inspect(dependent)],
        )
    )


class TestEventFilter:
    def test_a_provider_event_is_always_interesting(self) -> None:
        assert event_is_interesting(
            docker_event("die", "gluetun"),
            provider="gluetun",
            provider_ids=set(),
            managed_names=set(),
        )

    def test_provider_health_is_interesting(self) -> None:
        """Docker emits the verdict as part of the action, not as a separate field."""
        assert event_is_interesting(
            docker_event("health_status: unhealthy", "gluetun", PROVIDER_ID),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names=set(),
        )

    def test_another_containers_health_is_not(self) -> None:
        """Otherwise every *arr healthcheck would wake Tetherd for no reason."""
        assert not event_is_interesting(
            docker_event("health_status: unhealthy", "plex"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names={"qbittorrent"},
        )

    def test_a_managed_dependent_dying_is_interesting(self) -> None:
        assert event_is_interesting(
            docker_event("die", "qbittorrent"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names={"qbittorrent"},
        )

    def test_create_of_an_unknown_container_is_interesting(self) -> None:
        """How a newly added dependent is noticed before the next periodic pass."""
        assert event_is_interesting(
            docker_event("create", "sonarr"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names={"qbittorrent"},
        )

    def test_an_unrelated_container_stopping_is_not(self) -> None:
        assert not event_is_interesting(
            docker_event("die", "plex"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names={"qbittorrent"},
        )

    def test_a_truncated_provider_id_still_matches(self) -> None:
        assert event_is_interesting(
            docker_event("die", "other", actor_id=PROVIDER_ID[:12]),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names=set(),
        )

    def test_a_short_id_does_not_match_by_substring(self) -> None:
        """The same discipline as discovery: 'abc' must not match a 64-char ID."""
        assert not event_is_interesting(
            docker_event("die", "other", actor_id="aaa"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names=set(),
        )

    def test_an_unwatched_action_is_ignored(self) -> None:
        assert not event_is_interesting(
            docker_event("exec_create", "gluetun"),
            provider="gluetun",
            provider_ids={PROVIDER_ID},
            managed_names=set(),
        )


class TestDebounce:
    def test_a_provider_event_does_not_reconcile_until_the_quiet_period(
        self, tmp_path: Path
    ) -> None:
        daemon, reconciler, clock = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")
        assert reconciler.calls == 1

        assert daemon.consider(docker_event("die", "gluetun", PROVIDER_ID))
        assert daemon._due(clock.now) is False

        clock.advance(4)
        assert daemon._due(clock.now) is False
        clock.advance(1)
        assert daemon._due(clock.now) is True

    def test_a_burst_of_events_is_one_pass_after_the_last_one(self, tmp_path: Path) -> None:
        """A recreate is stop, die, destroy, create, start. That is one repair, not five."""
        daemon, reconciler, clock = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")

        for action in ("stop", "die", "destroy", "create", "start"):
            daemon.consider(docker_event(action, "gluetun", PROVIDER_ID))
            clock.advance(1)

        assert daemon._due(clock.now) is False
        clock.advance(5)
        assert daemon._due(clock.now) is True
        assert reconciler.calls == 1

    def test_an_uninteresting_event_does_not_reset_the_quiet_period(self, tmp_path: Path) -> None:
        daemon, _, clock = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")
        daemon.consider(docker_event("die", "gluetun", PROVIDER_ID))
        clock.advance(5)
        assert daemon.consider(docker_event("die", "plex")) is False
        assert daemon._due(clock.now) is True

    def test_the_periodic_pass_still_runs_without_events(self, tmp_path: Path) -> None:
        """The stream is a hint; this is what makes a missed event survivable."""
        daemon, _, clock = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")

        clock.advance(299)
        assert daemon._due(clock.now) is False
        clock.advance(1)
        assert daemon._due(clock.now) is True

    def test_wait_timeout_is_the_remaining_quiet_period(self, tmp_path: Path) -> None:
        daemon, _, clock = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")
        daemon.consider(docker_event("die", "gluetun", PROVIDER_ID))

        clock.advance(2)
        assert daemon._wait_timeout(clock.now) == pytest.approx(3.0)


class TestRemembering:
    def test_after_a_pass_the_provider_and_dependents_are_watched(self, tmp_path: Path) -> None:
        daemon, _, _ = daemon_for(tmp_path, report=provider_report())
        daemon._reconcile("startup")

        assert daemon.consider(docker_event("die", "qbittorrent"))
        assert daemon.consider(docker_event("health_status: unhealthy", "gluetun", PROVIDER_ID))


class TestNotifications:
    def test_a_repair_is_handed_to_the_notifier(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        repair = RemediationResult(
            container="qbittorrent",
            verdict=Verdict.STALE_NAMESPACE,
            action=Action.RESTART,
            succeeded=True,
            detail="restarted",
        )
        report = ReconcileReport(discovery=Discovery(provider=None), results=[repair])
        daemon, _, _ = daemon_for(tmp_path, report=report, sink=sink)

        daemon._reconcile("startup")

        assert sink.sent[0].title == "Tetherd restarted qbittorrent"
        assert sink.sent[0].severity is Severity.WARNING


class TestRun:
    def test_run_reconciles_once_then_stops(self, tmp_path: Path) -> None:
        daemon, reconciler, _ = daemon_for(tmp_path, report=provider_report())
        threading.Timer(0.05, daemon.request_stop).start()

        daemon.run()

        assert reconciler.calls == 1


def _hold_lock(path: str, ready: Any, release: Any) -> None:
    lock = InstanceLock(Path(path))
    lock.acquire()
    ready.set()
    release.wait(timeout=30)
    lock.release()


class TestInstanceLock:
    def test_a_second_process_is_refused(self, tmp_path: Path) -> None:
        """Two remediations racing is how a rename-aside becomes a name collision."""
        path = tmp_path / "tetherd.lock"
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(target=_hold_lock, args=(str(path), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=5)
            with pytest.raises(AlreadyRunningError, match="already holds"):
                InstanceLock(path).acquire()
        finally:
            release.set()
            holder.join(timeout=5)

    def test_the_holder_pid_is_in_the_error(self, tmp_path: Path) -> None:
        path = tmp_path / "tetherd.lock"
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        holder = multiprocessing.Process(target=_hold_lock, args=(str(path), ready, release))
        holder.start()
        try:
            assert ready.wait(timeout=5)
            with pytest.raises(AlreadyRunningError, match=str(holder.pid)):
                InstanceLock(path).acquire()
        finally:
            release.set()
            holder.join(timeout=5)

    def test_release_allows_another_acquire(self, tmp_path: Path) -> None:
        path = tmp_path / "tetherd.lock"
        first = InstanceLock(path)
        first.acquire()
        first.release()

        second = InstanceLock(path)
        second.acquire()
        second.release()

    def test_the_lock_file_records_this_pid(self, tmp_path: Path) -> None:
        path = tmp_path / "tetherd.lock"
        lock = InstanceLock(path)
        lock.acquire()
        try:
            assert path.read_text().strip() == str(os.getpid())
        finally:
            lock.release()
