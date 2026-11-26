# SPDX-License-Identifier: MPL-2.0
"""Keep all disposable runtime and navigation state inside the test directory."""

import pytest


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user"))
