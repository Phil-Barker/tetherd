"""The long-running process: when to reconcile, and that only one of us is doing it.

Reconcile itself lives in ``reconcile.py``. This module's job is narrower: hold a
lock so two Tetherd processes cannot repair the same container at once, watch the
Docker event stream, wait out a quiet period so a recreate burst becomes one pass,
and fall back to a timer in case an event was missed.

The event stream is a hint, not a source of truth. Docker can drop events, a
healthcheck can fail without emitting one we subscribed to, and Tetherd itself can
be down while the provider is replaced. The periodic pass is what makes those
survivable; the stream is what keeps the common case measured in seconds rather
than minutes.
"""

from __future__ import annotations

import fcntl
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import structlog

from .config import Settings
from .docker_api import DockerApi
from .notify import Notifier, build_notifier
from .provider import ProviderMonitor
from .reconcile import Reconciler, ReconcileReport, notifications_for
from .remediate import Remediator
from .snapshots import SnapshotStore
from .state import ProviderStateStore

log = structlog.get_logger("tetherd")

#: Actions that can mean a provider or dependent has changed network identity.
#: ``health_status`` is prefix-matched because the daemon emits
#: ``health_status: unhealthy`` as the action itself.
WATCHED_ACTIONS: Final = frozenset(
    {
        "create",
        "start",
        "restart",
        "stop",
        "die",
        "kill",
        "destroy",
        "pause",
        "unpause",
        "health_status",
    }
)

_EVENT_RECONNECT_SECONDS: Final = 5.0
_LOCK_MODE: Final = 0o644


class AlreadyRunningError(RuntimeError):
    """Another Tetherd process holds the instance lock."""


@dataclass
class Runtime:
    """The collaborators a process needs, wired once from configuration."""

    settings: Settings
    api: DockerApi
    snapshots: SnapshotStore
    remediator: Remediator
    reconciler: Reconciler
    monitor: ProviderMonitor
    notifier: Notifier
    lock: InstanceLock
    state: ProviderStateStore

    def close(self) -> None:
        self.api.close()


class InstanceLock:
    """An exclusive file lock, so two Tetherd processes cannot remediate at once.

    Two remediations racing on the same container is how a rename-aside stops
    being a checkpoint and starts being a name collision. The lock lives in the
    state directory because that is already required to be writable and shared
    across a host's only Tetherd instance.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, _LOCK_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _pid_in(fd)
            os.close(fd)
            where = f" (pid {holder})" if holder else ""
            raise AlreadyRunningError(
                f"another Tetherd process{where} already holds {self._path}. "
                "Running two at once would race on the same containers."
            ) from exc

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class Daemon:
    """Watches Docker and runs a reconcile pass whenever something may have changed."""

    def __init__(
        self,
        settings: Settings,
        api: DockerApi,
        reconciler: Reconciler,
        notifier: Notifier,
        *,
        lock: InstanceLock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] | None = None,
        events: Callable[[], Iterator[Mapping[str, Any]]] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._settings = settings
        self._api = api
        self._reconciler = reconciler
        self._notifier = notifier
        self._lock = lock or InstanceLock(settings.state_dir / "tetherd.lock")
        self._monotonic = monotonic
        self._wait = wait or _wait_for
        self._events = events or (lambda: api.container_events(set(WATCHED_ACTIONS)))
        self._stop = stop or threading.Event()
        self._wake = threading.Event()

        self._pending = False
        self._last_event_at = 0.0
        self._last_reconcile_at = 0.0
        self._watched_names: set[str] = set()
        self._provider_ids: set[str] = set()

    def request_stop(self) -> None:
        """Ask the loop to exit. Safe to call from a signal handler or another thread."""
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        """Block until asked to stop. Acquires the instance lock for the duration."""
        self._install_signals()
        with self._lock:
            log.info(
                "starting",
                provider=self._settings.provider,
                dry_run=self._settings.dry_run,
            )
            self._reconcile(reason="startup")
            consumer = threading.Thread(
                target=self._consume_events, name="tetherd-events", daemon=True
            )
            consumer.start()
            try:
                self._loop()
            finally:
                self.request_stop()
                log.info("stopped")

    # -- loop --------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            timeout = self._wait_timeout(self._monotonic())
            self._wait(self._wake, timeout)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._due(self._monotonic()):
                reason = "event" if self._pending else "interval"
                self._reconcile(reason=reason)

    def _consume_events(self) -> None:
        while not self._stop.is_set():
            try:
                for event in self._events():
                    if self._stop.is_set():
                        return
                    self.consider(event)
            except Exception:
                if self._stop.is_set():
                    return
                log.exception(
                    "event stream dropped; reconnecting",
                    retry_in=_EVENT_RECONNECT_SECONDS,
                )
            # The iterator ending is as much a drop as an exception: spinning
            # against a closed stream would pin a core until the next reconnect.
            if self._stop.is_set() or self._wait(self._stop, _EVENT_RECONNECT_SECONDS):
                return

    def consider(self, event: Mapping[str, Any]) -> bool:
        """Record an event if it might affect managed containers.

        Public so tests can feed events without standing up a consumer thread.
        Returns whether the event was of interest.
        """
        if not event_is_interesting(
            event,
            provider=self._settings.provider,
            provider_ids=self._provider_ids,
            managed_names=self._watched_names,
        ):
            return False
        self._pending = True
        self._last_event_at = self._monotonic()
        self._wake.set()
        log.debug("event queued a reconcile", action=_event_action(event), **_event_actor(event))
        return True

    def _due(self, now: float) -> bool:
        if self._pending and now - self._last_event_at >= self._settings.event_debounce_seconds:
            return True
        return now - self._last_reconcile_at >= self._settings.reconcile_interval_seconds

    def _wait_timeout(self, now: float) -> float:
        until_interval = self._settings.reconcile_interval_seconds - (now - self._last_reconcile_at)
        if not self._pending:
            return max(0.0, until_interval)
        until_debounce = self._settings.event_debounce_seconds - (now - self._last_event_at)
        return max(0.0, min(until_interval, until_debounce))

    def _reconcile(self, reason: str) -> ReconcileReport:
        self._pending = False
        self._last_reconcile_at = self._monotonic()
        log.info("reconciling", reason=reason)
        report = self._reconciler.run_once()
        self._remember(report)
        _log_report(report)
        self._notify(report)
        return report

    def _remember(self, report: ReconcileReport) -> None:
        self._watched_names = {container.name for container in report.discovery.managed}
        # IDs accumulate for the life of the process so a destroy event for a
        # just-replaced provider still matches. The name covers recreate.
        if report.discovery.provider is not None:
            self._provider_ids.add(report.discovery.provider.id)

    def _notify(self, report: ReconcileReport) -> None:
        for notification in notifications_for(report, self._settings.notify.notify_on_healthy_runs):
            failures = self._notifier.send(notification)
            for failure in failures:
                log.warning("notification failed", detail=failure)

    def _install_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.request_stop())


def event_is_interesting(
    event: Mapping[str, Any],
    *,
    provider: str,
    provider_ids: Set[str],
    managed_names: Set[str],
) -> bool:
    """Whether an event should wake a reconcile pass.

    Provider events always do. So do events about a container we already manage.
    ``create`` and ``start`` of anything else also do, because that is how a newly
    added dependent is noticed before the next periodic pass — the pass itself
    will decide it is not ours.
    """
    action = _event_action(event)
    if not _action_watched(action):
        return False

    actor = _event_actor(event)
    name = actor.get("name", "")
    actor_id = actor.get("id", "")

    if name == provider or _id_matches(actor_id, provider_ids):
        return True
    if name in managed_names:
        return True
    # A newly added dependent is noticed on create/start rather than waiting
    # for the periodic pass. Health of an unrelated container is not our problem.
    return action in {"create", "start"}


def assemble(settings: Settings) -> Runtime:
    """Build the long-running collaborators from configuration.

    Kept here so the CLI and the daemon share one wiring, and so tests can swap
    any one of them without reconstructing the rest.
    """
    api = DockerApi(host=settings.docker_host)
    snapshots = SnapshotStore(settings.snapshot_dir, settings.snapshot_retention)
    remediator = Remediator(
        api,
        snapshots,
        dry_run=settings.dry_run,
        restart_grace_seconds=settings.restart_grace_seconds,
    )
    state = ProviderStateStore(settings.provider_state_file)
    monitor = ProviderMonitor(api, settings.probe)
    reconciler = Reconciler(
        api,
        settings,
        snapshots=snapshots,
        remediator=remediator,
        monitor=monitor,
        state=state,
    )
    return Runtime(
        settings=settings,
        api=api,
        snapshots=snapshots,
        remediator=remediator,
        reconciler=reconciler,
        monitor=monitor,
        notifier=build_notifier(settings.notify),
        lock=InstanceLock(settings.state_dir / "tetherd.lock"),
        state=state,
    )


def _log_report(report: ReconcileReport) -> None:
    for note in report.notes:
        log.info("note", detail=note)
    for skipped in report.discovery.skipped:
        log.info(
            "skipped",
            container=skipped.container.name,
            reason=str(skipped.reason),
            detail=skipped.detail,
        )
    for result in report.results:
        method = log.info if result.succeeded else log.error
        method(
            "repair",
            container=result.container,
            action=str(result.action),
            succeeded=result.succeeded,
            detail=result.detail,
        )
    if not report.acted:
        log.info(
            "nothing to do",
            managed=len(report.discovery.managed),
            skipped=len(report.discovery.skipped),
        )


def _event_action(event: Mapping[str, Any]) -> str:
    return str(event.get("Action") or event.get("status") or "")


def _event_actor(event: Mapping[str, Any]) -> dict[str, str]:
    actor = event.get("Actor") or {}
    attributes = actor.get("Attributes") or {}
    return {
        "name": str(attributes.get("name") or ""),
        "id": str(actor.get("ID") or event.get("id") or ""),
    }


def _action_watched(action: str) -> bool:
    if action in WATCHED_ACTIONS:
        return True
    return any(action.startswith(f"{watched}:") for watched in WATCHED_ACTIONS)


def _pid_in(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 32).decode("ascii", errors="ignore").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


def _id_matches(actor_id: str, known_ids: Set[str]) -> bool:
    """Full-ID equality, or a prefix long enough not to be a substring accident.

    Docker events sometimes carry a truncated ID. Twelve characters is Docker's
    own default abbreviation; shorter than that is how 'abc' would match the
    wrong container.
    """
    if not actor_id:
        return False
    if actor_id in known_ids:
        return True
    if len(actor_id) < 12:
        return False
    return any(known.startswith(actor_id) or actor_id.startswith(known) for known in known_ids)


def _wait_for(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)
