"""Watch the provider container itself, not just the containers borrowing from it.

A VPN container can be running, healthy from Docker's point of view as a process,
and completely unable to route traffic. Every dependent is then up, correctly
attached, and offline. Nothing in Unraid notices this, and neither does the
project Tetherd replaces: both only ever ask whether the container is running.

Health is established from the cheapest sufficient source.

The provider's own Docker healthcheck comes first. Where one is defined, the
daemon is already running it on a schedule, from inside the namespace, using a
command the image author chose — gluetun ships one, and the Unraid survey found
users adding their own through Extra Parameters. Reading that verdict costs one
inspect, assumes nothing about what binaries exist in the image, and cannot
disagree with what the container itself reports.

An exec probe is the fallback for a provider with no healthcheck. It is
deliberately second choice: it depends on a usable network tool being present, and
a missing tool must never be mistaken for a dead tunnel. If no probe can run, the
provider is reported as unmonitored with an explanation, because restarting a VPN
container on the strength of a missing `ping` binary would be far worse than not
checking at all.

Restarts are rate-limited. Nothing observable from inside a tunnel distinguishes a
dead tunnel from an ISP outage, so an unbounded response to "cannot reach the
internet" is a loop that takes every dependent down on each pass.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from .config import ProbeSettings
from .docker_api import ContainerOperationError, DockerApi
from .models import ContainerInfo

#: Docker reports a container with no healthcheck, or one explicitly disabled, as
#: an empty test or the single directive NONE.
_DISABLED_HEALTHCHECK: Final = frozenset({"", "NONE"})

#: Substrings the daemon uses when the requested binary is not in the image. Used
#: to tell "this image has no ping" from "ping ran and failed".
_MISSING_BINARY_MARKERS: Final = ("executable file not found", "no such file or directory")

#: Exit codes a shell uses for "command not found" and "found but not executable".
_MISSING_BINARY_EXIT_CODES: Final = frozenset({126, 127})


class _ToolState(StrEnum):
    """Whether a probe command could be run at all, distinct from its result."""

    OK = "ok"
    MISSING = "missing"
    ERROR = "error"


class ProviderHealth(StrEnum):
    """What is known about the provider's ability to carry traffic."""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    """Running, but cannot reach the outside world. The failure nothing else catches."""

    DOWN = "down"
    """Not running. Repairing dependents now would only fail."""

    STARTING = "starting"
    """Inside its healthcheck's start period; too early to judge."""

    UNMONITORED = "unmonitored"
    """No healthcheck and no usable probe, so connectivity is unknown.

    Deliberately not a failure. Acting on an absence of evidence would mean
    restarting a working VPN container because its image lacks a network tool.
    """


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """The provider's health, where the verdict came from, and what to do."""

    health: ProviderHealth
    detail: str
    source: str
    consecutive_failures: int = 0
    restart_advised: bool = False

    @property
    def can_repair_dependents(self) -> bool:
        """Whether repairing dependents against this provider can succeed.

        Unreachable still counts: the namespace is intact and dependents attached
        to it are correctly attached. Their connectivity is the provider's
        problem, not something a rebuild would fix.
        """
        return self.health in (
            ProviderHealth.HEALTHY,
            ProviderHealth.UNREACHABLE,
            ProviderHealth.STARTING,
            ProviderHealth.UNMONITORED,
        )


class ProviderMonitor:
    """Tracks provider connectivity across rounds and decides when to restart it."""

    def __init__(
        self,
        docker: DockerApi,
        settings: ProbeSettings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._docker = docker
        self._settings = settings
        self._sleep = sleep
        self._monotonic = monotonic

        self._consecutive_failures = 0
        self._last_restart_at: float | None = None
        self._probe_unavailable_reason: str | None = None

    def check(self, provider: ContainerInfo) -> ProviderStatus:
        """Assess the provider, updating the consecutive-failure count."""
        if not provider.running:
            self._consecutive_failures = 0
            return ProviderStatus(
                health=ProviderHealth.DOWN,
                detail=f"{provider.name} is not running",
                source="container state",
            )

        health, detail, source = self._assess_connectivity(provider)

        if health is ProviderHealth.HEALTHY:
            self._consecutive_failures = 0
            return ProviderStatus(health=health, detail=detail, source=source)

        if health is not ProviderHealth.UNREACHABLE:
            # Starting and unmonitored are both "no verdict", so they must not
            # accumulate towards a restart.
            return ProviderStatus(health=health, detail=detail, source=source)

        self._consecutive_failures += 1
        threshold = self._settings.failures_before_restart
        advised = (
            self._settings.restart_provider_on_failure and self._consecutive_failures >= threshold
        )
        blocked = self._restart_blocked_for()

        if advised and blocked > 0:
            advised = False
            detail = (
                f"{detail}; a restart is due but was last done "
                f"{int(blocked)}s too recently, so it is being held off in case "
                "the outage is upstream rather than in the tunnel"
            )

        return ProviderStatus(
            health=health,
            detail=detail,
            source=source,
            consecutive_failures=self._consecutive_failures,
            restart_advised=advised,
        )

    def restart(self, provider: ContainerInfo) -> tuple[bool, str]:
        """Restart the provider and wait for it to settle.

        Dependents are not repaired here. They are all about to be found holding a
        stale namespace, which the normal remediation path handles, and doing it
        there keeps one code path responsible for repairs.
        """
        try:
            self._docker.restart(provider.id, timeout=30)
        except ContainerOperationError as exc:
            return False, f"could not restart {provider.name}: {exc.detail}"

        self._last_restart_at = self._monotonic()
        self._consecutive_failures = 0

        if self._settings.settle_seconds > 0:
            self._sleep(self._settings.settle_seconds)

        return True, (
            f"restarted {provider.name} after "
            f"{self._settings.failures_before_restart} failed connectivity rounds; "
            "its dependents now hold a stale namespace and will be repaired"
        )

    # -- connectivity ------------------------------------------------------

    def _assess_connectivity(self, provider: ContainerInfo) -> tuple[ProviderHealth, str, str]:
        healthcheck = _healthcheck_verdict(provider.payload)
        if healthcheck is not None:
            return healthcheck

        if not self._settings.enabled:
            return (
                ProviderHealth.UNMONITORED,
                (
                    f"{provider.name} has no healthcheck and probing is disabled, so "
                    "a dead tunnel behind a running container would go unnoticed. "
                    "Either add a healthcheck to the container or set "
                    "TETHERD_PROBE__ENABLED=true."
                ),
                "none",
            )

        return self._probe(provider)

    def _probe(self, provider: ContainerInfo) -> tuple[ProviderHealth, str, str]:
        if self._probe_unavailable_reason is not None:
            return ProviderHealth.UNMONITORED, self._probe_unavailable_reason, "none"

        if not self._settings.targets:
            return (
                ProviderHealth.UNMONITORED,
                "probing is enabled but no targets are configured",
                "none",
            )

        tool_used: str | None = None
        failure: str | None = None

        for target in self._settings.targets:
            for command in _probe_commands(target, self._settings.timeout_seconds):
                state, reachable, detail = self._run(provider, command)

                if state is _ToolState.MISSING:
                    continue
                if state is _ToolState.ERROR:
                    failure = detail
                    break

                tool_used = command[0]
                if reachable:
                    return (
                        ProviderHealth.HEALTHY,
                        f"reached {target} from inside {provider.name}",
                        f"probe: {' '.join(command)}",
                    )
                # The tool works and the target did not answer, so trying another
                # tool against the same target would tell us nothing new.
                break

        if tool_used is not None:
            targets = ", ".join(self._settings.targets)
            return (
                ProviderHealth.UNREACHABLE,
                f"{provider.name} is running but could not reach any of {targets}",
                f"probe: {tool_used}",
            )

        if failure is not None:
            # Something went wrong running the probe rather than the probe
            # reporting a result. Not cached: it may well be transient.
            return (
                ProviderHealth.UNMONITORED,
                f"could not probe {provider.name}: {failure}",
                "none",
            )

        self._probe_unavailable_reason = (
            f"cannot probe {provider.name}: none of {', '.join(sorted(_PROBE_TOOLS))} "
            "exist in the image, so there is no way to test connectivity from "
            "inside it. Probing is now off for this provider rather than guessing, "
            "because restarting a working VPN container over a missing binary "
            "would be worse than not checking. Add a healthcheck to the container "
            "instead: Docker runs it from inside the namespace and Tetherd will "
            "use its verdict."
        )
        return ProviderHealth.UNMONITORED, self._probe_unavailable_reason, "none"

    def _run(self, provider: ContainerInfo, command: Sequence[str]) -> tuple[_ToolState, bool, str]:
        """Run one probe command inside the provider.

        The three-way result matters: a tool that is absent, a tool that could not
        be launched, and a tool that ran and reported failure demand different
        responses, and only the last is evidence about the network.
        """
        try:
            exit_code, output = self._docker.exec_probe(
                provider.id, list(command), self._settings.timeout_seconds
            )
        except ContainerOperationError as exc:
            state = _ToolState.MISSING if _is_missing_binary(exc.detail) else _ToolState.ERROR
            return state, False, exc.detail

        if exit_code in _MISSING_BINARY_EXIT_CODES or _is_missing_binary(output):
            return _ToolState.MISSING, False, output
        return _ToolState.OK, exit_code == 0, output

    def _restart_blocked_for(self) -> float:
        """Seconds still to wait before another restart is permitted."""
        if self._last_restart_at is None:
            return 0.0
        elapsed = self._monotonic() - self._last_restart_at
        return max(0.0, self._settings.min_restart_interval_seconds - elapsed)


def _healthcheck_verdict(
    payload: Mapping[str, Any],
) -> tuple[ProviderHealth, str, str] | None:
    """Translate Docker's own health status, or None if there is no healthcheck."""
    test = (payload.get("Config") or {}).get("Healthcheck", {}).get("Test") or []
    directive = str(test[0]) if test else ""
    if directive in _DISABLED_HEALTHCHECK:
        return None

    status = str(((payload.get("State") or {}).get("Health") or {}).get("Status", ""))
    name = str(payload.get("Name", "")).lstrip("/")
    source = "docker healthcheck"

    if status == "healthy":
        return ProviderHealth.HEALTHY, f"{name} reports itself healthy", source
    if status == "unhealthy":
        return (
            ProviderHealth.UNREACHABLE,
            f"{name} reports itself unhealthy: {_last_healthcheck_output(payload)}",
            source,
        )
    if status == "starting":
        return (
            ProviderHealth.STARTING,
            f"{name} is still within its healthcheck start period",
            source,
        )

    # A healthcheck is configured but the daemon has no status for it yet.
    return ProviderHealth.STARTING, f"{name} has not reported a health status yet", source


def _last_healthcheck_output(payload: Mapping[str, Any]) -> str:
    """The healthcheck's own last words, which are what a user needs to see."""
    log = ((payload.get("State") or {}).get("Health") or {}).get("Log") or []
    if not log:
        return "no output recorded"
    output = str(log[-1].get("Output", "")).strip()
    return output.splitlines()[-1] if output else "no output recorded"


#: Tools tried inside the provider, in order of preference.
_PROBE_TOOLS: Final = frozenset({"ping", "nc", "wget"})


def _probe_commands(target: str, timeout: float) -> list[list[str]]:
    """Candidate probe commands for a target, most reliable first.

    A target may be given as ``host`` or ``host:port``. A port makes the check a
    TCP connect, which is the more trustworthy signal: ICMP is frequently dropped
    by the far end or by the tunnel provider, so a failed ping does not always
    mean a failed tunnel.
    """
    host, _, port = target.partition(":")
    seconds = max(1, int(timeout))

    if port:
        return [
            ["nc", "-z", "-w", str(seconds), host, port],
            ["wget", "-q", "-T", str(seconds), "-O", "/dev/null", f"http://{host}:{port}"],
        ]
    return [
        ["ping", "-c", "1", "-W", str(seconds), host],
        ["nc", "-z", "-w", str(seconds), host, "443"],
    ]


def _is_missing_binary(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _MISSING_BINARY_MARKERS)
