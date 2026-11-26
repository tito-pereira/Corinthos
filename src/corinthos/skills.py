# SPDX-License-Identifier: MPL-2.0
"""Read project-local skill instructions without imposing a workflow on them."""

import re
from pathlib import Path

from corinthos.errors import IntegrityError
from corinthos.filesystem import plain_path
from corinthos.workspace import TASK_FILES, group_paths, open_project

MAX_PROMPT_BYTES = 262_144
_MAX_FILE_BYTES = 65_536
_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _read(path: Path) -> str:
    plain_path(path, file=True)
    try:
        with path.open("rb") as source:
            data = source.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise IntegrityError(f"Project input is too large: {path.name}.")
        return data.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise IntegrityError(f"Project input must use UTF-8: {path.name}.") from error


def _description(content: str) -> str:
    if content.startswith("---\n"):
        for line in content.split("---", 2)[1].splitlines():
            if line.startswith("description:"):
                return line.partition(":")[2].strip().strip("\"'")[:512]
    return ""


def show_skill(root, name) -> dict:
    """Read the selected project's own skill, including local customizations."""
    identity = open_project(root)
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise IntegrityError("Select a valid project-local skill name.")
    path = Path(identity["root"]) / ".agents/skills" / name / "SKILL.md"
    if not path.is_file():
        raise IntegrityError(f"Project-local skill is unavailable: {name}.")
    content = _read(path)
    return {
        "name": name,
        "description": _description(content),
        "content": content,
        "path": str(path),
    }


def list_skills(root) -> list[dict]:
    """List the current local library, including user-defined skills."""
    identity = open_project(root)
    directory = Path(identity["root"]) / ".agents/skills"
    entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    if len(entries) > 128:
        raise IntegrityError("Project-local skill library exceeds 128 entries.")
    result = []
    for entry in entries:
        if entry.is_file():
            plain_path(entry, file=True)
            continue
        plain_path(entry)
        if not entry.is_dir() or not (entry / "SKILL.md").is_file():
            continue
        skill = show_skill(root, entry.name)
        result.append({"name": skill["name"], "description": skill["description"]})
    return result


def build_prompt(root, name, group=None, instruction="") -> str:
    """Supply bounded project instructions and artifacts to the selected skill."""
    identity = open_project(root)
    if not isinstance(instruction, str) or len(instruction.encode("utf-8")) > _MAX_FILE_BYTES:
        raise IntegrityError("Skill instruction must be bounded UTF-8 text.")
    path = Path(identity["root"])
    skill = show_skill(path, name)
    sections = [f"Project: {identity['project_id']}\nSkill: {name}"]
    inputs = [path / "AGENTS.md", path / "README.md"]
    inputs.extend(
        path / "context" / filename
        for filename in ("PROJECT.md", "DECISIONS.md", "ARCHITECTURE.md", "CURRENT_STATE.md")
    )
    inputs.append(path / "context_agents/corinthos/continuity.json")
    if group is not None:
        task, _ = group_paths(path, group)
        sections.append(
            f"Group: {group}\nTask: task/task_{group}\nAuxiliary: task_agents/task_{group}_agents"
        )
        # Reviewers see implementation evidence before the task they assess.
        ordered = (
            "implement-report.md",
            *[item for item in TASK_FILES if item != "implement-report.md"],
        )
        inputs.extend(task / filename for filename in ordered)
    for item in inputs:
        if item.exists():
            sections.append(f"## {item.relative_to(path).as_posix()}\n{_read(item)}")
    sections.append(f"## Selected SKILL.md\n{skill['content']}")
    if instruction:
        sections.append(f"## Requested work\n{instruction}")
    prompt = "\n\n".join(sections)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise IntegrityError("Selected project context exceeds the prompt limit; compact it first.")
    return prompt
