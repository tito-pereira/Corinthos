# SPDX-License-Identifier: MPL-2.0
"""Workspace preservation and bundled-blueprint behavior."""

import hashlib
import json
import os
from importlib.resources import files

import pytest

from corinthos.errors import ConflictError, IntegrityError
from corinthos.workspace import TASK_FILES, group_paths, initialize, open_project


def test_initialize_packages_all_skills_and_preserves_local_content(tmp_path):
    project = tmp_path / "project"
    (project / "repo").mkdir(parents=True)
    (project / "repo/source.txt").write_text("original source", encoding="utf-8")
    (project / "context").mkdir()
    (project / "context/PROJECT.md").write_text("Custom purpose", encoding="utf-8")
    (project / "context/ARQUITECTURE.md").write_bytes(b"Existing architecture\r\n")
    (project / "AGENTS.md").write_text("Custom instructions", encoding="utf-8")
    customized = project / ".agents/skills/implement-feature/SKILL.md"
    customized.parent.mkdir(parents=True)
    customized.write_text("Custom implementation skill", encoding="utf-8")

    first = initialize(project)
    second = initialize(project)

    assert first == second == open_project(project)
    assert first["workspace_format"] == 1
    assert len(list((project / ".agents/skills").iterdir())) == 17
    assert (project / "repo/source.txt").read_text(encoding="utf-8") == "original source"
    assert (project / "context/PROJECT.md").read_text(encoding="utf-8") == "Custom purpose"
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "Custom instructions"
    assert customized.read_text(encoding="utf-8") == "Custom implementation skill"
    assert (project / "context/ARCHITECTURE.md").read_bytes() == b"Existing architecture\r\n"
    assert (project / "context/ARQUITECTURE.md").read_bytes() == b"Existing architecture\r\n"


def test_blueprint_inventory_and_normalized_cleanup_boundaries():
    blueprint = files("corinthos.resources").joinpath("blueprint")
    inventory = json.loads(blueprint.joinpath("provenance.json").read_text(encoding="utf-8"))
    assert len(inventory["files"]) == 20
    assert sum(row["path"].endswith("/SKILL.md") for row in inventory["files"]) == 17
    for row in inventory["files"]:
        data = blueprint.joinpath(row["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"]
        assert b'"/task_?/' not in data
        assert b'"/task_?_agents/' not in data
    for name, action in (
        ("clean-reports", "reports"),
        ("orchestrator", "group"),
        ("docs-updater", "planning"),
    ):
        text = blueprint.joinpath(f".agents/skills/{name}/SKILL.md").read_text(encoding="utf-8")
        assert f"corinthos cleanup {action}" in text
        assert "summary" in text


@pytest.mark.parametrize("name", [".corinthos", ".nukytera"])
def test_legacy_state_is_opaque_and_unchanged(tmp_path, name):
    project = tmp_path / "old"
    legacy = project / name / "project.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"opaque old database\x00\xff")
    before = legacy.read_bytes()
    with pytest.raises(ConflictError, match="preserved"):
        initialize(project)
    assert legacy.read_bytes() == before
    assert list(project.iterdir()) == [legacy.parent]


def test_preflight_conflict_leaves_existing_workspace_untouched(tmp_path):
    project = tmp_path / "project"
    (project / "context").mkdir(parents=True)
    (project / "context/ARQUITECTURE.md").write_text("Old", encoding="utf-8")
    (project / "context/ARCHITECTURE.md").write_text("New", encoding="utf-8")
    with pytest.raises(ConflictError, match="differ"):
        initialize(project)
    assert {item.name for item in project.iterdir()} == {"context"}
    assert (project / "context/ARCHITECTURE.md").read_text(encoding="utf-8") == "New"


def test_wrong_file_kind_and_hardlinks_rejected_before_creation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").mkdir()
    with pytest.raises((IntegrityError, RuntimeError)):
        initialize(project)
    assert list(project.iterdir()) == [project / "AGENTS.md"]
    (project / "AGENTS.md").rmdir()
    source = tmp_path / "shared.txt"
    source.write_text("Shared bytes", encoding="utf-8")
    os.link(source, project / "AGENTS.md")
    with pytest.raises((IntegrityError, RuntimeError)):
        initialize(project)
    assert source.read_text(encoding="utf-8") == "Shared bytes"


def test_identity_is_bound_to_root_and_group_paths_are_canonical(tmp_path):
    project = tmp_path / "one"
    initialize(project)
    task, auxiliary = group_paths(project, 2, create=True)
    assert task == project / "task/task_2"
    assert auxiliary == project / "task_agents/task_2_agents"
    assert {item.name for item in task.iterdir()} == set(TASK_FILES)
    assert list(auxiliary.iterdir()) == []
    (task / "implement-report.md").write_text("Evidence", encoding="utf-8")
    group_paths(project, 2, create=True)
    assert (task / "implement-report.md").read_text(encoding="utf-8") == "Evidence"
    for invalid in (True, 0, -1, "2", 1_000_001):
        with pytest.raises(IntegrityError):
            group_paths(project, invalid, create=True)
    marker = project / "context_agents/corinthos/state.json"
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["root"] = str(tmp_path / "two")
    marker.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(IntegrityError, match="different root"):
        open_project(project)


def test_open_unmarked_is_read_only(tmp_path):
    project = tmp_path / "unmarked"
    project.mkdir()
    with pytest.raises(IntegrityError, match="init"):
        open_project(project)
    assert list(project.iterdir()) == []
