"""Unraid integration: keeping the Docker tab happy, and auditing its templates.

Unraid decides how to present a container from four labels read directly off it.
Losing them is what makes a container show as "3rd Party" in the Docker tab,
without an icon, WebUI link or console. Because Tetherd replays a container's own
recorded configuration, labels survive a rebuild for free — but that is a
property worth asserting rather than assuming, so ``label_drift`` exists to prove
it and to fail loudly if it ever stops being true.

This module also audits Unraid's template XML, deliberately without ever
rebuilding from it. The distinction matters:

- As a *source of truth for rebuilding*, the templates are wrong. They are
  located by guessing a filename from a container name, they lag behind the
  live container, they do not carry the labels above, and their format has
  changed under the predecessor project at least once (issue #59).
- As a thing to *audit*, they are valuable. Unraid genuinely does treat them as
  authoritative: anything that recreates a container through the Docker tab uses
  the XML. So a template that cannot produce a working container is a real
  problem, just not one Tetherd should solve by rebuilding differently and
  saying nothing.

Templates are matched by the ``<Name>`` element inside the file, never by
filename. Filename guessing is the bug behind upstream issues #77 and #75.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

LABEL_MANAGED = "net.unraid.docker.managed"
LABEL_ICON = "net.unraid.docker.icon"
LABEL_WEBUI = "net.unraid.docker.webui"
LABEL_SHELL = "net.unraid.docker.shell"

#: The labels Unraid reads off a container to present it in the Docker tab.
INTEGRATION_LABELS = (LABEL_MANAGED, LABEL_ICON, LABEL_WEBUI, LABEL_SHELL)

#: Set by some Unraid versions to record which template produced a container.
#: Not part of the documented set, so treated as optional.
LABEL_TEMPLATE = "net.unraid.docker.template"

#: Present on an Unraid host, and nowhere else.
UNRAID_MARKER = Path("/usr/local/emhttp")

#: Unraid's own notifier, used when running on an Unraid host.
UNRAID_NOTIFY = Path("/usr/local/emhttp/webGui/scripts/notify")

DEFAULT_TEMPLATE_DIR = Path("/config/docker-templates")


def is_unraid_host(marker: Path = UNRAID_MARKER) -> bool:
    """Whether Tetherd is running on an Unraid host.

    Unraid-specific behaviour is enabled by detection rather than
    configuration, so the same image works everywhere.
    """
    return marker.is_dir()


@dataclass(frozen=True, slots=True)
class LabelChange:
    label: str
    before: str | None
    after: str | None

    def __str__(self) -> str:
        if self.after is None:
            return f"{self.label} was lost (was {self.before!r})"
        return f"{self.label} changed from {self.before!r} to {self.after!r}"


def label_drift(
    before: Mapping[str, str],
    after: Mapping[str, str],
    labels: Iterable[str] = INTEGRATION_LABELS,
) -> tuple[LabelChange, ...]:
    """Integration labels lost or altered across a rebuild.

    Anything reported here would degrade how Unraid presents the container, so
    it is treated as a defect rather than a warning.
    """
    changes = []
    for label in labels:
        was = before.get(label)
        now = after.get(label)
        if was is not None and was != now:
            changes.append(LabelChange(label, was, now))
    return tuple(changes)


def describe_integration(labels: Mapping[str, str]) -> str:
    """How Unraid will present this container, for `tetherd doctor`."""
    managed = labels.get(LABEL_MANAGED)
    if not managed:
        return (
            "Unraid will show this as 3rd Party: it has no "
            f"{LABEL_MANAGED} label. Tetherd preserves labels across a rebuild, "
            "so this container was already unlabelled before Tetherd saw it."
        )

    extras = [
        name
        for label, name in (
            (LABEL_ICON, "icon"),
            (LABEL_WEBUI, "WebUI link"),
            (LABEL_SHELL, "console shell"),
        )
        if not labels.get(label)
    ]
    if extras:
        return f"managed by {managed}, but Unraid has no {', '.join(extras)} for it"
    return f"managed by {managed}, with icon, WebUI link and console configured"


@dataclass(frozen=True, slots=True)
class TemplateAudit:
    """What Unraid's own template would do if it recreated this container."""

    container: str
    path: Path | None
    problems: list[str] = field(default_factory=list)

    @property
    def template_missing(self) -> bool:
        return self.path is None

    @property
    def is_healthy(self) -> bool:
        return self.path is not None and not self.problems


def audit_templates(
    container_names: Iterable[str],
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, TemplateAudit]:
    """Check each container's Unraid template for problems, read-only.

    Tetherd's own rebuilds are unaffected by any of this, because they replay
    the live container. The point is that *Unraid's* rebuilds are not: if the
    user hits Apply in the Docker tab, dockerMan uses the XML, and a template
    that pairs port mappings with a borrowed network is rejected by the daemon.
    That is upstream issues #80, #69 and #65 - and the reason those users were
    left with a destroyed container was that nothing warned them first.
    """
    names = list(container_names)
    by_name = _index_templates_by_declared_name(template_dir)

    audits: dict[str, TemplateAudit] = {}
    for name in names:
        path = by_name.get(name)
        if path is None:
            audits[name] = TemplateAudit(
                container=name,
                path=None,
                problems=[
                    f"no template in {template_dir} declares <Name>{name}</Name>, so "
                    "Unraid cannot recreate this container from the Docker tab"
                ],
            )
            continue
        audits[name] = TemplateAudit(container=name, path=path, problems=_problems_in(path))
    return audits


def _index_templates_by_declared_name(template_dir: Path) -> dict[str, Path]:
    """Map declared container name to template path.

    Keyed on the ``<Name>`` element rather than the filename. Deriving a
    container name from a filename is what made upstream pick
    ``my-nordvpn-qbittorrent.xml`` for a container called ``qbittorrent``.
    """
    if not template_dir.is_dir():
        return {}

    index: dict[str, Path] = {}
    for path in sorted(template_dir.glob("*.xml")):
        declared = _declared_name(path)
        # First match wins, and sorting makes that deterministic rather than
        # dependent on directory order.
        if declared and declared not in index:
            index[declared] = path
    return index


def _declared_name(path: Path) -> str | None:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return None
    if root.tag != "Container":
        return None
    name = root.findtext("Name")
    return name.strip() if name else None


def _problems_in(path: Path) -> list[str]:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        return [f"template {path.name} cannot be parsed: {exc}"]

    problems: list[str] = []
    network = (root.findtext("Network") or "").strip().lower()
    extra_params = (root.findtext("ExtraParams") or "").strip()
    borrows_network = "--net=container:" in extra_params or "--network=container:" in extra_params

    published = [
        config
        for config in root.findall("Config")
        if (config.get("Type") or "").strip().lower() == "port" and (config.text or "").strip()
    ]

    if (borrows_network or network.startswith("container:")) and published:
        ports = ", ".join(sorted((c.text or "").strip() for c in published))
        problems.append(
            f"template {path.name} publishes ports ({ports}) while routing through "
            "another container's network. Unraid will fail to recreate it with "
            '"conflicting options: port publishing and the container type network '
            'mode". Remove the port mappings from the template; they belong on the '
            "container that owns the network."
        )

    if borrows_network and network not in {"none", ""}:
        problems.append(
            f"template {path.name} sets Network Type to {network!r} while also "
            "passing --net=container: in Extra Parameters. Unraid records the "
            "network from Network Type, so the container may not end up borrowing "
            "the network at all. Set Network Type to None."
        )

    return problems
