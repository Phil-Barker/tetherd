"""Turn a captured inspect payload back into a container-create request.

Two things make this safe. First, the create body is assembled from the
container's own recorded configuration, so a rebuild reproduces what was
actually running rather than an approximation derived from a template file.
Second, fields that cannot coexist with a borrowed network namespace are removed
and reported, so a rebuild cannot fail the way it does in upstream issues #80,
#69 and #65 — where a container is destroyed and then rejected on creation
because its template still carries port mappings.

The forbidden field lists are grounded in tests against a real daemon; see
docs/design-notes.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import CONTAINER_NETWORK_PREFIX

_SHARED_NAMESPACE = "belongs to the container that owns the network namespace"
_PORTS = "ports must be published on the container that owns the network namespace"

# Rejected outright by the daemon when combined with a container: network mode.
CONFIG_FIELDS_REJECTED = {
    "Hostname": _SHARED_NAMESPACE,
    "ExposedPorts": _PORTS,
}
HOST_CONFIG_FIELDS_REJECTED = {
    "PortBindings": _PORTS,
    "PublishAllPorts": _PORTS,
    "Dns": _SHARED_NAMESPACE,
    "ExtraHosts": _SHARED_NAMESPACE,
}

# Accepted by Docker 29.6.1 but documented as unsupported, so enforcement may
# differ on the older daemons Unraid ships. Meaningless in a borrowed namespace
# either way, so they are dropped rather than gambled on.
CONFIG_FIELDS_INERT = {
    "Domainname": _SHARED_NAMESPACE,
    "MacAddress": _SHARED_NAMESPACE,
}
HOST_CONFIG_FIELDS_INERT = {
    "DnsSearch": _SHARED_NAMESPACE,
    "DnsOptions": _SHARED_NAMESPACE,
    "MacAddress": _SHARED_NAMESPACE,
}

FORBIDDEN_CONFIG_FIELDS = {**CONFIG_FIELDS_REJECTED, **CONFIG_FIELDS_INERT}
FORBIDDEN_HOST_CONFIG_FIELDS = {**HOST_CONFIG_FIELDS_REJECTED, **HOST_CONFIG_FIELDS_INERT}

# Reported by inspect but not part of a create request.
_NON_CREATE_CONFIG_FIELDS = frozenset({"Hostname", "ArgsEscaped", "OnBuild"})


@dataclass(frozen=True, slots=True)
class StrippedField:
    """A field removed from the create body, and why."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path} ({self.reason})"


@dataclass(frozen=True, slots=True)
class CreateRequest:
    """A ready-to-post container-create request."""

    name: str
    body: Mapping[str, Any]
    stripped: tuple[StrippedField, ...]

    @property
    def image(self) -> str:
        return str(self.body.get("Image", ""))


class PayloadError(ValueError):
    """The captured configuration is not usable for a rebuild."""


def build_create_request(
    payload: Mapping[str, Any],
    *,
    provider_id: str,
    name: str | None = None,
) -> CreateRequest:
    """Assemble a create request from a container's inspect payload.

    ``provider_id`` is the provider's *current* ID. Rewriting the network mode
    to it is the whole point of the rebuild: the recorded value points at the
    provider that existed when the snapshot was taken, which by definition no
    longer does.
    """
    config = dict(payload.get("Config") or {})
    host_config = dict(payload.get("HostConfig") or {})

    container_name = name or str(payload.get("Name", "")).lstrip("/")
    if not container_name:
        raise PayloadError("captured configuration has no container name")
    if not config.get("Image"):
        raise PayloadError(f"captured configuration for {container_name} has no image")
    if not provider_id:
        raise PayloadError(f"no current provider ID to attach {container_name} to")

    stripped: list[StrippedField] = []

    for field, reason in FORBIDDEN_CONFIG_FIELDS.items():
        if _has_meaningful_value(config, field):
            stripped.append(StrippedField(f"Config.{field}", reason))
        config.pop(field, None)

    for field, reason in FORBIDDEN_HOST_CONFIG_FIELDS.items():
        if _has_meaningful_value(host_config, field):
            stripped.append(StrippedField(f"HostConfig.{field}", reason))
        host_config.pop(field, None)

    for field in _NON_CREATE_CONFIG_FIELDS:
        config.pop(field, None)

    host_config["NetworkMode"] = f"{CONTAINER_NETWORK_PREFIX}{provider_id}"

    body: dict[str, Any] = {**config, "HostConfig": host_config}
    # A borrowed namespace cannot also have endpoint configuration of its own,
    # and a dependent's inspect payload never carries any.
    body.pop("NetworkingConfig", None)

    return CreateRequest(name=container_name, body=body, stripped=tuple(stripped))


def _has_meaningful_value(source: Mapping[str, Any], field: str) -> bool:
    """Whether a field was actually set, as opposed to present and empty.

    Inspect reports absent settings as empty strings, lists or objects. Only a
    real value is worth reporting to the user as removed, or their logs would
    fill with notices about settings they never made.
    """
    value = source.get(field)
    if value is None or value is False:
        return False
    return bool(value)
