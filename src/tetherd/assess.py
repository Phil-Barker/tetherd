"""Decide whether a dependent container has lost the provider's network.

The two failure modes and the signals that distinguish them are documented in
docs/design-notes.md and verified by scripts/spike-netns.sh. In short:

- provider recreated  -> the dependent's reference points at a dead ID and it
  cannot start, so it must be rebuilt
- provider restarted  -> the reference is still correct and still live, but the
  namespace behind it was replaced; only timestamp ordering reveals this, and a
  restart repairs it

Nothing here reads ``EndpointID``, which is absent from a dependent's inspect
payload and is the reason the predecessor project's detection was unreliable.
"""

from __future__ import annotations

from .models import Assessment, ContainerInfo, Verdict, reference_matches


def assess(dependent: ContainerInfo, provider: ContainerInfo) -> Assessment:
    """Assess one dependent against the current state of its provider."""
    ref = dependent.provider_ref
    if ref is None:
        # Callers are expected to filter these out during discovery; treating it
        # as healthy keeps the function total rather than raising on bad input.
        return Assessment(
            container=dependent,
            verdict=Verdict.HEALTHY,
            reason="does not borrow another container's network",
        )

    if not reference_matches(ref, provider):
        return Assessment(
            container=dependent,
            verdict=Verdict.DEAD_PROVIDER_REF,
            reason=(
                f"points at container {_short(ref)}, but {provider.name} is now "
                f"{_short(provider.id)}; the provider was recreated, so this "
                f"container cannot start until it is rebuilt"
            ),
        )

    if not provider.running:
        return Assessment(
            container=dependent,
            verdict=Verdict.PROVIDER_DOWN,
            reason=f"provider {provider.name} is not running; waiting for it to come up",
        )

    if dependent.started_at is None:
        return Assessment(
            container=dependent,
            verdict=Verdict.NEVER_STARTED,
            reason="has never been started, so there is no namespace to have lost",
        )

    if provider.started_at is not None and provider.started_at > dependent.started_at:
        return Assessment(
            container=dependent,
            verdict=Verdict.STALE_NAMESPACE,
            reason=(
                f"provider {provider.name} started at "
                f"{_stamp(provider.started_at)}, after this container started at "
                f"{_stamp(dependent.started_at)}; it is holding a network "
                f"namespace that no longer exists"
            ),
        )

    return Assessment(
        container=dependent,
        verdict=Verdict.HEALTHY,
        reason=f"sharing the current namespace of {provider.name}",
    )


def _short(container_id: str) -> str:
    return container_id[:12] if len(container_id) > 12 else container_id


def _stamp(value: object) -> str:
    return str(value).replace("+00:00", "Z")
