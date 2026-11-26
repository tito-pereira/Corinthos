# SPDX-License-Identifier: MPL-2.0
"""Per-user JSON state is bounded, versioned and separate from project state."""

from concurrent.futures import ThreadPoolExecutor
from os import link

import pytest

from corinthos import user_state
from corinthos.errors import IntegrityError

CONFIG = {"version": 1}


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "user" / "corinthos"
    monkeypatch.setattr(user_state, "state_root", lambda: root)
    return root


def test_read_is_noncreating_and_initialize_preserves_existing_document(state):
    assert user_state.read_state("config.json", CONFIG) == CONFIG
    assert not state.exists()
    user_state.initialize()
    config = state / "config.json"
    original = b'{ "version": 1, "example": true }\n'
    config.write_bytes(original)
    user_state.initialize()
    assert config.read_bytes() == original
    assert (state / "cache").is_dir() and (state / "logs").is_dir()
    assert user_state.read_state("recent-projects.json", CONFIG) == {
        "version": 1,
        "projects": [],
    }


@pytest.mark.parametrize("content", [b'{"version": 2}', b'{"version": true}', b"{", b"x" * 131073])
def test_invalid_or_future_documents_are_never_replaced(state, content):
    state.mkdir(parents=True)
    file = state / "config.json"
    file.write_bytes(content)
    with pytest.raises(IntegrityError):
        user_state.update_state("config.json", CONFIG, lambda _: CONFIG)
    assert file.read_bytes() == content


def test_lock_serializes_updates_and_rejected_transform_preserves_state(state):
    def increment(_):
        user_state.update_state(
            "config.json", CONFIG, lambda value: {**value, "count": value.get("count", 0) + 1}
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(4)))
    with pytest.raises(IntegrityError):
        user_state.update_state(
            "config.json", CONFIG, lambda _: {"version": 1, "bad": float("nan")}
        )
    assert user_state.read_state("config.json", CONFIG) == {"version": 1, "count": 4}


def test_state_cannot_access_other_paths(state):
    with pytest.raises(IntegrityError, match="Unknown"):
        user_state.read_state("../config.json", CONFIG)
    assert not state.exists()


def test_hardlinked_state_document_is_rejected(state):
    state.mkdir(parents=True)
    outside = state.parent / "outside.json"
    outside.write_text('{"version": 1}', encoding="utf-8")
    try:
        link(outside, state / "config.json")
    except PermissionError:
        pytest.skip("This account cannot create a test hardlink.")
    with pytest.raises(IntegrityError, match="ordinary"):
        user_state.update_state("config.json", CONFIG, lambda _: {**CONFIG, "changed": True})
    assert outside.read_text(encoding="utf-8") == '{"version": 1}'
