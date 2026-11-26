# SPDX-License-Identifier: MPL-2.0
"""Fresh read-only GUI summaries and bounded project context."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from corinthos.errors import IntegrityError
from corinthos.gui_content import pending_groups, project_context, summary, supporting_context
from corinthos.workspace import initialize


@pytest.fixture
def project(tmp_path):
    initialize(tmp_path)
    return tmp_path


def test_group_table_uses_records_not_prose_or_fenced_examples(project):
    path = project / "task/implementation-groups.md"
    path.write_text(
        "Allowed statuses: Not Implemented, In Progress, Suspended.\n"
        "```md\n| Group | Status | Objective |\n| --- | --- | --- |\n"
        "| 99 | Not Implemented | Example only |\n```\n"
        "| Group | Title | Status | Dependencies |\n| --- | --- | --- | --- |\n"
        "| 1 | Finished | Implemented | None |\n"
        "| 2 | Current work \\| details | In Progress | None |\n"
        "| 3 | Resume work | Suspended | None |\n",
        encoding="utf-8",
    )
    assert pending_groups(project) == [
        {"group": 2, "status": "In Progress", "objective": "Current work | details"},
        {"group": 3, "status": "Suspended", "objective": "Resume work"},
    ]
    path.write_text(
        "| Group | Status | Objective |\n| --- | --- | --- |\n| 1 | Implemented | Done |",
        encoding="utf-8",
    )
    assert summary(project, "tasks") == {"kind": "tasks", "empty": True, "items": []}


def test_headed_status_checkboxes_ignore_completed_feature_checkboxes(project):
    (project / "task/implementation-groups.md").write_text(
        "## Group 4: Project GUI\n- [ ] Implemented\n- [x] In Progress\n"
        "- [ ] Not Implemented\n- [x] A completed feature\n"
        "## Group 5: Already done\n**Status:** Implemented\n",
        encoding="utf-8",
    )
    assert pending_groups(project) == [
        {"group": 4, "status": "In Progress", "objective": "Project GUI"}
    ]


@pytest.mark.parametrize(
    "text",
    [
        "| Group | Status | Objective |\n| --- | --- | --- |\n| 1 | Maybe | Work |",
        "| Group | Status | Objective |\n| --- | --- | --- |\n| 1 | In Progress |",
        "## Group 1: Missing status\nWork to do.",
        "## Group 1: Ambiguous\n- [x] Implemented\n- [x] Suspended",
    ],
)
def test_malformed_group_records_are_errors_not_empty_or_dispatchable(project, text):
    (project / "task/implementation-groups.md").write_text(text, encoding="utf-8")
    with pytest.raises(IntegrityError):
        pending_groups(project)


def test_ideas_are_fresh_empty_completed_or_requested_without_writes(project):
    path = project / "context/DECISIONS.md"
    assert summary(project, "ideas") == {"kind": "ideas", "empty": True, "items": []}
    path.unlink()
    assert summary(project, "ideas")["empty"]
    path.write_text("# Ideas\n\nNo pending ideas.\n", encoding="utf-8")
    assert summary(project, "ideas")["empty"]
    path.write_text("# Ideas\n**Status:** Completed\n- Add earlier feature.\n", encoding="utf-8")
    assert summary(project, "ideas")["empty"]
    path.write_text(
        "# New request\n**Status:** Requested implementation; not yet implemented.\n"
        "## Conversation\n- [ ] Add project chat.\n- [x] Old completed work.\n"
        "## Acceptance criteria\n1. Detailed duplicate requirement.\n",
        encoding="utf-8",
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = summary(project, "ideas")
    assert not result["empty"]
    assert "Add project chat." in result["items"]
    assert not any("completed work" in item or "duplicate" in item for item in result["items"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_document_read_and_utf8_failures_are_not_empty_state(project, monkeypatch):
    path = project / "context/DECISIONS.md"
    path.write_bytes(b"\xff")
    with pytest.raises(IntegrityError, match="Cannot read"):
        summary(project, "ideas")
    path.write_text("An idea", encoding="utf-8")
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("unreadable fixture")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(IntegrityError, match="Cannot read"):
        summary(project, "ideas")


def test_project_context_includes_current_support_and_local_skills(project):
    (project / "README.md").write_text("The selected project's instructions.", encoding="utf-8")
    (project / "context/ARCHITECTURE.md").unlink()
    current = project / "context_agents/current/current-status.md"
    current.parent.mkdir()
    current.write_text("Selected project resume detail.", encoding="utf-8")
    archived = project / "context_agents/history/old.md"
    archived.parent.mkdir()
    archived.write_text("Obsolete archived fact.", encoding="utf-8")
    custom = project / ".agents/skills/local/SKILL.md"
    custom.parent.mkdir()
    custom.write_text("Local instructions", encoding="utf-8")
    prompt = project_context(project)
    assert "The selected project's instructions." in prompt
    assert "context/ARCHITECTURE.md\n[missing]" in prompt
    assert "Selected project resume detail." in prompt
    assert ".agents/skills/local/SKILL.md" in prompt
    assert "Obsolete archived fact." not in prompt
    assert len(prompt.encode("utf-8")) <= 196_608


def test_supporting_context_budget_is_explicit_and_prioritizes_current(project):
    directory = project / "context_agents"
    (directory / "current-status.md").write_text("Current priority", encoding="utf-8")
    for index in range(3):
        (directory / f"large-{index}.md").write_text("é" * 30_000, encoding="utf-8")
    result = supporting_context(project)
    assert "Current priority" in result
    assert "omitted" in result
    assert len(result.encode("utf-8")) <= 65_536


def test_context_rejects_unsafe_or_oversized_essential_documents(project):
    source = project / "context/PROJECT.md"
    source.unlink()
    original = project / "original.md"
    original.write_text("Hardlinked context", encoding="utf-8")
    os.link(original, source)
    with pytest.raises(IntegrityError, match="ordinary"):
        project_context(project)
    source.unlink()
    source.write_bytes(b"x" * 65_537)
    with pytest.raises(IntegrityError, match="64 KiB"):
        project_context(project)


def test_summary_wire_size_is_bounded_for_unicode(project):
    (project / "task/implementation-groups.md").write_text(
        "| Group | Status | Objective |\n| --- | --- | --- |\n"
        + "\n".join(f"| {index} | Not Implemented | {'計' * 600} |" for index in range(1, 13)),
        encoding="utf-8",
    )
    result = summary(project, "tasks")
    assert not result["empty"]
    assert len(json.dumps(result).encode("utf-8")) <= 8192
    assert "omitted" in result["items"][-1]
    with pytest.raises(IntegrityError):
        summary(project, "unknown")
