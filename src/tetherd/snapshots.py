"""A durable record of how each managed container was configured.

This is what makes recreation possible without consulting Unraid's templates. A
container's own inspect payload is captured while it is healthy and kept on disk,
so if the container later has to be rebuilt, it is rebuilt from what was actually
running rather than from a template file that may be stale, may describe a
different container entirely, or may not exist.

Two properties matter more than they look.

Snapshots are written only when the configuration has meaningfully changed. A
reconcile loop runs every few minutes, so writing unconditionally would fill the
retention window with identical copies within the hour and age out the last
known-good configuration — precisely the thing worth keeping. Volatile fields are
therefore excluded from the comparison, including the network mode, which changes
every time the provider is replaced and is rewritten on rebuild anyway.

Reads never fail because of one bad file. A truncated snapshot, from a host that
lost power mid-write, must not make a container unrecoverable when four earlier
snapshots are sitting next to it. Writes are atomic to keep that rare, and
unreadable files are skipped rather than raised.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .models import CONTAINER_NETWORK_PREFIX

#: Bumped only if the envelope's shape changes incompatibly. Older snapshots are
#: read on a best-effort basis rather than discarded, because a snapshot Tetherd
#: cannot read is a container Tetherd cannot rebuild.
SCHEMA_VERSION: Final = 1

_FILENAME_TIME_FORMAT: Final = "%Y%m%dT%H%M%S"
_SNAPSHOT_SUFFIX: Final = ".json"

#: Excluded from the change comparison. These vary run to run without the
#: configuration having changed, and NetworkMode is rewritten on every rebuild.
_VOLATILE_HOST_CONFIG_FIELDS: Final = frozenset({"NetworkMode"})


class SnapshotError(RuntimeError):
    """A snapshot could not be written."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One captured configuration, as read back from disk."""

    container: str
    container_id: str
    provider_id: str | None
    captured_at: datetime
    digest: str
    payload: Mapping[str, Any]
    path: Path

    #: Whether this capture wrote a new file, as opposed to matching what was
    #: already recorded. Useful for logging; not part of the snapshot's identity.
    is_new: bool = field(default=False, compare=False)

    @property
    def image(self) -> str:
        return str((self.payload.get("Config") or {}).get("Image", ""))

    @property
    def age(self) -> str:
        """Human-readable age, for status output and notifications."""
        seconds = int((datetime.now(UTC) - self.captured_at).total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"


class SnapshotStore:
    """Per-container snapshot history on disk, newest first, with retention."""

    def __init__(self, directory: Path, retention: int = 5) -> None:
        if retention < 1:
            raise ValueError("retention must keep at least one snapshot")
        self._directory = directory
        self._retention = retention

    @property
    def directory(self) -> Path:
        return self._directory

    def capture(self, payload: Mapping[str, Any]) -> Snapshot:
        """Record a container's configuration, writing only if it has changed.

        Returns the snapshot representing this configuration, whose ``is_new``
        tells the caller whether anything was written. The returned snapshot
        always reflects the payload just supplied, so callers can rebuild from it
        immediately.
        """
        container = str(payload.get("Name", "")).lstrip("/")
        if not container:
            raise SnapshotError("cannot snapshot a container with no name")

        digest = _digest_of(payload)
        existing = self.latest(container)
        if existing is not None and existing.digest == digest:
            return existing

        snapshot = Snapshot(
            container=container,
            container_id=str(payload.get("Id", "")),
            provider_id=_provider_id_from(payload),
            captured_at=datetime.now(UTC),
            digest=digest,
            payload=payload,
            path=self._next_path(container),
            is_new=True,
        )
        self._write(snapshot)
        self._prune(container)
        return snapshot

    def latest(self, container: str) -> Snapshot | None:
        """The most recent readable snapshot, or None if there are none."""
        return next(iter(self.history(container)), None)

    def history(self, container: str) -> list[Snapshot]:
        """Every readable snapshot for a container, newest first."""
        return list(self._read_all(container))

    def containers(self) -> list[str]:
        """Every container with at least one snapshot on disk."""
        if not self._directory.is_dir():
            return []
        return sorted(
            child.name
            for child in self._directory.iterdir()
            if child.is_dir() and any(child.iterdir())
        )

    def forget(self, container: str) -> int:
        """Discard a container's history, returning how many files were removed.

        Used when a container is deliberately removed or excluded, so its
        configuration is not replayed onto a host that no longer wants it.
        """
        directory = self._container_dir(container)
        if not directory.is_dir():
            return 0

        removed = 0
        for path in directory.glob(f"*{_SNAPSHOT_SUFFIX}"):
            path.unlink(missing_ok=True)
            removed += 1
        # Leaving an empty directory would make `containers` report a container
        # that has just been forgotten.
        if not any(directory.iterdir()):
            directory.rmdir()
        return removed

    # -- internals ---------------------------------------------------------

    def _container_dir(self, container: str) -> Path:
        return self._directory / _safe_name(container)

    def _paths(self, container: str) -> list[Path]:
        """Snapshot files for a container, newest first."""
        directory = self._container_dir(container)
        if not directory.is_dir():
            return []
        # Ordered by the sequence number in the filename rather than by the
        # timestamp beside it, so history stays correct on a host whose clock
        # steps backwards - a NAS with a dead RTC and no internet boots with one.
        return sorted(directory.glob(f"*{_SNAPSHOT_SUFFIX}"), key=_sequence_of, reverse=True)

    def _read_all(self, container: str) -> Iterator[Snapshot]:
        for path in self._paths(container):
            snapshot = _read(path)
            if snapshot is not None:
                yield snapshot

    def _next_path(self, container: str) -> Path:
        """The next filename in sequence for a container.

        Numbering continues from the highest sequence still on disk rather than
        restarting after retention prunes. Reusing a freed low number would make
        the newest snapshot sort as the oldest, and retention would then delete
        it on the very next capture.

        The timestamp in the name is for humans reading the directory; ordering
        never depends on it.
        """
        directory = self._container_dir(container)
        existing = self._paths(container)
        sequence = (_sequence_of(existing[0]) + 1) if existing else 0
        stamp = datetime.now(UTC).strftime(_FILENAME_TIME_FORMAT)
        return directory / f"{sequence:06d}-{stamp}{_SNAPSHOT_SUFFIX}"

    def _write(self, snapshot: Snapshot) -> None:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "container": snapshot.container,
            "container_id": snapshot.container_id,
            "provider_id": snapshot.provider_id,
            "captured_at": snapshot.captured_at.isoformat(),
            "digest": snapshot.digest,
            "inspect": snapshot.payload,
        }

        directory = snapshot.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _write_atomically(snapshot.path, json.dumps(envelope, indent=2, sort_keys=True))
        except OSError as exc:
            raise SnapshotError(
                f"cannot write a snapshot for {snapshot.container} to {directory}: {exc}. "
                "Tetherd needs a writable state directory to rebuild containers later."
            ) from exc

    def _prune(self, container: str) -> None:
        for path in self._paths(container)[self._retention :]:
            path.unlink(missing_ok=True)


def _sequence_of(path: Path) -> int:
    """The sequence number leading a snapshot filename, or -1 if absent.

    A file that does not follow the naming scheme sorts oldest, so a stray file
    dropped into the directory can never displace a real snapshot.
    """
    leading = path.name.split("-", 1)[0]
    return int(leading) if leading.isdigit() else -1


def _write_atomically(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory, then rename.

    A half-written snapshot is worse than no snapshot, because it looks like a
    recoverable configuration until it is read.
    """
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".tetherd-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read(path: Path) -> Snapshot | None:
    """Read one snapshot, or None if it is unusable.

    Deliberately forgiving: a single corrupt file must not hide the good
    snapshots stored alongside it.
    """
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict):
        return None

    payload = envelope.get("inspect")
    if not isinstance(payload, dict) or not payload:
        return None

    container = str(envelope.get("container") or "").strip()
    if not container:
        return None

    captured_at = _parse_isoformat(envelope.get("captured_at"))
    if captured_at is None:
        # Fall back to the file's own mtime rather than dropping an otherwise
        # complete snapshot over a missing field.
        captured_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    return Snapshot(
        container=container,
        container_id=str(envelope.get("container_id") or ""),
        provider_id=envelope.get("provider_id") or None,
        captured_at=captured_at,
        digest=str(envelope.get("digest") or _digest_of(payload)),
        payload=payload,
        path=path,
    )


def _parse_isoformat(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _digest_of(payload: Mapping[str, Any]) -> str:
    """Fingerprint the parts of a payload that a rebuild would reproduce.

    Only Config and HostConfig are considered, because those are what the create
    request is assembled from. Everything else inspect returns is either runtime
    state or daemon-assigned, and would make every capture look like a change.
    """
    host_config = {
        key: value
        for key, value in (payload.get("HostConfig") or {}).items()
        if key not in _VOLATILE_HOST_CONFIG_FIELDS
    }
    material = json.dumps(
        {"Config": payload.get("Config") or {}, "HostConfig": host_config},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _provider_id_from(payload: Mapping[str, Any]) -> str | None:
    network_mode = str((payload.get("HostConfig") or {}).get("NetworkMode", ""))
    if not network_mode.startswith(CONTAINER_NETWORK_PREFIX):
        return None
    return network_mode[len(CONTAINER_NETWORK_PREFIX) :] or None


def _safe_name(container: str) -> str:
    """A filesystem-safe directory name for a container.

    Docker already restricts names to characters that are safe here, so this only
    guards against a malformed payload reaching the store and writing outside its
    own directory.
    """
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in container)
    return cleaned.lstrip(".") or "_unnamed"
