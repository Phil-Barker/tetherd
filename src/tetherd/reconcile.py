"""One full pass: look at everything, decide, act, and account for it.

Kept separate from the event loop so that a complete pass can be tested without
threads, signals or timing. The loop's only job is deciding *when* to call this.

The order is deliberate. The provider is dealt with first, because a dependent
cannot be repaired against a provider that is down, and repairing dependents
before restarting a dead-tunnelled provider would mean repairing them all twice.
Snapshots are taken from containers observed healthy, which is what makes them a
record of a configuration that demonstrably worked.

Every pass produces an account of itself, including the containers that were
examined and deliberately left alone. "Nothing happened and I cannot tell why" is
the single most common complaint about the project this replaces, and it is a
reporting failure rather than a logic one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .assess import assess
from .config import Settings
from .discovery import Discovery, discover
from .docker_api import DockerApi
from .models import Assessment, ContainerInfo, Verdict
from .notify import Notification, Severity
from .provider import ProviderHealth, ProviderMonitor, ProviderStatus
from .remediate import Action, RemediationResult, Remediator
from .snapshots import SnapshotError, SnapshotStore
from .state import ProviderStateStore
from .storage import StorageError


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one pass saw and did."""

    discovery: Discovery
    provider_status: ProviderStatus | None = None
    provider_restarted: bool = False
    assessments: list[Assessment] = field(default_factory=list)
    results: list[RemediationResult] = field(default_factory=list)
    recovered: list[RemediationResult] = field(default_factory=list)
    snapshots_taken: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def repairs(self) -> list[RemediationResult]:
        """Results where something was actually done, or attempted and failed."""
        return [result for result in self.results if result.action is not Action.NONE]

    @property
    def failures(self) -> list[RemediationResult]:
        return [result for result in self.repairs if not result.succeeded]

    @property
    def acted(self) -> bool:
        return bool(self.repairs) or self.provider_restarted or bool(self.recovered)

    @property
    def healthy(self) -> bool:
        return not self.failures and not self.notes


class Reconciler:
    """Performs a single reconcile pass over the provider and its dependents."""

    def __init__(
        self,
        api: DockerApi,
        settings: Settings,
        *,
        snapshots: SnapshotStore,
        remediator: Remediator,
        monitor: ProviderMonitor,
        state: ProviderStateStore,
    ) -> None:
        self._api = api
        self._settings = settings
        self._snapshots = snapshots
        self._remediator = remediator
        self._monitor = monitor
        self._state = state

    def run_once(self) -> ReconcileReport:
        notes: list[str] = []

        # Before anything else: a rebuild interrupted by a reboot leaves a
        # container renamed aside, and it has to be put back before discovery can
        # see it at all.
        recovered = self._remediator.recover_interrupted()

        known = self._state.load().ids
        discovery = discover(self._api, self._settings, known_provider_ids=known)

        if discovery.provider is None:
            notes.append(
                f"the provider {self._settings.provider!r} does not exist. Nothing "
                "can be repaired until it is back, because a container's network "
                "mode has to name a container that exists."
            )
            return ReconcileReport(discovery=discovery, recovered=recovered, notes=notes)

        provider = discovery.provider
        try:
            self._state.remember(provider.id)
        except StorageError as exc:
            # Losing the provider's ID history costs orphan recognition on a later
            # pass. It must not cost this pass entirely.
            notes.append(str(exc))
        notes.extend(self._scoping_notes(discovery))

        status = self._monitor.check(provider)
        restarted = False
        if status.restart_advised:
            restarted, detail = self._monitor.restart(provider)
            notes.append(detail)
            if restarted:
                # Its ID is unchanged by a restart, but its start time is not, and
                # that is what every dependent is about to be judged against.
                provider = self._refreshed(provider) or provider

        if not status.can_repair_dependents:
            notes.append(
                f"leaving {len(discovery.managed)} dependent(s) alone until "
                f"{provider.name} is back: {status.detail}"
            )
            return ReconcileReport(
                discovery=discovery,
                provider_status=status,
                provider_restarted=restarted,
                recovered=recovered,
                notes=notes,
            )

        assessments: list[Assessment] = []
        results: list[RemediationResult] = []
        snapshotted: list[str] = []

        for dependent in discovery.managed:
            assessment = assess(dependent, provider)
            assessments.append(assessment)

            if assessment.verdict is Verdict.HEALTHY:
                # Only a container seen working is worth recording, because that
                # recording is what a future rebuild replays.
                if self._record(dependent, notes):
                    snapshotted.append(dependent.name)
                continue

            result = self._remediator.remediate(assessment, provider)
            results.append(result)

            if result.succeeded and result.action is not Action.NONE:
                repaired = self._refreshed(dependent, by_name=True)
                if repaired is not None and self._record(repaired, notes):
                    snapshotted.append(repaired.name)

        return ReconcileReport(
            discovery=discovery,
            provider_status=status,
            provider_restarted=restarted,
            assessments=assessments,
            results=results,
            recovered=recovered,
            snapshots_taken=snapshotted,
            notes=notes,
        )

    # -- helpers -----------------------------------------------------------

    def _record(self, container: ContainerInfo, notes: list[str]) -> bool:
        """Snapshot a container, reporting rather than raising if state is unwritable."""
        if self._settings.dry_run:
            return False
        try:
            return self._snapshots.capture(container.payload).is_new
        except SnapshotError as exc:
            message = str(exc)
            if message not in notes:
                notes.append(message)
            return False

    def _refreshed(self, container: ContainerInfo, by_name: bool = False) -> ContainerInfo | None:
        """Re-inspect a container after acting on it.

        A rebuilt container has a new ID, so it can only be found again by name.
        """
        payload = self._api.inspect(container.name if by_name else container.id)
        return ContainerInfo.from_inspect(payload) if payload is not None else None

    def _scoping_notes(self, discovery: Discovery) -> list[str]:
        notes: list[str] = []

        if discovery.adopted:
            names = ", ".join(container.name for container in discovery.adopted)
            notes.append(
                f"adopting {names}: pointing at a container that no longer exists, "
                f"so treating them as dependents of {self._settings.provider}. Set "
                "TETHERD_ADOPT_ORPHANS=false if you run more than one provider and "
                "these belong to the other one."
            )

        for name in discovery.unresolved_includes:
            notes.append(
                f"{name} was named in include but is not borrowing "
                f"{self._settings.provider}'s network, so Tetherd cannot see it. On "
                "Unraid this usually means Network Type is not set to None while "
                "the network is being set in Extra Parameters."
            )
        return notes


def notifications_for(report: ReconcileReport, notify_on_healthy_runs: bool) -> list[Notification]:
    """Turn a pass into the messages worth sending, if any.

    A quiet pass sends nothing by default. Tetherd reconciles every few minutes,
    so notifying on success would train users to ignore it, and the one message
    that mattered would be lost in the noise.
    """
    notifications: list[Notification] = []

    for result in report.failures:
        notifications.append(
            Notification(
                title=f"Tetherd could not repair {result.container}",
                message=result.detail,
                severity=Severity.ERROR,
                context={
                    "container": result.container,
                    "action": str(result.action),
                    "verdict": str(result.verdict),
                    "succeeded": "false",
                },
            )
        )

    succeeded = [result for result in report.repairs if result.succeeded]
    if succeeded:
        notifications.append(
            Notification(
                title=_repair_title(succeeded),
                message="\n".join(f"{r.container}: {r.detail}" for r in succeeded),
                severity=Severity.WARNING,
                context={
                    "containers": ",".join(result.container for result in succeeded),
                    "succeeded": "true",
                },
            )
        )

    if report.provider_status is not None and report.provider_status.health is ProviderHealth.DOWN:
        notifications.append(
            Notification(
                title="Tetherd is waiting for the provider",
                message=report.provider_status.detail,
                severity=Severity.WARNING,
                context={"provider": report.provider_status.detail},
            )
        )

    if not notifications and notify_on_healthy_runs:
        managed = len(report.discovery.managed)
        notifications.append(
            Notification(
                title="Tetherd checked in",
                message=f"{managed} container(s) sharing the provider's network are healthy",
                severity=Severity.INFO,
            )
        )

    return notifications


def _repair_title(results: Sequence[RemediationResult]) -> str:
    if len(results) == 1:
        result = results[0]
        verb = "restarted" if result.action is Action.RESTART else "rebuilt"
        return f"Tetherd {verb} {result.container}"
    return f"Tetherd repaired {len(results)} containers"
