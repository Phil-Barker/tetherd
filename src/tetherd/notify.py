"""Tell the user what happened, through whichever channels they configured.

Three sinks, all optional and all independent: Unraid's native notifier, any
Apprise URL, and an executable hook.

The governing rule is that notification is never allowed to affect remediation. A
sink that is misconfigured, unreachable, or slow must not turn a successful repair
into a failure, or delay the next one — so every sink is wrapped, every failure is
collected rather than raised, and everything with a network or a subprocess in it
has a timeout. The worst outcome of a broken notifier is a logged warning.

Unraid's notifier is preferred where it exists because it puts the message in the
place a user is already looking: the web UI's notification bell, and whatever
email or agent they have configured there. The recon of a live 7.3.2 host
confirmed the script's location.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from .config import NotifySettings

#: Confirmed present on Unraid 7.3.2 by scripts/unraid-recon.sh.
UNRAID_NOTIFY_SCRIPT: Final = Path("/usr/local/emhttp/webGui/scripts/notify")

_SUBPROCESS_TIMEOUT: Final = 30.0
_EVENT_NAME: Final = "Tetherd"


class Severity(StrEnum):
    """How loudly to say it."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def unraid_importance(self) -> str:
        """Unraid's own vocabulary, which drives the icon and colour it uses."""
        return {
            Severity.INFO: "normal",
            Severity.WARNING: "warning",
            Severity.ERROR: "alert",
        }[self]


@dataclass(frozen=True, slots=True)
class Notification:
    """One thing worth telling the user about."""

    title: str
    message: str
    severity: Severity = Severity.INFO
    #: Structured detail, passed to the hook as environment variables so a script
    #: can act on it without parsing the human-readable message.
    context: Mapping[str, str] = field(default_factory=dict)

    def as_environment(self) -> dict[str, str]:
        """The hook's environment, namespaced to avoid colliding with anything."""
        environment = {
            "TETHERD_TITLE": self.title,
            "TETHERD_MESSAGE": self.message,
            "TETHERD_SEVERITY": str(self.severity),
        }
        for key, value in self.context.items():
            environment[f"TETHERD_{key.upper()}"] = value
        return environment


class Sink(Protocol):
    """One delivery channel."""

    @property
    def name(self) -> str: ...

    def send(self, notification: Notification) -> None:
        """Deliver, or raise. The caller is responsible for containing failures."""


class UnraidSink:
    """Unraid's native notifier, which surfaces in the web UI's notification bell."""

    def __init__(self, script: Path = UNRAID_NOTIFY_SCRIPT) -> None:
        self._script = script

    @property
    def name(self) -> str:
        return "unraid"

    @property
    def available(self) -> bool:
        return self._script.is_file() and os.access(self._script, os.X_OK)

    def send(self, notification: Notification) -> None:
        _run(
            [
                str(self._script),
                "-e",
                _EVENT_NAME,
                "-s",
                notification.title,
                "-d",
                notification.message,
                "-i",
                notification.severity.unraid_importance,
            ]
        )


class AppriseSink:
    """Any of the several dozen services Apprise speaks."""

    def __init__(self, urls: Sequence[str]) -> None:
        self._urls = list(urls)

    @property
    def name(self) -> str:
        return "apprise"

    @property
    def available(self) -> bool:
        return bool(self._urls)

    def send(self, notification: Notification) -> None:
        # Imported here rather than at module scope so that neither `tetherd
        # doctor` nor the test suite pays for Apprise's import cost, and so a
        # broken install degrades to one failing sink instead of no Tetherd.
        import apprise

        client = apprise.Apprise()
        for url in self._urls:
            if not client.add(url):
                raise NotificationError(f"Apprise rejected the URL {_redact(url)}")

        if not client.notify(title=notification.title, body=notification.message):
            destinations = ", ".join(_redact(url) for url in self._urls)
            raise NotificationError(f"Apprise could not deliver to {destinations}")


class HookSink:
    """A user-supplied executable, given the notification as environment variables."""

    def __init__(self, hook: Path) -> None:
        self._hook = hook

    @property
    def name(self) -> str:
        return "hook"

    @property
    def available(self) -> bool:
        return self._hook.is_file() and os.access(self._hook, os.X_OK)

    def send(self, notification: Notification) -> None:
        _run([str(self._hook)], environment=notification.as_environment())


class NotificationError(RuntimeError):
    """A sink could not deliver."""


class Notifier:
    """Fans a notification out to every configured sink, containing failures."""

    def __init__(self, sinks: Iterable[Sink]) -> None:
        self._sinks = list(sinks)

    @property
    def sink_names(self) -> list[str]:
        return [sink.name for sink in self._sinks]

    @property
    def configured(self) -> bool:
        return bool(self._sinks)

    def send(self, notification: Notification) -> list[str]:
        """Deliver to every sink, returning a description of any that failed.

        Failures are returned rather than raised. A notifier that can break a
        repair is worse than no notifier.
        """
        failures: list[str] = []
        for sink in self._sinks:
            try:
                sink.send(notification)
            # Deliberately broad: a third-party notification library may raise
            # anything, and none of it is worth failing a repair over.
            except Exception as exc:
                failures.append(f"{sink.name}: {exc}")
        return failures


def build_notifier(settings: NotifySettings) -> Notifier:
    """Assemble the sinks the configuration asks for and that are actually usable.

    A sink the host cannot support is skipped silently: `notify.unraid` defaults
    to true so that Unraid users get native notifications without configuring
    anything, which means it is also true on every machine that is not Unraid.
    """
    sinks: list[Sink] = []

    if settings.unraid:
        unraid = UnraidSink()
        if unraid.available:
            sinks.append(unraid)

    apprise_sink = AppriseSink(settings.urls)
    if apprise_sink.available:
        sinks.append(apprise_sink)

    if settings.hook is not None:
        hook = HookSink(settings.hook)
        if hook.available:
            sinks.append(hook)

    return Notifier(sinks)


def describe_unavailable(settings: NotifySettings) -> list[str]:
    """Configured sinks that cannot be used, for `tetherd doctor` to report.

    Silently dropping a sink the user explicitly asked for is how someone ends up
    believing they are being notified when they are not.
    """
    problems: list[str] = []

    if settings.hook is not None and not HookSink(settings.hook).available:
        problems.append(
            f"the notification hook {settings.hook} is not an executable file, so it "
            "will never run. Check the path and that it is chmod +x."
        )

    return problems


def _run(command: Sequence[str], environment: Mapping[str, str] | None = None) -> None:
    """Run a command, translating every failure mode into NotificationError."""
    merged = {**os.environ, **(environment or {})}
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env=merged,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotificationError(
            f"{command[0]} did not finish within {_SUBPROCESS_TIMEOUT:.0f}s"
        ) from exc
    except OSError as exc:
        raise NotificationError(f"{command[0]} could not be run: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise NotificationError(
            f"{command[0]} exited {completed.returncode}" + (f": {detail[-1]}" if detail else "")
        )


def _redact(url: str) -> str:
    """A notification URL with its credentials removed.

    These carry bot tokens and API keys, and they end up in logs.
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return "<malformed url>"
    return f"{scheme}://***" if remainder else f"{scheme}://"
