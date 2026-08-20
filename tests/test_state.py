"""Remembering the provider's past IDs, which is what makes orphans recognisable."""

from __future__ import annotations

from pathlib import Path

import pytest

from tetherd.state import ProviderState, ProviderStateStore


@pytest.fixture
def store(tmp_path: Path) -> ProviderStateStore:
    return ProviderStateStore(tmp_path / "provider-state.json", history=3)


class TestRemembering:
    def test_a_first_provider_id_is_recorded(self, store: ProviderStateStore) -> None:
        state = store.remember("a" * 64)

        assert store.load().ids == ("a" * 64,)
        assert state.current == "a" * 64

    def test_history_survives_a_new_store_on_the_same_file(self, store: ProviderStateStore) -> None:
        """The point of persisting at all: a host reboot restarts Tetherd too.

        The provider comes up with a fresh ID and every dependent still points at
        the old one, which only the previous process ever saw.
        """
        store.remember("a" * 64)

        reopened = ProviderStateStore(store.path)

        assert reopened.load().ids == ("a" * 64,)

    def test_a_new_provider_id_is_kept_alongside_the_old_one(
        self, store: ProviderStateStore
    ) -> None:
        store.remember("a" * 64)
        store.remember("b" * 64)

        assert store.load().ids == ("b" * 64, "a" * 64)

    def test_the_current_id_leads_the_history(self, store: ProviderStateStore) -> None:
        store.remember("a" * 64)
        store.remember("b" * 64)

        assert store.load().current == "b" * 64

    def test_seeing_the_same_id_again_does_not_duplicate_it(
        self, store: ProviderStateStore
    ) -> None:
        """The common case by far: every pass sees the same running provider."""
        for _ in range(5):
            store.remember("a" * 64)

        assert store.load().ids == ("a" * 64,)

    def test_a_returning_id_moves_back_to_the_front(self, store: ProviderStateStore) -> None:
        store.remember("a" * 64)
        store.remember("b" * 64)
        store.remember("a" * 64)

        assert store.load().ids == ("a" * 64, "b" * 64)

    def test_history_is_capped(self, store: ProviderStateStore) -> None:
        """Only the most recent few can plausibly still be referenced."""
        for letter in "abcde":
            store.remember(letter * 64)

        assert store.load().ids == ("e" * 64, "d" * 64, "c" * 64)

    def test_an_empty_id_is_ignored(self, store: ProviderStateStore) -> None:
        store.remember("a" * 64)

        assert store.remember("").ids == ("a" * 64,)


class TestResilience:
    def test_absent_state_reads_as_empty(self, store: ProviderStateStore) -> None:
        assert store.load().ids == ()
        assert store.load().current is None

    def test_corrupt_state_reads_as_empty_rather_than_failing(
        self, store: ProviderStateStore
    ) -> None:
        """Losing one pass of orphan recognition beats refusing to start."""
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{ truncated")

        assert store.load().ids == ()

    def test_state_of_the_wrong_shape_reads_as_empty(self, store: ProviderStateStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('{"provider_ids": "not-a-list"}')

        assert store.load().ids == ()

    def test_corrupt_state_is_replaced_on_the_next_write(self, store: ProviderStateStore) -> None:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{ truncated")

        store.remember("a" * 64)

        assert store.load().ids == ("a" * 64,)


class TestProviderStateValue:
    def test_remembering_does_not_mutate_the_original(self) -> None:
        original = ProviderState(ids=("a" * 64,))

        updated = original.remembering("b" * 64)

        assert original.ids == ("a" * 64,)
        assert updated.ids == ("b" * 64, "a" * 64)
