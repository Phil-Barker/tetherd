"""Notification: useful when it works, harmless when it does not.

The property that matters most is containment. A misconfigured Telegram URL or a
hook script with a typo must not turn a successful repair into a failed one, and
must not stall the loop that performs the next repair.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tetherd.config import NotifySettings
from tetherd.notify import (
    AppriseSink,
    HookSink,
    Notification,
    NotificationError,
    Notifier,
    Severity,
    UnraidSink,
    _redact,
    build_notifier,
    describe_unavailable,
)


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class RecordingSink:
    def __init__(self, name: str = "recording") -> None:
        self._name = name
        self.received: list[Notification] = []

    @property
    def name(self) -> str:
        return self._name

    def send(self, notification: Notification) -> None:
        self.received.append(notification)


class BrokenSink:
    def __init__(self, name: str = "broken") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def send(self, notification: Notification) -> None:
        del notification
        raise NotificationError("the service refused the message")


class TestContainment:
    def test_a_broken_sink_does_not_stop_the_others(self) -> None:
        working = RecordingSink()

        failures = Notifier([BrokenSink(), working]).send(
            Notification(title="Repaired", message="qbittorrent was restarted")
        )

        assert len(working.received) == 1
        assert failures == ["broken: the service refused the message"]

    def test_a_sink_that_raises_something_unexpected_is_still_contained(self) -> None:
        """A third-party notification library may raise anything at all."""

        class Exploding:
            name = "exploding"

            def send(self, notification: Notification) -> None:
                del notification
                raise ValueError("unexpected")

        failures = Notifier([Exploding()]).send(Notification(title="t", message="m"))

        assert failures == ["exploding: unexpected"]

    def test_no_sinks_configured_is_not_an_error(self) -> None:
        notifier = Notifier([])

        assert notifier.send(Notification(title="t", message="m")) == []
        assert notifier.configured is False


class TestUnraidSink:
    def test_it_is_unavailable_when_the_script_is_absent(self, tmp_path: Path) -> None:
        """Which is the case on every machine that is not Unraid."""
        assert UnraidSink(tmp_path / "notify").available is False

    def test_it_is_unavailable_when_the_script_is_not_executable(self, tmp_path: Path) -> None:
        script = tmp_path / "notify"
        script.write_text("#!/bin/sh\n")

        assert UnraidSink(script).available is False

    def test_it_calls_the_script_with_unraids_own_vocabulary(self, tmp_path: Path) -> None:
        script = executable(tmp_path / "notify", f'printf "%s\\n" "$@" > {tmp_path}/args')

        UnraidSink(script).send(
            Notification(
                title="Provider down", message="gluetun is unhealthy", severity=Severity.ERROR
            )
        )

        arguments = (tmp_path / "args").read_text().splitlines()
        assert arguments == [
            "-e",
            "Tetherd",
            "-s",
            "Provider down",
            "-d",
            "gluetun is unhealthy",
            "-i",
            "alert",
        ]

    @pytest.mark.parametrize(
        ("severity", "importance"),
        [
            (Severity.INFO, "normal"),
            (Severity.WARNING, "warning"),
            (Severity.ERROR, "alert"),
        ],
    )
    def test_severity_maps_onto_unraids_importance_levels(
        self, severity: Severity, importance: str
    ) -> None:
        assert severity.unraid_importance == importance

    def test_a_failing_script_is_reported_with_its_own_output(self, tmp_path: Path) -> None:
        script = executable(tmp_path / "notify", "echo 'no such event' >&2; exit 2")

        with pytest.raises(NotificationError, match="no such event"):
            UnraidSink(script).send(Notification(title="t", message="m"))


class TestHookSink:
    def test_the_hook_receives_the_notification_as_environment_variables(
        self, tmp_path: Path
    ) -> None:
        """So a script can act on it without parsing prose."""
        hook = executable(tmp_path / "hook", f"env | grep ^TETHERD_ | sort > {tmp_path}/env")

        HookSink(hook).send(
            Notification(
                title="Rebuilt",
                message="qbittorrent was rebuilt",
                severity=Severity.WARNING,
                context={"container": "qbittorrent", "action": "recreate"},
            )
        )

        captured = dict(line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines())
        assert captured["TETHERD_CONTAINER"] == "qbittorrent"
        assert captured["TETHERD_ACTION"] == "recreate"
        assert captured["TETHERD_SEVERITY"] == "warning"
        assert captured["TETHERD_TITLE"] == "Rebuilt"

    def test_a_non_executable_hook_is_unavailable(self, tmp_path: Path) -> None:
        hook = tmp_path / "hook"
        hook.write_text("#!/bin/sh\n")

        assert HookSink(hook).available is False

    def test_a_failing_hook_is_reported(self, tmp_path: Path) -> None:
        hook = executable(tmp_path / "hook", "exit 1")

        with pytest.raises(NotificationError, match="exited 1"):
            HookSink(hook).send(Notification(title="t", message="m"))


class TestBuildingFromConfiguration:
    def test_unraid_is_skipped_on_a_host_that_is_not_unraid(self) -> None:
        """`notify.unraid` defaults to true, so this is the common case off Unraid."""
        notifier = build_notifier(NotifySettings(unraid=True))

        assert notifier.sink_names == []

    def test_apprise_is_configured_when_urls_are_given(self) -> None:
        notifier = build_notifier(NotifySettings(unraid=False, urls=["json://localhost/"]))

        assert notifier.sink_names == ["apprise"]

    def test_a_usable_hook_is_configured(self, tmp_path: Path) -> None:
        hook = executable(tmp_path / "hook", "true")

        notifier = build_notifier(NotifySettings(unraid=False, hook=hook))

        assert notifier.sink_names == ["hook"]

    def test_an_unusable_hook_is_surfaced_rather_than_silently_dropped(
        self, tmp_path: Path
    ) -> None:
        """Otherwise a user believes they are being notified when they are not."""
        hook = tmp_path / "hook"
        hook.write_text("#!/bin/sh\n")

        problems = describe_unavailable(NotifySettings(unraid=False, hook=hook))

        assert len(problems) == 1
        assert "chmod +x" in problems[0]

    def test_a_working_configuration_reports_no_problems(self, tmp_path: Path) -> None:
        hook = executable(tmp_path / "hook", "true")

        assert describe_unavailable(NotifySettings(unraid=False, hook=hook)) == []


class TestCredentialHandling:
    """Notification URLs carry bot tokens and API keys, and they end up in logs."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("tgram://12345:secret-bot-token/98765", "tgram://***"),
            ("discord://webhook-id/webhook-token", "discord://***"),
            ("mailto://user:password@example.invalid", "mailto://***"),
            ("nonsense", "<malformed url>"),
        ],
    )
    def test_a_url_is_reduced_to_its_scheme(self, url: str, expected: str) -> None:
        assert _redact(url) == expected

    def test_an_unparseable_url_is_rejected_without_leaking_it(self) -> None:
        """Apprise validates the scheme locally, so no network call is made."""
        with pytest.raises(NotificationError) as caught:
            AppriseSink(["not-a-real-scheme://12345:secret-bot-token@host"]).send(
                Notification(title="t", message="m")
            )

        assert "secret-bot-token" not in str(caught.value)
        assert "rejected" in str(caught.value)
