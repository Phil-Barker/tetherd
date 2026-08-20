"""Work out which containers Tetherd is responsible for, and say why.

Discovery is automatic: any container borrowing the provider's network stack is
managed, with optional include, exclude and label filters on top. Every
container that is examined and then set aside records a reason, because
"nothing happened and I cannot tell why" is the dominant complaint about the
tool this replaces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .config import Settings
from .docker_api import DockerApi
from .models import ContainerInfo, reference_matches

ENABLE_LABEL = "tetherd.enable"


class SkipReason(StrEnum):
    IS_PROVIDER = "is_provider"
    OTHER_PROVIDER = "other_provider"
    ORPHANED = "orphaned"
    EXCLUDED = "excluded"
    NOT_INCLUDED = "not_included"
    MISSING_LABEL = "missing_label"


@dataclass(frozen=True, slots=True)
class Skipped:
    container: ContainerInfo
    reason: SkipReason
    detail: str


@dataclass(frozen=True, slots=True)
class Discovery:
    """What a discovery pass found."""

    provider: ContainerInfo | None
    managed: list[ContainerInfo] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    adopted: list[ContainerInfo] = field(default_factory=list)
    """Managed containers claimed only because their reference points at nothing.

    Worth reporting separately. Adopting them is almost always right — a container
    referencing an ID that no longer exists cannot start, and no other provider can
    claim it either — but on a host with two providers the guess could be wrong, so
    it should never happen silently.
    """

    unresolved_includes: list[str] = field(default_factory=list)
    """Names the user asked to manage that were not found borrowing the network.

    Almost always a wiring mistake: on Unraid, setting the network via Extra
    Parameters without also setting Network Type to None leaves the container's
    NetworkMode pointing somewhere else entirely, so it is never detected. That
    is upstream issue #57, whose accepted workaround was to toggle the network
    type off and back on. Naming the container explicitly turns a silent
    non-event into a reportable problem.
    """

    @property
    def provider_missing(self) -> bool:
        return self.provider is None


def discover(
    api: DockerApi,
    settings: Settings,
    known_provider_ids: Iterable[str] = (),
) -> Discovery:
    """Find the provider and the dependents Tetherd should manage.

    ``known_provider_ids`` are IDs the provider has had previously. They matter
    because a container orphaned by a provider recreation points at an ID that
    no longer resolves, so the only way to recognise it as ours is to remember
    what the provider used to be called.
    """
    provider_payload = api.inspect(settings.provider)
    provider = ContainerInfo.from_inspect(provider_payload) if provider_payload else None

    historical = set(known_provider_ids)
    if provider is not None:
        historical.add(provider.id)

    managed: list[ContainerInfo] = []
    skipped: list[Skipped] = []
    adopted: list[ContainerInfo] = []

    for container_id in api.list_network_borrowers():
        payload = api.inspect(container_id)
        if payload is None:
            # Removed between listing and inspecting; it will be picked up on a
            # later pass if it comes back.
            continue

        container = ContainerInfo.from_inspect(payload)
        decision, was_adopted = _classify(api, container, provider, historical, settings)
        if decision is None:
            managed.append(container)
            if was_adopted:
                adopted.append(container)
        else:
            skipped.append(decision)

    managed.sort(key=lambda c: c.name)
    skipped.sort(key=lambda s: s.container.name)
    adopted.sort(key=lambda c: c.name)

    found = {c.name for c in managed} | {s.container.name for s in skipped}
    unresolved = [name for name in settings.include if name not in found]

    return Discovery(
        provider=provider,
        managed=managed,
        skipped=skipped,
        adopted=adopted,
        unresolved_includes=unresolved,
    )


def _classify(
    api: DockerApi,
    container: ContainerInfo,
    provider: ContainerInfo | None,
    historical_provider_ids: set[str],
    settings: Settings,
) -> tuple[Skipped | None, bool]:
    """Decide a container's fate.

    Returns the reason it was set aside, or None if it should be managed, along
    with whether managing it required adopting an orphan.
    """
    if provider is not None and container.id == provider.id:
        return Skipped(container, SkipReason.IS_PROVIDER, "this is the provider itself"), False

    ref = container.provider_ref
    if ref is None:  # pragma: no cover - the list filter already excludes these
        return (
            Skipped(
                container,
                SkipReason.OTHER_PROVIDER,
                "does not borrow another container's network",
            ),
            False,
        )

    adopted = False
    if not _belongs_to_us(ref, provider, historical_provider_ids):
        expected = settings.provider

        # A reference to a container that does not exist is an orphan. Nothing can
        # claim it and it cannot start, and this is precisely the state a host is
        # in when its provider was recreated before Tetherd was ever installed -
        # so there is no history to recognise it by.
        if api.exists(ref):
            return (
                Skipped(
                    container,
                    SkipReason.OTHER_PROVIDER,
                    f"borrows the network of {_short(ref)}, which is not {expected} "
                    f"or any container {expected} has previously been",
                ),
                False,
            )

        if not settings.adopt_orphans:
            return (
                Skipped(
                    container,
                    SkipReason.ORPHANED,
                    f"points at {_short(ref)}, which no longer exists, and "
                    "adopt_orphans is off so it is being left alone",
                ),
                False,
            )

        adopted = True

    if container.name in settings.exclude:
        return Skipped(container, SkipReason.EXCLUDED, "listed in exclude"), False

    if settings.include and container.name not in settings.include:
        return (
            Skipped(
                container,
                SkipReason.NOT_INCLUDED,
                "include is set and this container is not in it",
            ),
            False,
        )

    if settings.require_label and not _label_enabled(container.labels):
        return (
            Skipped(
                container,
                SkipReason.MISSING_LABEL,
                f"require_label is on and {ENABLE_LABEL}=true is not set",
            ),
            False,
        )

    return None, adopted


def _belongs_to_us(
    ref: str, provider: ContainerInfo | None, historical_provider_ids: set[str]
) -> bool:
    if provider is not None and reference_matches(ref, provider):
        return True
    # An orphan pointing at a dead ID: recognisable only from history. Compared
    # as a prefix so an abbreviated reference still matches a remembered ID.
    return any(known == ref or known.startswith(ref) for known in historical_provider_ids if ref)


def _label_enabled(labels: Mapping[str, str]) -> bool:
    return labels.get(ENABLE_LABEL, "").strip().lower() in {"true", "1", "yes"}


def _short(container_id: str) -> str:
    return container_id[:12] if len(container_id) > 12 else container_id
