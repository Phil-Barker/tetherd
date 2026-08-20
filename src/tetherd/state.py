"""Remember what the provider used to be.

A container orphaned by a provider recreation points at an ID that no longer
resolves to anything. Nothing about it says which provider it *meant*, so the only
way to recognise it as ours is to remember the IDs the provider has held.

Keeping this on disk is what makes the recognition survive Tetherd restarting. The
awkward case is the one where it matters most: the host reboots, the VPN container
comes up with a fresh ID, Tetherd starts alongside it, and every dependent is
pointing at an ID that was only ever known to the previous process. Without a
persisted history those containers look like they belong to somebody else's
provider, and the safe response to that is to leave them alone — which would mean
never repairing them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .storage import read_json, write_json_atomically

SCHEMA_VERSION: Final = 1

#: How many past provider IDs to keep. Only the most recent few can plausibly
#: still be referenced by a container that has not been repaired yet, and an
#: unbounded list would grow for the lifetime of the installation.
DEFAULT_HISTORY: Final = 10


@dataclass(frozen=True, slots=True)
class ProviderState:
    """The provider IDs seen, most recent first."""

    ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def current(self) -> str | None:
        return self.ids[0] if self.ids else None

    def remembering(self, container_id: str, history: int = DEFAULT_HISTORY) -> ProviderState:
        """This state plus ``container_id``, moved to the front if already known."""
        if not container_id:
            return self
        remaining = tuple(known for known in self.ids if known != container_id)
        return ProviderState(ids=(container_id, *remaining)[:history])


class ProviderStateStore:
    """Reads and writes the provider's ID history."""

    def __init__(self, path: Path, history: int = DEFAULT_HISTORY) -> None:
        self._path = path
        self._history = history

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ProviderState:
        """The recorded state, or an empty one if it is absent or unreadable.

        Unreadable state is treated as absent rather than fatal. The cost is one
        pass in which orphans are not recognised; the cost of raising would be
        Tetherd refusing to start at all.
        """
        payload = read_json(self._path)
        if not isinstance(payload, dict):
            return ProviderState()

        raw: Any = payload.get("provider_ids")
        if not isinstance(raw, list):
            return ProviderState()
        return ProviderState(ids=tuple(str(item) for item in raw if item))

    def remember(self, container_id: str) -> ProviderState:
        """Record the provider's current ID, returning the updated state."""
        updated = self.load().remembering(container_id, self._history)
        write_json_atomically(
            self._path,
            {"schema_version": SCHEMA_VERSION, "provider_ids": list(updated.ids)},
        )
        return updated
