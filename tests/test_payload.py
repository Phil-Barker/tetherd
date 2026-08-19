"""Create-request assembly, and the sanitising that keeps a rebuild from failing."""

from __future__ import annotations

from typing import Any

import pytest

from tetherd.payload import (
    FORBIDDEN_CONFIG_FIELDS,
    FORBIDDEN_HOST_CONFIG_FIELDS,
    PayloadError,
    build_create_request,
)

from .conftest import PROVIDER_ID, make_inspect

NEW_PROVIDER_ID = "e34532fbac3fc9c0fe4a93c1bd2c59824014a2d69514da6f7650996697813966"


@pytest.fixture
def captured() -> dict[str, Any]:
    payload = make_inspect(
        container_id="d" * 64,
        name="qbittorrent",
        network_mode=f"container:{PROVIDER_ID}",
        labels={"net.unraid.docker.managed": "dockerman"},
    )
    payload["HostConfig"]["Binds"] = ["/mnt/user/downloads:/downloads:rw"]
    payload["HostConfig"]["Memory"] = 536870912
    return payload


def test_network_mode_is_repointed_at_the_current_provider(captured: dict[str, Any]) -> None:
    """The recorded provider no longer exists; that is why a rebuild is happening."""
    request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

    assert request.body["HostConfig"]["NetworkMode"] == f"container:{NEW_PROVIDER_ID}"


def test_configuration_is_carried_over_verbatim(captured: dict[str, Any]) -> None:
    request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

    assert request.name == "qbittorrent"
    assert request.image == "alpine:3.22"
    assert request.body["Env"] == captured["Config"]["Env"]
    assert request.body["Cmd"] == captured["Config"]["Cmd"]
    assert request.body["Labels"] == {"net.unraid.docker.managed": "dockerman"}
    assert request.body["HostConfig"]["Binds"] == ["/mnt/user/downloads:/downloads:rw"]
    assert request.body["HostConfig"]["Memory"] == 536870912
    assert request.body["HostConfig"]["RestartPolicy"] == {
        "Name": "unless-stopped",
        "MaximumRetryCount": 0,
    }


class TestSanitising:
    """The fix for upstream issues #80, #69 and #65.

    A container whose template carries port mappings is destroyed and then
    rejected on creation with "conflicting options: port publishing and the
    container type network mode", leaving the user with nothing.
    """

    def test_port_bindings_are_removed_and_reported(self, captured: dict[str, Any]) -> None:
        captured["HostConfig"]["PortBindings"] = {"8080/tcp": [{"HostPort": "8080"}]}
        captured["Config"]["ExposedPorts"] = {"8080/tcp": {}}

        request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

        assert "PortBindings" not in request.body["HostConfig"]
        assert "ExposedPorts" not in request.body
        reported = {s.path for s in request.stripped}
        assert reported == {"HostConfig.PortBindings", "Config.ExposedPorts"}
        assert all("network namespace" in s.reason for s in request.stripped)

    @pytest.mark.parametrize("field", sorted(FORBIDDEN_HOST_CONFIG_FIELDS))
    def test_every_forbidden_host_config_field_is_removed(
        self, captured: dict[str, Any], field: str
    ) -> None:
        captured["HostConfig"][field] = ["something"] if field != "PublishAllPorts" else True

        request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

        assert field not in request.body["HostConfig"]
        assert f"HostConfig.{field}" in {s.path for s in request.stripped}

    @pytest.mark.parametrize("field", sorted(FORBIDDEN_CONFIG_FIELDS))
    def test_every_forbidden_config_field_is_removed(
        self, captured: dict[str, Any], field: str
    ) -> None:
        captured["Config"][field] = "something"

        request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

        assert field not in request.body
        assert f"Config.{field}" in {s.path for s in request.stripped}

    def test_settings_the_user_never_made_are_not_reported(self, captured: dict[str, Any]) -> None:
        """Inspect reports unset fields as empty, which is not worth telling anyone about."""
        captured["HostConfig"].update(
            {"PortBindings": {}, "Dns": [], "ExtraHosts": None, "PublishAllPorts": False}
        )
        captured["Config"]["ExposedPorts"] = {}

        request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

        assert request.stripped == ()

    def test_hostname_is_dropped_silently_when_docker_assigned_it(
        self, captured: dict[str, Any]
    ) -> None:
        """Docker defaults a container's hostname to its own ID.

        That is not a user setting, so it is removed without a warning; it would
        otherwise be reported on every single rebuild.
        """
        captured["Config"]["Hostname"] = captured["Id"][:12]

        request = build_create_request(captured, provider_id=NEW_PROVIDER_ID)

        assert "Hostname" not in request.body


class TestUnusableConfiguration:
    """Refusing to build a bad request is what keeps teardown from happening."""

    def test_missing_image_is_refused(self, captured: dict[str, Any]) -> None:
        captured["Config"]["Image"] = ""

        with pytest.raises(PayloadError, match="no image"):
            build_create_request(captured, provider_id=NEW_PROVIDER_ID)

    def test_missing_name_is_refused(self, captured: dict[str, Any]) -> None:
        captured["Name"] = ""

        with pytest.raises(PayloadError, match="no container name"):
            build_create_request(captured, provider_id=NEW_PROVIDER_ID)

    def test_missing_provider_is_refused(self, captured: dict[str, Any]) -> None:
        with pytest.raises(PayloadError, match="no current provider"):
            build_create_request(captured, provider_id="")

    def test_an_explicit_name_overrides_the_captured_one(self, captured: dict[str, Any]) -> None:
        request = build_create_request(
            captured, provider_id=NEW_PROVIDER_ID, name="qbittorrent-restored"
        )

        assert request.name == "qbittorrent-restored"
