"""Configuration loading, with emphasis on what an Unraid template can express."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from tetherd.config import CONFIG_FILE_ENV_VAR, Settings


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's own TETHERD_* variables or config file leaking into assertions."""
    for key in list(os.environ):
        if key.startswith("TETHERD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, "/nonexistent/tetherd.yaml")


def test_provider_is_required() -> None:
    with pytest.raises(ValidationError, match="provider"):
        Settings()  # type: ignore[call-arg]


def test_minimal_configuration_has_workable_defaults() -> None:
    settings = Settings(provider="gluetun")

    assert settings.provider == "gluetun"
    assert settings.reconcile_interval_seconds == 300.0
    assert settings.dry_run is False
    assert settings.probe.enabled is False
    assert settings.snapshot_dir == Path("/config/snapshots")


@pytest.mark.parametrize(
    "raw",
    ["qbittorrent,sonarr", "qbittorrent sonarr", "qbittorrent, sonarr", '["qbittorrent","sonarr"]'],
)
def test_lists_accept_the_formats_a_web_form_encourages(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("TETHERD_INCLUDE", raw)

    assert Settings(provider="gluetun").include == ["qbittorrent", "sonarr"]


def test_nested_settings_come_from_delimited_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TETHERD_PROBE__ENABLED", "true")
    monkeypatch.setenv("TETHERD_PROBE__TARGETS", "9.9.9.9 1.1.1.1")
    monkeypatch.setenv("TETHERD_NOTIFY__URLS", "tgram://token/chat")

    settings = Settings(provider="gluetun")

    assert settings.probe.enabled is True
    assert settings.probe.targets == ["9.9.9.9", "1.1.1.1"]
    assert settings.notify.urls == ["tgram://token/chat"]


def test_yaml_file_is_read_when_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = tmp_path / "tetherd.yaml"
    config_file.write_text(
        "provider: gluetun\nreconcile_interval_seconds: 60\nexclude:\n  - plex\n"
    )
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(config_file))

    settings = Settings()  # type: ignore[call-arg]

    assert settings.provider == "gluetun"
    assert settings.reconcile_interval_seconds == 60
    assert settings.exclude == ["plex"]


def test_environment_overrides_the_yaml_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "tetherd.yaml"
    config_file.write_text("provider: from-yaml\n")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(config_file))
    monkeypatch.setenv("TETHERD_PROVIDER", "from-env")

    assert Settings().provider == "from-env"  # type: ignore[call-arg]


class TestContradictoryScoping:
    """Misconfiguration should fail loudly at startup, not silently at runtime."""

    def test_a_container_cannot_be_both_included_and_excluded(self) -> None:
        with pytest.raises(ValidationError, match="both include and exclude"):
            Settings(provider="gluetun", include=["sonarr"], exclude=["sonarr"])

    def test_the_provider_cannot_be_its_own_dependent(self) -> None:
        with pytest.raises(ValidationError, match="cannot also be a managed dependent"):
            Settings(provider="gluetun", include=["gluetun"])


@pytest.mark.parametrize(
    ("env_var", "value"),
    [
        ("TETHERD_RECONCILE_INTERVAL_SECONDS", "0"),
        ("TETHERD_SNAPSHOT_RETENTION", "0"),
        ("TETHERD_RESTART_GRACE_SECONDS", "-1"),
        ("TETHERD_PROBE__TIMEOUT_SECONDS", "0"),
        ("TETHERD_PROBE__FAILURES_BEFORE_RESTART", "0"),
        ("TETHERD_LOG_LEVEL", "CHATTY"),
    ],
)
def test_nonsensical_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
) -> None:
    monkeypatch.setenv("TETHERD_PROVIDER", "gluetun")
    monkeypatch.setenv(env_var, value)

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
