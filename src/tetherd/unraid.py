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
filename. Filename guessing is the bug behind upstream issues #77 and #75, and
it is made worse by Unraid keeping the XML for apps you have uninstalled so they
can be reinstalled from Previous Apps. The directory therefore records everything
ever installed, so a substring match can land on an abandoned template describing
network wiring that was correct years ago.

For the same reason the audit is driven by the containers that exist and never by
the contents of the template directory: an orphaned template is history the user
chose to keep, not a misconfiguration to report.
"""

from __future__ import annotations

import re
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

#: Deliberately not used. The accepted community fix for upstream issue #77 was
#: to read a container's template path from this label rather than guessing a
#: filename, but a survey of a live Unraid 7.3.2 server found it on no container
#: at all: only the four labels above are in use. Templates are identified by
#: their <Name> element instead, which requires no label. Kept named here so the
#: reasoning is discoverable rather than rediscovered.
UNUSED_LABEL_TEMPLATE = "net.unraid.docker.template"

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
    declared_provider: str | None = None
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
    provider: str | None = None,
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
        declared, problems = _inspect_template(path, provider)
        audits[name] = TemplateAudit(
            container=name, path=path, declared_provider=declared, problems=problems
        )
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


def _inspect_template(path: Path, provider: str | None) -> tuple[str | None, list[str]]:
    """The provider a template declares, and anything wrong with the template."""
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        return None, [f"template {path.name} cannot be parsed: {exc}"]

    network = (root.findtext("Network") or "").strip()
    extra_params = (root.findtext("ExtraParams") or "").strip()

    # A borrowed network appears either in the Network element, which is what
    # current Unraid writes, or as a flag in Extra Parameters, the older style
    # the upstream README still documents as its "alternate steps".
    from_extra_params = _provider_from_extra_params(extra_params)
    declared = _provider_from_network_element(network) or from_extra_params
    if declared is None:
        return None, []

    problems: list[str] = []

    published = sorted(
        (config.text or "").strip()
        for config in root.findall("Config")
        if (config.get("Type") or "").strip().lower() == "port" and (config.text or "").strip()
    )
    if published:
        problems.append(
            f"template {path.name} publishes ports ({', '.join(published)}) while "
            "routing through another container's network. Unraid will fail to "
            'recreate it with "conflicting options: port publishing and the '
            'container type network mode". Remove the port mappings from the '
            "template; they belong on the container that owns the network."
        )

    # Issue #57: the network is requested in Extra Parameters while Network Type
    # says something else, so Unraid records the wrong network and the container
    # is never detected as a dependent.
    if from_extra_params is not None and network.lower() not in {"none", ""}:
        problems.append(
            f"template {path.name} sets Network Type to {network!r} while also "
            "requesting a container network in Extra Parameters. Unraid records "
            "the network from Network Type, so this container may not end up "
            "borrowing the network at all. Set Network Type to None."
        )

    if provider and declared != provider:
        problems.append(
            f"template {path.name} routes through {declared!r}, but Tetherd is "
            f"configured for {provider!r}. Recreating this container from the "
            "Docker tab would attach it to the wrong container."
        )

    return declared, problems


def _provider_from_network_element(network: str) -> str | None:
    if not network.lower().startswith("container:"):
        return None
    return network.partition(":")[2].strip() or None


#: Docker's CLI accepts --net and --network, with the value joined by = or by a
#: space, and Unraid stores Extra Parameters verbatim, so all four spellings turn
#: up along with optional quoting around the value.
_EXTRA_PARAMS_NETWORK = re.compile(
    r"--net(?:work)?[=\s]+[\"']?container:(?P<provider>[^\s\"']+)",
    re.IGNORECASE,
)


def _provider_from_extra_params(extra_params: str) -> str | None:
    match = _EXTRA_PARAMS_NETWORK.search(extra_params)
    return match.group("provider") if match else None
