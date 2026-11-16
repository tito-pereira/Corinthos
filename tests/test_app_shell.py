# SPDX-License-Identifier: MPL-2.0
"""Interactive Home and non-authoritative recent-project references."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest

from corinthos.app_shell import _remember
from corinthos.bootstrap import initialize
from corinthos.cli import main
from corinthos.project_manager.service import ProjectManager
from corinthos.user_state import state_root


@pytest.fixture
def isolated_user(tmp_path, monkeypatch):
    user_base = tmp_path / "user-state"
    user_base.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(user_base))
    monkeypatch.setenv("XDG_STATE_HOME", str(user_base))
    return state_root()


def _input(monkeypatch, *answers):
    remaining = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(remaining))


def _recent(root):
    return json.loads((root / "recent-projects.json").read_text(encoding="utf-8"))


def test_home_creates_project_and_resumes_same_draft(tmp_path, monkeypatch, capsys, isolated_user):
    root = tmp_path / "projects" / "Offline notes"
    _input(monkeypatch, "1", str(root), "1", "Create an offline notes CLI.", "5", "0", "0")
    assert main([]) == 0
    assert (root / ".corinthos" / "bootstrap.json").is_file()
    manager = ProjectManager(root)
    [original] = manager.list()
    assert original["payload"]["vision"] == "Create an offline notes CLI."
    assert manager.store.list_events() == []
    assert _recent(isolated_user)["projects"][0]["root"] == str(root.resolve())
    assert "PROJECT" in capsys.readouterr().out

    _input(monkeypatch, "3", "1", "2", "1", "5", "0", "0")
    assert main([]) == 0
    assert "Project Completion" in capsys.readouterr().out
    assert manager.list() == [original]
    assert manager.store.list_events() == []


@pytest.mark.parametrize("has_database", [False, True])
def test_open_current_only_records_a_reference(
    tmp_path, monkeypatch, capsys, isolated_user, has_database
):
    root = tmp_path / "workspace"
    initialize(root)
    if has_database:
        ProjectManager(root)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    monkeypatch.chdir(root)
    _input(monkeypatch, "0", "0")
    assert main(["."]) == 0
    assert "Start project specification" in capsys.readouterr().out
    assert _recent(isolated_user)["projects"][0]["root"] == str(root.resolve())
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert before == after


def test_home_provider_settings_and_help_menus_return_without_native_work(
    monkeypatch, capsys, isolated_user
):
    def unexpected(*args, **kwargs):
        pytest.fail("Listing providers must not run native discovery.")

    monkeypatch.setattr("corinthos.providers.refresh_providers", unexpected)
    _input(monkeypatch, "4", "0", "5", "0", "6", "0", "0")
    assert main([]) == 0
    output = capsys.readouterr().out
    assert all(label in output for label in ("PROVIDERS", "SETTINGS", "usage: corinthos"))
    assert output.count("0. Back") == 3
    assert not isolated_user.exists()


def test_open_current_rejects_uninitialized_and_unrecognized_state(
    tmp_path, monkeypatch, capsys, isolated_user
):
    monkeypatch.chdir(tmp_path)
    assert main(["."]) == 1
    assert "No Corinthos project exists" in capsys.readouterr().err
    assert not (tmp_path / ".corinthos").exists()

    private = tmp_path / ".corinthos"
    private.mkdir()
    database = private / "state.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 0")
    before = database.read_bytes()
    assert main(["."]) == 1
    assert "application or schema version is unsupported" in capsys.readouterr().err
    assert database.read_bytes() == before
    assert not (isolated_user / "recent-projects.json").exists()


def test_missing_recent_project_is_neither_created_nor_searched(
    tmp_path, monkeypatch, capsys, isolated_user
):
    root = tmp_path / "original"
    initialize(root)
    _remember(root.resolve())
    moved = root.rename(tmp_path / "moved")
    _input(monkeypatch, "3", "1", "0")
    assert main([]) == 0
    result = capsys.readouterr()
    assert "workspace does not exist" in result.err
    assert str(moved) not in result.out
    assert not root.exists()
    assert _recent(isolated_user)["projects"][0]["root"] == str(root.resolve())


def test_recent_projects_are_bounded_and_reopened_paths_are_deduplicated(tmp_path, isolated_user):
    roots = [(tmp_path / f"project-{i}").resolve() for i in range(22)]
    for root in roots:
        initialize(root)
        _remember(root)
    _remember(roots[10])
    result = _recent(isolated_user)
    expected = [roots[10], *[root for root in reversed(roots[2:]) if root != roots[10]]]
    assert result.keys() == {"version", "projects"} and result["version"] == 3
    assert [item["root"] for item in result["projects"]] == [str(root) for root in expected]
    for entry in result["projects"]:
        assert entry.keys() == {"root", "last_accessed_at"}
        assert datetime.fromisoformat(entry["last_accessed_at"]).tzinfo is not None
