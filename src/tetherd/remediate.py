"""Repair a dependent container, escalating only as far as necessary.

The project this replaces has one remediation: stop, remove, recreate from an
Unraid template. That is destructive, it is applied to both failure modes even
though only one needs it, and it removes the container *before* discovering
whether the replacement can be created — which is why upstream issues #80, #69
and #65 end with a container that no longer exists.

Tetherd escalates instead.

A stale namespace, where the reference is still valid but the namespace behind it
was replaced, needs nothing more than a restart. Restarting re-enters the
provider's current namespace, so the container is never destroyed and its
configuration is never re-derived from anything.

A dead reference, where the provider was recreated and the recorded ID no longer
exists, does need a new container, because the network mode is fixed at creation.
Even then the old container is renamed aside rather than removed, and is only
removed once the replacement is running and verified. If anything fails in
between, the replacement is discarded and the original is renamed back and
restarted, leaving the host as it was found.

Two ordering rules make that safe, and both are the direct inverse of how the
predecessor fails:

- the create request is assembled and validated before anything is stopped, so an
  unbuildable configuration costs nothing
- nothing is removed until a verified replacement is running

The rename-aside is also a crash-safe checkpoint. If Tetherd is killed mid-rebuild
— a host reboot, an OOM kill — the next run finds a container missing and its
aside copy present, and puts it back.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from .assess import assess
from .docker_api import ContainerOperationError, DockerApi
from .models import Assessment, ContainerInfo, Verdict
from .payload import CreateRequest, PayloadError, StrippedField, build_create_request
from .snapshots import SnapshotStore

#: Suffix for a container renamed aside during a rebuild. Docker permits `.` and
#: `-` in names, and the namespace is distinctive enough to be recognised on a
#: later run without ever being confused for a user's own container.
ASIDE_SUFFIX: Final = ".tetherd-old"

_POLL_INTERVAL: Final = 0.5


class Action(StrEnum):
    """What was done, or would be done, to repair a container."""

    NONE = "none"
    RESTART = "restart"
    RECREATE = "recreate"


@dataclass(frozen=True, slots=True)
class RemediationResult:
    """The outcome of remediating one container, and why.

    ``detail`` is user-facing. Being unable to explain what happened, or why
    nothing happened, is the largest single source of support burden on the
    project this replaces.
    """

    container: str
    verdict: Verdict
    action: Action
    succeeded: bool
    detail: str
    escalated: bool = False
    rolled_back: bool = False
    stripped: tuple[StrippedField, ...] = field(default_factory=tuple)

    @property
    def changed_anything(self) -> bool:
        return self.succeeded and self.action is not Action.NONE


class Remediator:
    """Applies the least destructive repair that fixes each failure mode."""

    def __init__(
        self,
        docker: DockerApi,
        snapshots: SnapshotStore,
        *,
        dry_run: bool = False,
        restart_grace_seconds: float = 15.0,
        stop_timeout: int = 30,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._docker = docker
        self._snapshots = snapshots
        self._dry_run = dry_run
        self._grace = restart_grace_seconds
        self._stop_timeout = stop_timeout
        self._sleep = sleep

    def remediate(self, assessment: Assessment, provider: ContainerInfo) -> RemediationResult:
        """Repair one container, escalating from restart to rebuild if needed."""
        container = assessment.container

        if not assessment.needs_action:
            return RemediationResult(
                container=container.name,
                verdict=assessment.verdict,
                action=Action.NONE,
                succeeded=True,
                detail=assessment.reason,
            )

        if assessment.verdict is Verdict.STALE_NAMESPACE:
            restarted = self._restart(container, provider)
            if restarted.succeeded:
                return restarted
            # A restart that does not take hold means the recorded network mode
            # is no longer usable, whatever the timestamps implied.
            rebuilt = self._recreate(container, provider, assessment.verdict)
            return _escalated(rebuilt, restarted.detail)

        return self._recreate(container, provider, assessment.verdict)

    def recover_interrupted(self) -> list[RemediationResult]:
        """Restore any container left renamed aside by an interrupted rebuild.

        A rebuild can only be interrupted between the rename and the verified
        replacement, so an aside container whose original name is now free is
        unambiguously ours to put back. If the original name *is* taken, the
        rebuild got far enough to create a replacement and the aside copy is
        litter to be swept up.
        """
        results: list[RemediationResult] = []
        for container_id, aside_name in self._docker.find_by_name_suffix(ASIDE_SUFFIX):
            original = _original_name(aside_name)
            if original:
                results.append(self._restore_or_discard(container_id, aside_name, original))
        return results

    # -- tier one: restart -------------------------------------------------

    def _restart(self, container: ContainerInfo, provider: ContainerInfo) -> RemediationResult:
        if self._dry_run:
            return self._planned(container, Verdict.STALE_NAMESPACE, Action.RESTART, "restart it")

        try:
            self._docker.restart(container.id, timeout=self._stop_timeout)
        except ContainerOperationError as exc:
            return RemediationResult(
                container=container.name,
                verdict=Verdict.STALE_NAMESPACE,
                action=Action.RESTART,
                succeeded=False,
                detail=f"restart failed: {exc.detail}",
            )

        healthy, why = self._wait_until_healthy(container.name, provider)
        return RemediationResult(
            container=container.name,
            verdict=Verdict.STALE_NAMESPACE,
            action=Action.RESTART,
            succeeded=healthy,
            detail=(
                f"restarted; now {why}" if healthy else f"restarted, but it did not recover: {why}"
            ),
        )

    # -- tier two: recreate ------------------------------------------------

    def _recreate(
        self, container: ContainerInfo, provider: ContainerInfo, verdict: Verdict
    ) -> RemediationResult:
        source, origin = self._configuration_for(container)
        if source is None:
            return RemediationResult(
                container=container.name,
                verdict=verdict,
                action=Action.RECREATE,
                succeeded=False,
                detail=(
                    f"cannot rebuild {container.name}: no configuration is available. "
                    "Tetherd records one the first time it sees a container healthy, "
                    "so this only happens if it has never observed this container."
                ),
            )

        # Assembled before anything is touched. An unbuildable configuration must
        # cost nothing, which is exactly what upstream gets wrong.
        try:
            request = build_create_request(source, provider_id=provider.id, name=container.name)
        except PayloadError as exc:
            return RemediationResult(
                container=container.name,
                verdict=verdict,
                action=Action.RECREATE,
                succeeded=False,
                detail=f"cannot rebuild {container.name}: {exc}",
            )

        if self._dry_run:
            return self._planned(
                container,
                verdict,
                Action.RECREATE,
                f"rebuild it from its {origin} configuration and attach it to {provider.name}",
                stripped=request.stripped,
            )

        return self._perform_rebuild(container, provider, verdict, request, origin)

    def _perform_rebuild(
        self,
        container: ContainerInfo,
        provider: ContainerInfo,
        verdict: Verdict,
        request: CreateRequest,
        origin: str,
    ) -> RemediationResult:
        was_running = container.running
        existed = self._docker.exists(container.id)
        aside: str | None = None

        try:
            if existed:
                if was_running:
                    self._docker.stop(container.id, timeout=self._stop_timeout)
                aside = self._aside_name(container.name)
                self._docker.rename(container.id, aside)

            created_id = self._docker.create(request.name, request.body)
            if was_running or not existed:
                self._docker.start(created_id)
        except ContainerOperationError as exc:
            return self._roll_back(container, verdict, aside, was_running, exc.detail)

        if was_running or not existed:
            healthy, why = self._wait_until_healthy(container.name, provider)
            if not healthy:
                return self._roll_back(
                    container,
                    verdict,
                    aside,
                    was_running,
                    f"the replacement did not recover: {why}",
                )

        # Only now is the original expendable.
        if aside is not None:
            self._discard(aside)

        return RemediationResult(
            container=container.name,
            verdict=verdict,
            action=Action.RECREATE,
            succeeded=True,
            detail=f"rebuilt from its {origin} configuration and attached to {provider.name}",
            stripped=request.stripped,
        )

    def _roll_back(
        self,
        container: ContainerInfo,
        verdict: Verdict,
        aside: str | None,
        was_running: bool,
        why: str,
    ) -> RemediationResult:
        """Discard the replacement and put the original back as it was found."""
        # The replacement may exist under the original name if creation succeeded
        # and a later step failed.
        if aside is not None:
            self._discard(container.name)

        restored = True
        if aside is not None:
            try:
                self._docker.rename(aside, container.name)
                if was_running:
                    self._docker.start(container.name)
            except ContainerOperationError:
                restored = False

        detail = f"rebuild failed: {why}."
        if aside is None:
            detail += " Nothing was changed."
        elif restored:
            detail += f" The original {container.name} was put back as it was found."
        else:
            detail += (
                f" The original could not be renamed back and is still present as "
                f"{aside}; rename it to {container.name} to restore it."
            )

        return RemediationResult(
            container=container.name,
            verdict=verdict,
            action=Action.RECREATE,
            succeeded=False,
            detail=detail,
            rolled_back=restored and aside is not None,
        )

    # -- helpers -----------------------------------------------------------

    def _configuration_for(self, container: ContainerInfo) -> tuple[Mapping[str, Any] | None, str]:
        """The freshest usable configuration, and where it came from.

        A live inspect is preferred over a stored snapshot: it is by definition
        current, and it includes any change the user made through the Unraid UI
        since the snapshot was taken. The snapshot is the fallback for a container
        that no longer exists at all.
        """
        live = self._docker.inspect(container.id)
        if live is not None:
            return live, "live"

        snapshot = self._snapshots.latest(container.name)
        if snapshot is not None:
            return snapshot.payload, f"recorded {snapshot.age}"
        return None, "unknown"

    def _aside_name(self, name: str) -> str:
        """A free name to park the original under.

        A leftover from an earlier interrupted run may already hold the obvious
        one, and overwriting it is not an option: it may be the only copy of a
        container's configuration.
        """
        candidate = f"{name}{ASIDE_SUFFIX}"
        suffix = 1
        while self._docker.exists(candidate):
            candidate = f"{name}{ASIDE_SUFFIX}-{suffix}"
            suffix += 1
        return candidate

    def _discard(self, ref: str) -> None:
        # Best effort by design: failing to tidy up must never turn a successful
        # repair into a reported failure.
        with contextlib.suppress(ContainerOperationError):
            self._docker.remove(ref, force=True)

    def _restore_or_discard(
        self, container_id: str, aside_name: str, original: str
    ) -> RemediationResult:
        if self._dry_run:
            action = "restore" if not self._docker.exists(original) else "remove"
            return RemediationResult(
                container=original,
                verdict=Verdict.DEAD_PROVIDER_REF,
                action=Action.RECREATE,
                succeeded=True,
                detail=f"would {action} {aside_name}, left behind by an interrupted rebuild",
            )

        if self._docker.exists(original):
            self._discard(container_id)
            return RemediationResult(
                container=original,
                verdict=Verdict.HEALTHY,
                action=Action.NONE,
                succeeded=True,
                detail=f"removed {aside_name}, left behind by an interrupted rebuild",
            )

        try:
            self._docker.rename(container_id, original)
        except ContainerOperationError as exc:
            return RemediationResult(
                container=original,
                verdict=Verdict.DEAD_PROVIDER_REF,
                action=Action.RECREATE,
                succeeded=False,
                detail=(
                    f"{original} is missing and {aside_name} could not be renamed "
                    f"back to it: {exc.detail}"
                ),
            )

        return RemediationResult(
            container=original,
            verdict=Verdict.DEAD_PROVIDER_REF,
            action=Action.RECREATE,
            succeeded=True,
            detail=(
                f"restored {original} from {aside_name}, left behind by an "
                "interrupted rebuild; it will be repaired on this pass"
            ),
        )

    def _planned(
        self,
        container: ContainerInfo,
        verdict: Verdict,
        action: Action,
        what: str,
        stripped: tuple[StrippedField, ...] = (),
    ) -> RemediationResult:
        return RemediationResult(
            container=container.name,
            verdict=verdict,
            action=action,
            succeeded=True,
            detail=f"dry run: would {what}",
            stripped=stripped,
        )

    def _wait_until_healthy(self, name: str, provider: ContainerInfo) -> tuple[bool, str]:
        """Poll until the container is assessed healthy, or the grace period ends.

        The provider is re-inspected each round rather than trusted from the
        caller: its start time is what a stale namespace is judged against, and it
        may itself have just been restarted.
        """
        deadline = self._grace
        waited = 0.0
        reason = "it did not come up"

        while True:
            current = self._inspect_info(name)
            if current is not None:
                latest_provider = self._inspect_info(provider.id) or provider
                verdict = assess(current, latest_provider)
                if verdict.verdict is Verdict.HEALTHY:
                    return True, verdict.reason
                reason = verdict.reason

            if waited >= deadline:
                return False, reason
            self._sleep(min(_POLL_INTERVAL, deadline - waited))
            waited += _POLL_INTERVAL

    def _inspect_info(self, ref: str) -> ContainerInfo | None:
        payload = self._docker.inspect(ref)
        return ContainerInfo.from_inspect(payload) if payload is not None else None


def _original_name(aside_name: str) -> str:
    """The container name an aside copy was parked from, or "" if unrecognisable.

    Handles the disambiguating suffix added when an earlier aside copy already
    occupied the obvious name.
    """
    base, _, remainder = aside_name.partition(ASIDE_SUFFIX)
    if not base or (remainder and not remainder.lstrip("-").isdigit()):
        return ""
    return base


def _escalated(result: RemediationResult, first_attempt: str) -> RemediationResult:
    """Fold a failed restart into the rebuild result that followed it."""
    return RemediationResult(
        container=result.container,
        verdict=result.verdict,
        action=result.action,
        succeeded=result.succeeded,
        detail=f"{first_attempt}; escalated to a rebuild: {result.detail}",
        escalated=True,
        rolled_back=result.rolled_back,
        stripped=result.stripped,
    )
