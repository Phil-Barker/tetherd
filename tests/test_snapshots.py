"""The snapshot store: what makes rebuilding possible without Unraid's templates."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tetherd.snapshots import SCHEMA_VERSION, Snapshot, SnapshotError, SnapshotStore

from .conftest import DEPENDENT_ID, PROVIDER_ID, make_inspect


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "snapshots", retention=3)


class TestCapturing:
    def test_a_capture_can_be_read_back(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        captured = store.capture(dependent_payload)

        restored = store.latest("qbittorrent")

        assert restored is not None
        assert restored.digest == captured.digest
        assert restored.container_id == DEPENDENT_ID
        assert restored.payload["Config"]["Image"] == "alpine:3.22"

    def test_the_provider_is_recorded_alongside_the_configuration(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """Which provider a snapshot was taken against is useful when diagnosing."""
        assert store.capture(dependent_payload).provider_id == PROVIDER_ID

    def test_a_container_on_a_normal_network_records_no_provider(
        self, store: SnapshotStore, provider_payload: dict[str, Any]
    ) -> None:
        assert store.capture(provider_payload).provider_id is None

    def test_an_unnamed_payload_is_refused(self, store: SnapshotStore) -> None:
        with pytest.raises(SnapshotError, match="no name"):
            store.capture({"Id": "abc", "Config": {"Image": "alpine"}})

    def test_no_snapshots_yet_is_not_an_error(self, store: SnapshotStore) -> None:
        assert store.latest("never-seen") is None
        assert store.history("never-seen") == []
        assert store.containers() == []


class TestChangeDetection:
    """Writing on every reconcile would age out the configuration worth keeping.

    The loop reconciles every few minutes, so an unconditional write would fill a
    five-deep retention window with identical copies within the hour and discard
    the last known-good configuration.
    """

    def test_recapturing_an_unchanged_configuration_writes_nothing(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        first = store.capture(dependent_payload)
        second = store.capture(dependent_payload)

        assert first.is_new is True
        assert second.is_new is False
        assert len(store.history("qbittorrent")) == 1

    def test_a_changed_configuration_is_captured(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        store.capture(dependent_payload)

        dependent_payload["Config"]["Env"] = ["PATH=/usr/bin", "PUID=99"]
        second = store.capture(dependent_payload)

        assert second.is_new is True
        assert len(store.history("qbittorrent")) == 2

    def test_a_new_provider_id_is_not_a_configuration_change(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """The network mode changes on every provider replacement and is rewritten on rebuild.

        Treating it as a change would mean a container that is merely reattached
        repeatedly loses its real configuration history to churn.
        """
        store.capture(dependent_payload)

        dependent_payload["HostConfig"]["NetworkMode"] = "container:" + "b" * 64
        second = store.capture(dependent_payload)

        assert second.is_new is False
        assert len(store.history("qbittorrent")) == 1

    def test_runtime_state_is_not_a_configuration_change(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        store.capture(dependent_payload)

        dependent_payload["State"] = {"Running": False, "StartedAt": "2026-01-01T00:00:00Z"}
        dependent_payload["Id"] = "f" * 64

        assert store.capture(dependent_payload).is_new is False

    def test_an_unchanged_capture_still_returns_a_usable_snapshot(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """Callers rebuild from whatever capture returns, so it is never empty."""
        store.capture(dependent_payload)

        second = store.capture(dependent_payload)

        assert second.payload["Config"]["Image"] == "alpine:3.22"
        assert second.path.is_file()


class TestRetention:
    def test_only_the_configured_number_of_snapshots_is_kept(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        for index in range(6):
            dependent_payload["Config"]["Env"] = [f"REVISION={index}"]
            store.capture(dependent_payload)

        assert len(store.history("qbittorrent")) == 3

    def test_the_newest_snapshots_are_the_ones_kept(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        for index in range(6):
            dependent_payload["Config"]["Env"] = [f"REVISION={index}"]
            store.capture(dependent_payload)

        history = store.history("qbittorrent")

        assert [snapshot.payload["Config"]["Env"] for snapshot in history] == [
            ["REVISION=5"],
            ["REVISION=4"],
            ["REVISION=3"],
        ]

    def test_history_is_newest_first(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        store.capture(dependent_payload)
        dependent_payload["Config"]["Env"] = ["CHANGED=1"]
        newest = store.capture(dependent_payload)

        assert store.history("qbittorrent")[0].digest == newest.digest

    def test_retention_below_one_is_refused(self, tmp_path: Path) -> None:
        """Zero retention would mean capturing snapshots and immediately deleting them."""
        with pytest.raises(ValueError, match="at least one"):
            SnapshotStore(tmp_path, retention=0)

    def test_ordering_follows_the_sequence_not_the_timestamp(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """History must survive a host clock that steps backwards.

        An Unraid box with a dead RTC and no internet access boots with a wrong
        clock, and NTP then steps it. If ordering came from the timestamp, the
        newest snapshot would be treated as the oldest and pruned first.
        """
        store.capture(dependent_payload)
        dependent_payload["Config"]["Env"] = ["CHANGED=1"]
        newest = store.capture(dependent_payload)
        # Rename the newer file to claim an earlier wall-clock time.
        backdated = newest.path.with_name(
            newest.path.name.split("-", 1)[0] + "-19700101T000000.json"
        )
        newest.path.rename(backdated)

        assert store.history("qbittorrent")[0].payload["Config"]["Env"] == ["CHANGED=1"]

    def test_a_stray_file_does_not_displace_real_snapshots(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        snapshot = store.capture(dependent_payload)
        (snapshot.path.parent / "notes.json").write_text("{}")

        assert store.latest("qbittorrent") is not None
        assert store.latest("qbittorrent").container_id == DEPENDENT_ID  # type: ignore[union-attr]

    def test_captures_in_the_same_second_do_not_overwrite_each_other(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """A burst of provider events can produce two captures within one second."""
        for index in range(3):
            dependent_payload["Config"]["Env"] = [f"REVISION={index}"]
            store.capture(dependent_payload)

        assert len({snapshot.path for snapshot in store.history("qbittorrent")}) == 3


class TestPerContainerIsolation:
    def test_similarly_named_containers_keep_separate_histories(self, store: SnapshotStore) -> None:
        """Upstream matched container names by substring; this store never does."""
        store.capture(make_inspect(container_id="a" * 64, name="radarr"))
        store.capture(make_inspect(container_id="b" * 64, name="radarr-4k"))

        assert store.latest("radarr") is not None
        assert store.latest("radarr").container_id == "a" * 64  # type: ignore[union-attr]
        assert store.latest("radarr-4k").container_id == "b" * 64  # type: ignore[union-attr]

    def test_all_snapshotted_containers_can_be_listed(self, store: SnapshotStore) -> None:
        store.capture(make_inspect(container_id="a" * 64, name="prowlarr"))
        store.capture(make_inspect(container_id="b" * 64, name="bazarr"))

        assert store.containers() == ["bazarr", "prowlarr"]

    def test_forgetting_a_container_removes_its_history(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        store.capture(dependent_payload)
        dependent_payload["Config"]["Env"] = ["CHANGED=1"]
        store.capture(dependent_payload)

        removed = store.forget("qbittorrent")

        assert removed == 2
        assert store.latest("qbittorrent") is None
        assert store.containers() == []

    def test_forgetting_an_unknown_container_is_harmless(self, store: SnapshotStore) -> None:
        assert store.forget("never-seen") == 0


class TestResilience:
    """A snapshot store that can be broken by one bad file is not a safety net."""

    def test_a_corrupt_snapshot_does_not_hide_the_good_ones(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        good = store.capture(dependent_payload)
        dependent_payload["Config"]["Env"] = ["CHANGED=1"]
        newest = store.capture(dependent_payload)
        newest.path.write_text("{ truncated by a power cut")

        history = store.history("qbittorrent")

        assert len(history) == 1
        assert history[0].digest == good.digest

    def test_a_snapshot_missing_its_payload_is_skipped(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        snapshot = store.capture(dependent_payload)
        snapshot.path.write_text(json.dumps({"container": "qbittorrent", "inspect": {}}))

        assert store.latest("qbittorrent") is None

    def test_a_snapshot_missing_its_timestamp_falls_back_to_the_file_mtime(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        """Worth recovering: the configuration itself is intact."""
        snapshot = store.capture(dependent_payload)
        envelope = json.loads(snapshot.path.read_text())
        del envelope["captured_at"]
        snapshot.path.write_text(json.dumps(envelope))

        restored = store.latest("qbittorrent")

        assert restored is not None
        assert restored.captured_at.tzinfo is not None

    def test_an_unwritable_state_directory_is_reported_clearly(
        self, tmp_path: Path, dependent_payload: dict[str, Any]
    ) -> None:
        """The likeliest real cause is a missing or read-only /config mount."""
        read_only = tmp_path / "read-only"
        read_only.mkdir()
        read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            store = SnapshotStore(read_only / "snapshots")

            with pytest.raises(SnapshotError, match="writable state directory"):
                store.capture(dependent_payload)
        finally:
            read_only.chmod(stat.S_IRWXU)

    def test_no_temporary_files_are_left_behind(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        store.capture(dependent_payload)

        leftovers = list(store.directory.rglob(".tetherd-*"))

        assert leftovers == []

    @pytest.mark.skipif(os.name != "posix", reason="path traversal guard is POSIX-specific")
    def test_a_malformed_name_cannot_write_outside_the_store(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        dependent_payload["Name"] = "/../../escaped"

        snapshot = store.capture(dependent_payload)

        assert store.directory.resolve() in snapshot.path.resolve().parents


class TestSnapshotPresentation:
    def test_the_recorded_schema_version_is_written(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        snapshot = store.capture(dependent_payload)

        assert json.loads(snapshot.path.read_text())["schema_version"] == SCHEMA_VERSION

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=5), "5s ago"),
            (timedelta(minutes=3), "3m ago"),
            (timedelta(hours=5), "5h ago"),
            (timedelta(days=2), "2d ago"),
        ],
    )
    def test_age_reads_naturally_in_status_output(
        self, delta: timedelta, expected: str, dependent_payload: dict[str, Any]
    ) -> None:
        snapshot = Snapshot(
            container="qbittorrent",
            container_id=DEPENDENT_ID,
            provider_id=PROVIDER_ID,
            captured_at=datetime.now(UTC) - delta,
            digest="abc",
            payload=dependent_payload,
            path=Path("/tmp/unused.json"),
        )

        assert snapshot.age == expected

    def test_the_image_is_exposed_for_reporting(
        self, store: SnapshotStore, dependent_payload: dict[str, Any]
    ) -> None:
        assert store.capture(dependent_payload).image == "alpine:3.22"
