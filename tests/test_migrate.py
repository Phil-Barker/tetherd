"""Rebuild-DNDC environment translation: map what we can, explain what we cannot."""

from __future__ import annotations

from tetherd.migrate import collect_rdndc, translate_rdndc


def test_the_documented_docker_run_translates() -> None:
    """The example from Rebuild-DNDC's README, minus secrets."""
    translation = translate_rdndc(
        {
            "mastercontname": "vpn",
            "mastercontconcheck": "yes",
            "ping_ip": "1.1.1.1",
            "ping_ip_alt": "8.8.8.8",
            "ping_count": "4",
            "sleep_secs": "10",
            "cron": "*/5 * * * *",
            "run_startup": "yes",
            "discord_notifications": "yes",
            "discord_url": "https://discordapp.com/api/webhooks/xxx/yyy",
        }
    )

    assert translation.settings["provider"] == "vpn"
    assert translation.settings["probe"]["enabled"] is True
    assert translation.settings["probe"]["targets"] == ["1.1.1.1", "8.8.8.8"]
    assert translation.settings["probe"]["settle_seconds"] == 10.0
    assert translation.settings["reconcile_interval_seconds"] == 300
    assert translation.settings["notify"]["urls"] == ["https://discordapp.com/api/webhooks/xxx/yyy"]
    assert any("ping_count is not mapped" in note for note in translation.notes)
    assert any("run_startup is not mapped" in note for note in translation.notes)


def test_cont_list_becomes_include_with_a_warning() -> None:
    """Rebuild-DNDC used cont_list only for manual rebuilds, not as a scope filter."""
    translation = translate_rdndc(
        {"mastercontname": "gluetun", "cont_list": "ContainerA ContainerB"}
    )

    assert translation.settings["include"] == ["ContainerA", "ContainerB"]
    assert any("restricts" in note for note in translation.notes)


def test_keys_are_matched_case_insensitively() -> None:
    translation = translate_rdndc({"MasterContName": "GluetunVPN"})

    assert translation.settings["provider"] == "GluetunVPN"


def test_missing_provider_is_called_out() -> None:
    translation = translate_rdndc({"ping_ip": "1.1.1.1"})

    assert "provider" not in translation.settings
    assert any("mastercontname was not set" in note for note in translation.notes)


def test_port_forwarding_is_reported_as_out_of_scope() -> None:
    translation = translate_rdndc(
        {"mastercontname": "gluetun", "rutorrent_pf": "yes", "rutorrent_cont_name": "ruTorrent"}
    )

    assert "rutorrent_pf" in translation.unmapped
    assert any("intentionally out of scope" in note for note in translation.notes)


def test_yaml_is_valid_and_carries_the_notes() -> None:
    translation = translate_rdndc({"mastercontname": "gluetun", "ping_count": "4"})

    dumped = translation.as_yaml()

    assert dumped.startswith("# ping_count is not mapped")
    assert "provider: gluetun" in dumped


def test_env_form_nests_with_double_underscores() -> None:
    translation = translate_rdndc(
        {"mastercontname": "gluetun", "mastercontconcheck": "yes", "ping_ip": "1.1.1.1"}
    )

    dumped = translation.as_env()

    assert "TETHERD_PROVIDER=gluetun" in dumped
    assert "TETHERD_PROBE__ENABLED=true" in dumped
    assert "TETHERD_PROBE__TARGETS=1.1.1.1" in dumped


def test_collect_ignores_the_rest_of_the_environment() -> None:
    collected = collect_rdndc(
        {
            "PATH": "/usr/bin",
            "mastercontname": "gluetun",
            "TETHERD_PROVIDER": "should-not-appear",
            "rutorrent_pf": "yes",
        }
    )

    assert collected == {"mastercontname": "gluetun", "rutorrent_pf": "yes"}


def test_a_rich_cron_is_left_unmapped() -> None:
    translation = translate_rdndc({"mastercontname": "vpn", "cron": "0 2 * * *"})

    assert "reconcile_interval_seconds" not in translation.settings
    assert any("could not be parsed" in note for note in translation.notes)
