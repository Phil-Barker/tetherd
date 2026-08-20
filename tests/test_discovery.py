"""Discovery scoping, including the orphan case that needs provider ID history."""

from __future__ import annotations

from typing import Any

import pytest

from tetherd.config import Settings
from tetherd.discovery import ENABLE_LABEL, SkipReason, discover
from tetherd.models import CONTAINER_NETWORK_PREFIX

from .conftest import PROVIDER_ID, make_inspect

OLD_PROVIDER_ID = "1111111111111111111111111111111111111111111111111111111111111111"


class FakeDockerApi:
    """Stands in for DockerApi, driven by inspect payloads.

    Mirrors the real split: the list endpoint reveals NetworkMode, so only
    network-borrowing containers are offered up, and callers inspect from there.
    """

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._by_ref: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            self._by_ref[payload["Id"]] = payload
            self._by_ref[str(payload["Name"]).lstrip("/")] = payload
        self._payloads = payloads

    def inspect(self, ref: str) -> dict[str, Any] | None:
        return self._by_ref.get(ref)

    def exists(self, ref: str) -> bool:
        return ref in self._by_ref

    def list_network_borrowers(self) -> list[str]:
        return [
            str(p["Id"])
            for p in self._payloads
            if str(p["HostConfig"]["NetworkMode"]).startswith(CONTAINER_NETWORK_PREFIX)
        ]


def api_with(*payloads: dict[str, Any]) -> Any:
    return FakeDockerApi(list(payloads))


def dependent(
    name: str, *, provider_ref: str = PROVIDER_ID, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    return make_inspect(
        container_id=f"{abs(hash(name)):064x}"[:64],
        name=name,
        network_mode=f"container:{provider_ref}",
        labels=labels,
    )


def settings(**overrides: Any) -> Settings:
    return Settings(provider="gluetun", **overrides)


def test_finds_the_provider_and_its_dependents(provider_payload: dict[str, Any]) -> None:
    api = api_with(provider_payload, dependent("qbittorrent"), dependent("sonarr"))

    result = discover(api, settings())

    assert result.provider is not None
    assert result.provider.name == "gluetun"
    assert [c.name for c in result.managed] == ["qbittorrent", "sonarr"]
    assert result.skipped == []


def test_reports_a_missing_provider_rather_than_failing(
    provider_payload: dict[str, Any],
) -> None:
    api = api_with(dependent("qbittorrent"))

    result = discover(api, settings())

    assert result.provider_missing
    # The dependent is still claimed: its reference points at nothing, so no other
    # provider can own it and it cannot start. Reconcile will then wait.
    assert [c.name for c in result.managed] == ["qbittorrent"]
    assert [c.name for c in result.adopted] == ["qbittorrent"]


def test_dependents_of_another_container_are_left_alone(
    provider_payload: dict[str, Any],
) -> None:
    other = make_inspect(container_id="b" * 64, name="other-vpn", network_mode="bridge")
    api = api_with(
        provider_payload, other, dependent("theirs", provider_ref="b" * 64), dependent("ours")
    )

    result = discover(api, settings())

    assert [c.name for c in result.managed] == ["ours"]
    skipped = {s.container.name: s for s in result.skipped}
    assert skipped["theirs"].reason is SkipReason.OTHER_PROVIDER
    assert "is not gluetun" in skipped["theirs"].detail


def test_an_orphan_is_adopted_even_without_history(
    provider_payload: dict[str, Any],
) -> None:
    """The state a host is in when Tetherd is installed to fix it.

    The provider was recreated before Tetherd ever ran, so there is no recorded
    history. The referenced ID exists on nothing, the container cannot start, and
    no other provider can claim it either — so guessing is safer than leaving it.
    """
    orphan = dependent("qbittorrent", provider_ref=OLD_PROVIDER_ID)
    api = api_with(provider_payload, orphan)

    result = discover(api, settings())

    assert [c.name for c in result.managed] == ["qbittorrent"]
    assert [c.name for c in result.adopted] == ["qbittorrent"]


def test_history_turns_an_adoption_into_a_recognition(
    provider_payload: dict[str, Any],
) -> None:
    """Once Tetherd has seen the provider, the guess is no longer a guess."""
    orphan = dependent("qbittorrent", provider_ref=OLD_PROVIDER_ID)
    api = api_with(provider_payload, orphan)

    result = discover(api, settings(), known_provider_ids={OLD_PROVIDER_ID})

    assert [c.name for c in result.managed] == ["qbittorrent"]
    assert result.adopted == []


def test_adoption_can_be_turned_off(
    provider_payload: dict[str, Any],
) -> None:
    """Two providers on one host: an orphan of the other one must not be claimed."""
    orphan = dependent("qbittorrent", provider_ref=OLD_PROVIDER_ID)
    api = api_with(provider_payload, orphan)

    result = discover(api, settings(adopt_orphans=False))

    assert result.managed == []
    assert result.skipped[0].reason is SkipReason.ORPHANED
    assert "adopt_orphans is off" in result.skipped[0].detail


def test_the_provider_is_never_managed_as_its_own_dependent(
    provider_payload: dict[str, Any],
) -> None:
    """A provider may itself route through something else, e.g. a second VPN hop."""
    provider_payload["HostConfig"]["NetworkMode"] = f"container:{PROVIDER_ID}"
    api = api_with(provider_payload)

    result = discover(api, settings())

    assert result.managed == []
    assert [s.reason for s in result.skipped] == [SkipReason.IS_PROVIDER]


class TestFilters:
    def test_exclude_takes_a_container_out_of_scope(self, provider_payload: dict[str, Any]) -> None:
        api = api_with(provider_payload, dependent("qbittorrent"), dependent("sonarr"))

        result = discover(api, settings(exclude=["sonarr"]))

        assert [c.name for c in result.managed] == ["qbittorrent"]
        assert result.skipped[0].reason is SkipReason.EXCLUDED

    def test_include_restricts_scope_to_the_named_containers(
        self, provider_payload: dict[str, Any]
    ) -> None:
        api = api_with(provider_payload, dependent("qbittorrent"), dependent("sonarr"))

        result = discover(api, settings(include=["qbittorrent"]))

        assert [c.name for c in result.managed] == ["qbittorrent"]
        assert result.skipped[0].reason is SkipReason.NOT_INCLUDED

    def test_include_does_not_match_on_substrings(self, provider_payload: dict[str, Any]) -> None:
        api = api_with(provider_payload, dependent("radarr"), dependent("radarr-4k"))

        result = discover(api, settings(include=["radarr"]))

        assert [c.name for c in result.managed] == ["radarr"]

    def test_require_label_opts_out_unlabelled_containers(
        self, provider_payload: dict[str, Any]
    ) -> None:
        api = api_with(
            provider_payload,
            dependent("opted-in", labels={ENABLE_LABEL: "true"}),
            dependent("unlabelled"),
        )

        result = discover(api, settings(require_label=True))

        assert [c.name for c in result.managed] == ["opted-in"]
        assert result.skipped[0].reason is SkipReason.MISSING_LABEL

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " true "])
    def test_label_accepts_the_obvious_affirmatives(
        self, provider_payload: dict[str, Any], value: str
    ) -> None:
        api = api_with(provider_payload, dependent("app", labels={ENABLE_LABEL: value}))

        result = discover(api, settings(require_label=True))

        assert [c.name for c in result.managed] == ["app"]


def test_an_included_container_that_is_not_wired_up_is_reported(
    provider_payload: dict[str, Any],
) -> None:
    """The diagnostic for upstream issue #57.

    The user explicitly asked for 'sonarr' to be managed, but it is not
    borrowing anyone's network, so it never appears in discovery at all. Saying
    so is far more useful than silently managing nothing.
    """
    misconfigured = make_inspect(container_id="c" * 64, name="sonarr", network_mode="bridge")
    api = api_with(provider_payload, misconfigured, dependent("qbittorrent"))

    result = discover(api, settings(include=["qbittorrent", "sonarr"]))

    assert [c.name for c in result.managed] == ["qbittorrent"]
    assert result.unresolved_includes == ["sonarr"]
