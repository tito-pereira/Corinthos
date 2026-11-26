# SPDX-License-Identifier: MPL-2.0
"""Project-local skill instructions and bounded task context."""

import pytest

from corinthos.errors import IntegrityError
from corinthos.skills import build_prompt, list_skills, show_skill
from corinthos.workspace import group_paths, initialize


def test_local_skill_customization_and_group_artifacts(tmp_path):
    initialize(tmp_path)
    skill = tmp_path / ".agents/skills/review-feature/SKILL.md"
    skill.write_text(
        "---\nname: review-feature\ndescription: Local review\n---\nRead evidence first.",
        encoding="utf-8",
    )
    task, _ = group_paths(tmp_path, 3, create=True)
    (task / "implement-report.md").write_text("Observed implementation", encoding="utf-8")
    (task / "task-description.md").write_text("Assess this change", encoding="utf-8")
    continuity = tmp_path / "context_agents/corinthos/continuity.json"
    continuity.write_text('{"pending": ["Follow up"]}', encoding="utf-8")

    prompt = build_prompt(tmp_path, "review-feature", group=3, instruction="Independent review")

    assert len(list_skills(tmp_path)) == 17
    assert show_skill(tmp_path, "review-feature")["description"] == "Local review"
    assert "Read evidence first." in prompt
    assert "task/task_3" in prompt
    assert "task_agents/task_3_agents" in prompt
    assert "Follow up" in prompt
    assert prompt.index("Observed implementation") < prompt.index("Assess this change")
    assert "Independent review" in prompt


def test_invalid_skill_and_oversized_context_are_rejected(tmp_path):
    initialize(tmp_path)
    for name in ("../outside", "a/b", "", "UNKNOWN"):
        with pytest.raises(IntegrityError):
            show_skill(tmp_path, name)
    (tmp_path / "context/CURRENT_STATE.md").write_bytes(b"x" * 65_537)
    with pytest.raises(IntegrityError, match="too large"):
        build_prompt(tmp_path, "implement-feature")


def test_custom_skill_is_discovered_without_bundled_fallback(tmp_path):
    initialize(tmp_path)
    custom = tmp_path / ".agents/skills/local-task/SKILL.md"
    custom.parent.mkdir()
    custom.write_text("Run a local task.", encoding="utf-8")
    (tmp_path / ".agents/skills/README.md").write_text("Local library", encoding="utf-8")
    assert "local-task" in {item["name"] for item in list_skills(tmp_path)}
    (tmp_path / ".agents/skills/create-feature/SKILL.md").unlink()
    with pytest.raises(IntegrityError, match="unavailable"):
        show_skill(tmp_path, "create-feature")
