"""Domain types projected from Docker inspect payloads.

Tetherd deliberately keeps the full inspect payload alongside the projected
fields: the projection is what decisions are made from, and the payload is what
a rebuild is replayed from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

CONTAINER_NETWORK_PREFIX = "container:"

# Docker reports a never-started container's StartedAt as the Go zero time.
_ZERO_TIME_PREFIX = "0001-01-01"


def parse_docker_timestamp(value: str | None) -> datetime | None:
    """Parse a Docker RFC3339 timestamp, returning None for absent or zero times.

    Docker emits nanosecond precision (nine fractional digits), which
    ``datetime.fromisoformat`` rejects, so the fraction is truncated to
    microseconds. Sub-microsecond precision is irrelevant here: the timestamps
    are only ever compared to each other, and container starts are seconds apart.
    """
    if not value or value.startswith(_ZERO_TIME_PREFIX):
        return None

    normalised = value.removesuffix("Z")
    if "." in normalised:
        whole, _, fraction = normalised.partition(".")
        normalised = f"{whole}.{fraction[:6]}"

    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """The subset of a container's inspect payload that Tetherd reasons about."""

    id: str
    name: str
    running: bool
    started_at: datetime | None
    network_mode: str
    sandbox_key: str
    labels: Mapping[str, str]
    payload: Mapping[str, Any]

    @classmethod
    def from_inspect(cls, payload: Mapping[str, Any]) -> Self:
        state = payload.get("State") or {}
        host_config = payload.get("HostConfig") or {}
        config = payload.get("Config") or {}
        network_settings = payload.get("NetworkSettings") or {}

        return cls(
            id=str(payload["Id"]),
            # Docker prefixes container names with a slash in inspect output.
            name=str(payload.get("Name", "")).lstrip("/"),
            running=bool(state.get("Running", False)),
            started_at=parse_docker_timestamp(state.get("StartedAt")),
            network_mode=str(host_config.get("NetworkMode", "")),
            sandbox_key=str(network_settings.get("SandboxKey", "")),
            labels=dict(config.get("Labels") or {}),
            payload=payload,
        )

    @property
    def provider_ref(self) -> str | None:
        """The reference this container borrows its network from, if any.

        The daemon normalises ``container:<name>`` to ``container:<full-id>`` at
        create time, so in practice this is a container ID. A name is still
        tolerated because that normalisation has only been verified on recent
        daemons; see docs/design-notes.md.
        """
        if not self.network_mode.startswith(CONTAINER_NETWORK_PREFIX):
            return None
        ref = self.network_mode.removeprefix(CONTAINER_NETWORK_PREFIX)
        return ref or None

    @property
    def image(self) -> str:
        config = self.payload.get("Config") or {}
        return str(config.get("Image", ""))


def reference_matches(ref: str, container: ContainerInfo) -> bool:
    """Whether a network-mode reference identifies this container.

    Normally an exact full-ID match, since the daemon normalises references at
    create time. A name or an abbreviated ID is also accepted, because that
    normalisation has only been verified on recent daemons.

    An abbreviation must be a *prefix* of the ID, never merely contained in it,
    and a name must match in full. Substring matching is the bug class behind
    upstream issues #62 and #77, where 'radarr' matched 'radarr-4k'.
    """
    if not ref:
        return False
    if ref == container.id or ref == container.name:
        return True
    return len(ref) >= 12 and container.id.startswith(ref)


class Verdict(StrEnum):
    """The assessed network health of a dependent container."""

    HEALTHY = "healthy"
    """Sharing the provider's current namespace; nothing to do."""

    STALE_NAMESPACE = "stale_namespace"
    """The provider restarted after this container, so its namespace is gone.

    Repairable with a restart. Invisible to any check that only compares
    container IDs, because the reference is still correct and still live.
    """

    DEAD_PROVIDER_REF = "dead_provider_ref"
    """Points at a container ID that no longer exists, so it cannot start.

    Requires a rebuild from a snapshot.
    """

    PROVIDER_DOWN = "provider_down"
    """The provider itself is not running, so repair would fail. Wait."""

    NEVER_STARTED = "never_started"
    """Created but never started; nothing to infer from timestamps yet."""


@dataclass(frozen=True, slots=True)
class Assessment:
    """A verdict plus the evidence behind it.

    The reason is user-facing: it is what ``tetherd doctor`` prints, and the
    inability to answer "why did nothing happen?" is the single biggest source
    of support burden on the project this replaces.
    """

    container: ContainerInfo
    verdict: Verdict
    reason: str

    @property
    def needs_action(self) -> bool:
        return self.verdict in (Verdict.STALE_NAMESPACE, Verdict.DEAD_PROVIDER_REF)
