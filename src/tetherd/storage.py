"""Durable files on disk, written so a power cut cannot leave a half-file.

Tetherd's state is the only thing that makes a container recoverable after its
provider has been replaced, so a truncated write is not a cosmetic problem. Every
write goes to a temporary file in the same directory, is flushed to the platform,
and is then renamed over the target, which is atomic within a filesystem.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    """State could not be written."""


def write_text_atomically(path: Path, text: str) -> None:
    """Replace ``path`` in one step, creating parents as needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace(path, text)
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc


def write_json_atomically(path: Path, payload: Any) -> None:
    """Serialise and replace ``path`` in one step, creating parents as needed."""
    write_text_atomically(path, json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path) -> Any | None:
    """Parse ``path``, or return None if it is absent or unusable.

    Deliberately forgiving. State that cannot be read is rebuilt from the live
    daemon on the next pass, which is a far better outcome than refusing to start.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _replace(path: Path, text: str) -> None:
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
