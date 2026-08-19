"""Assessment behaviour, keyed to the failure modes proven in docs/design-notes.md."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tetherd.assess import assess
from tetherd.models import ContainerInfo, Verdict, parse_docker_timestamp

from .conftest import PROVIDER_ID, make_inspect


def info(payload: dict[str, Any]) -> ContainerInfo:
    return ContainerInfo.from_inspect(payload)


def test_dependent_started_after_provider_is_healthy(
    provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
) -> None:
    dependent_payload["State"]["StartedAt"] = "2026-08-19T09:54:00.000000000Z"

    result = assess(info(dependent_payload), info(provider_payload))

    assert result.verdict is Verdict.HEALTHY
    assert not result.needs_action


def test_provider_restarted_after_dependent_is_a_stale_namespace(
    provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
) -> None:
    """The case upstream cannot see: reference is correct, live, and still wrong."""
    dependent = info(dependent_payload)
    provider = info(provider_payload)

    assert dependent.provider_ref == provider.id, "reference is correct"
    assert provider.running, "provider is up"

    result = assess(dependent, provider)

    assert result.verdict is Verdict.STALE_NAMESPACE
    assert result.needs_action
    assert "no longer exists" in result.reason


def test_provider_recreated_leaves_a_dead_reference(
    provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
) -> None:
    provider_payload["Id"] = "e34532fbac3fc9c0fe4a93c1bd2c59824014a2d69514da6f7650996697813966"

    result = assess(info(dependent_payload), info(provider_payload))

    assert result.verdict is Verdict.DEAD_PROVIDER_REF
    assert result.needs_action
    assert "was recreated" in result.reason


def test_provider_down_defers_action(
    provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
) -> None:
    provider_payload["State"]["Running"] = False

    result = assess(info(dependent_payload), info(provider_payload))

    assert result.verdict is Verdict.PROVIDER_DOWN
    assert not result.needs_action


def test_never_started_dependent_is_not_actioned(
    provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
) -> None:
    dependent_payload["State"]["StartedAt"] = "0001-01-01T00:00:00Z"
    dependent_payload["State"]["Running"] = False

    result = assess(info(dependent_payload), info(provider_payload))

    assert result.verdict is Verdict.NEVER_STARTED
    assert not result.needs_action


def test_container_not_borrowing_a_network_is_ignored(
    provider_payload: dict[str, Any],
) -> None:
    standalone = make_inspect(container_id="a" * 64, name="plex", network_mode="bridge")

    result = assess(info(standalone), info(provider_payload))

    assert result.verdict is Verdict.HEALTHY
    assert not result.needs_action


class TestReferenceMatching:
    """Reference matching must never fall back to substring comparison.

    Substring matching on container names is the bug behind upstream issues #62
    and #77, where 'radarr' matched 'radarr-4k' and the wrong container was
    rebuilt or skipped.
    """

    def test_matches_provider_by_name(
        self, provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
    ) -> None:
        dependent_payload["HostConfig"]["NetworkMode"] = "container:gluetun"
        dependent_payload["State"]["StartedAt"] = "2026-08-19T09:54:00.000000000Z"

        assert assess(info(dependent_payload), info(provider_payload)).verdict is Verdict.HEALTHY

    def test_matches_abbreviated_provider_id(
        self, provider_payload: dict[str, Any], dependent_payload: dict[str, Any]
    ) -> None:
        dependent_payload["HostConfig"]["NetworkMode"] = f"container:{PROVIDER_ID[:12]}"
        dependent_payload["State"]["StartedAt"] = "2026-08-19T09:54:00.000000000Z"

        assert assess(info(dependent_payload), info(provider_payload)).verdict is Verdict.HEALTHY

    @pytest.mark.parametrize(
        "ref",
        [
            "gluetun-uk",  # a different container whose name extends the provider's
            "glue",
            PROVIDER_ID[:8],  # too short to be an unambiguous ID prefix
            PROVIDER_ID[8:20],  # a substring of the ID, but not a prefix
        ],
    )
    def test_rejects_names_and_ids_that_merely_resemble_the_provider(
        self, provider_payload: dict[str, Any], dependent_payload: dict[str, Any], ref: str
    ) -> None:
        dependent_payload["HostConfig"]["NetworkMode"] = f"container:{ref}"

        result = assess(info(dependent_payload), info(provider_payload))

        assert result.verdict is Verdict.DEAD_PROVIDER_REF


class TestTimestampParsing:
    def test_parses_nanosecond_precision(self) -> None:
        parsed = parse_docker_timestamp("2026-08-19T09:53:09.868937429Z")

        assert parsed == datetime(2026, 8, 19, 9, 53, 9, 868937, tzinfo=UTC)

    def test_treats_go_zero_time_as_absent(self) -> None:
        assert parse_docker_timestamp("0001-01-01T00:00:00Z") is None

    @pytest.mark.parametrize("value", [None, "", "not a timestamp"])
    def test_returns_none_for_unusable_input(self, value: str | None) -> None:
        assert parse_docker_timestamp(value) is None

    def test_result_is_timezone_aware_so_comparisons_never_raise(self) -> None:
        parsed = parse_docker_timestamp("2026-08-19T09:53:09Z")

        assert parsed is not None
        assert parsed.tzinfo is not None
