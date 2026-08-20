"""Translate a Rebuild-DNDC environment into Tetherd settings.

The original's Unraid template is a set of environment variables with names that
do not survive a clean-room rewrite. This exists so a user can paste their old
container config and get something they can drop into Tetherd, with the
mismatches called out rather than silently guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

KNOWN_RDNDC_KEYS = frozenset(
    {
        "mastercontname",
        "mastercontconcheck",
        "ping_ip",
        "ping_ip_alt",
        "ping_count",
        "sleep_secs",
        "cron",
        "run_startup",
        "discord_notifications",
        "discord_url",
        "gotify_notifications",
        "gotify_url",
        "cont_list",
        "save_no_mcontids",
        "tz",
        "host_os",
    }
)


_TRUTHY = frozenset({"yes", "true", "1", "on"})


@dataclass(frozen=True, slots=True)
class Translation:
    """A Rebuild-DNDC environment, expressed as Tetherd configuration."""

    settings: dict[str, Any]
    notes: tuple[str, ...] = field(default_factory=tuple)
    unmapped: tuple[str, ...] = field(default_factory=tuple)

    def as_yaml(self) -> str:
        body = yaml.safe_dump(self.settings, sort_keys=False, default_flow_style=False)
        if not self.notes:
            return body
        commented = "\n".join(f"# {note}" for note in self.notes)
        return f"{commented}\n{body}"

    def as_env(self) -> str:
        lines: list[str] = []
        for note in self.notes:
            lines.append(f"# {note}")
        lines.extend(_env_lines(self.settings, prefix="TETHERD"))
        return "\n".join(lines) + "\n"


def collect_rdndc(environ: Mapping[str, str]) -> dict[str, str]:
    """Pull Rebuild-DNDC keys out of a process environment.

    Anything else (PATH, HOME, TETHERD_*) is ignored, so this is safe to run
    against os.environ. Keys that look like the original's port-forwarding
    options are kept so they can be reported as unmapped.
    """
    collected: dict[str, str] = {}
    for key, value in environ.items():
        lowered = key.lower()
        if lowered in KNOWN_RDNDC_KEYS or lowered.startswith("rutorrent_"):
            collected[lowered] = value
    return collected


def translate_rdndc(environ: Mapping[str, str]) -> Translation:
    """Map Rebuild-DNDC environment variables onto a Tetherd settings dict.

    Unknown keys are reported rather than dropped on the floor, because a user
    who set ``rutorrent_pf`` deserves to be told Tetherd does not do port
    forwarding, not to discover it by the torrent client staying closed.
    """
    env = {key.strip().lower(): value.strip() for key, value in environ.items() if value.strip()}
    settings: dict[str, Any] = {}
    notes: list[str] = []
    unmapped: list[str] = []

    if name := env.get("mastercontname"):
        settings["provider"] = name
    else:
        notes.append(
            "mastercontname was not set, so provider is missing. Tetherd will not "
            "start without TETHERD_PROVIDER."
        )

    probe: dict[str, Any] = {}
    if "mastercontconcheck" in env:
        probe["enabled"] = _truthy(env["mastercontconcheck"])

    targets = [ip for ip in (env.get("ping_ip"), env.get("ping_ip_alt")) if ip]
    if targets:
        probe["targets"] = targets

    if "sleep_secs" in env:
        probe["settle_seconds"] = _as_number(env["sleep_secs"], 10.0)
    if probe:
        settings["probe"] = probe

    if "cron" in env:
        interval = _cron_to_seconds(env["cron"])
        if interval is None:
            notes.append(
                f"cron={env['cron']!r} could not be parsed as a simple interval; "
                "leaving Tetherd's default of 300 seconds."
            )
        else:
            settings["reconcile_interval_seconds"] = interval

    if "cont_list" in env:
        names = env["cont_list"].replace(",", " ").split()
        if names:
            settings["include"] = names
            notes.append(
                "cont_list mapped to include, which *restricts* Tetherd to those "
                "containers. Rebuild-DNDC used it only for the manual rebuildm "
                "command. Delete include if you want every dependent managed."
            )

    urls: list[str] = []
    if env.get("discord_url"):
        urls.append(env["discord_url"])
        if env.get("discord_notifications") and not _truthy(env["discord_notifications"]):
            notes.append(
                "discord_url is set but discord_notifications was not yes; Tetherd "
                "has no separate enable flag, so the URL is included anyway."
            )
    elif _truthy(env.get("discord_notifications", "")):
        notes.append("discord_notifications was yes but discord_url was empty, so nothing to map.")

    if env.get("gotify_url"):
        urls.append(env["gotify_url"])
        if env.get("gotify_notifications") and not _truthy(env["gotify_notifications"]):
            notes.append(
                "gotify_url is set but gotify_notifications was not yes; Tetherd "
                "has no separate enable flag, so the URL is included anyway."
            )
    elif _truthy(env.get("gotify_notifications", "")):
        notes.append("gotify_notifications was yes but gotify_url was empty, so nothing to map.")
    if urls:
        settings.setdefault("notify", {})["urls"] = urls

    if "ping_count" in env:
        notes.append(
            "ping_count is not mapped. In Rebuild-DNDC it was the packet count of "
            "a single ping, not a retry count, despite the docs. Tetherd counts "
            "failed probe rounds instead (default 3)."
        )

    if "run_startup" in env:
        notes.append("run_startup is not mapped: Tetherd always reconciles once at start.")

    if "save_no_mcontids" in env:
        notes.append(
            "save_no_mcontids is not mapped. Tetherd keeps the last 10 provider "
            "IDs, which is enough to recognise orphans across a reboot."
        )

    for key in sorted(env):
        if key not in KNOWN_RDNDC_KEYS:
            unmapped.append(key)

    for key in unmapped:
        notes.append(
            f"{key} has no Tetherd equivalent and was skipped. If this was "
            "rebuild-dndc port-forwarding (rutorrent_pf and friends), that is "
            "intentionally out of scope."
        )

    return Translation(settings=settings, notes=tuple(notes), unmapped=tuple(unmapped))


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _as_number(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def _cron_to_seconds(cron: str) -> int | None:
    """A 5-field cron that is really just an interval, in seconds.

    Rebuild-DNDC documented ``*/5 * * * *`` as its default. Anything richer than
    'every N minutes' is not a setting Tetherd has, so it is left unmapped.
    """
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, day, month, weekday = parts
    if (hour, day, month, weekday) != ("*", "*", "*", "*"):
        return None
    if minute.startswith("*/"):
        try:
            every = int(minute[2:])
        except ValueError:
            return None
        if every < 1:
            return None
        return every * 60
    return None


def _env_lines(payload: Mapping[str, Any], prefix: str) -> list[str]:
    lines: list[str] = []
    for key, value in payload.items():
        env_key = f"{prefix}_{key.upper()}"
        if isinstance(value, dict):
            # Nested settings use a double underscore, matching Settings.env_nested_delimiter.
            lines.extend(_env_lines(value, f"{prefix}_{key.upper()}_"))
        elif isinstance(value, list):
            lines.append(f"{env_key}={' '.join(str(item) for item in value)}")
        elif isinstance(value, bool):
            lines.append(f"{env_key}={'true' if value else 'false'}")
        else:
            lines.append(f"{env_key}={value}")
    return lines
