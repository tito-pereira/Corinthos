# SPDX-License-Identifier: MPL-2.0
"""Additive project workspaces backed by the distributed, project-local blueprint."""

import os
import uuid
from importlib.resources import files
from pathlib import Path

from corinthos.errors import ConflictError, IntegrityError
from corinthos.filesystem import atomic_json, file_lock, plain_path, read_json

WORKSPACE_FORMAT = 1
TASK_FILES = (
    "implementation-steps.md",
    "task-description.md",
    "implement-report.md",
    "review-report.md",
    "task-improvement.md",
)
_BASE_DIRS = ("repo", "context", "context_agents", "task", "task_agents", ".agents/skills")
_CONTEXT_FILES = ("PROJECT.md", "DECISIONS.md", "ARCHITECTURE.md", "CURRENT_STATE.md")


def _root(root) -> Path:
    if not isinstance(root, (str, os.PathLike)) or not str(root):
        raise IntegrityError("Select an absolute project workspace path.")
    path = Path(root)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or (os.name == "nt" and path.drive.startswith("\\\\"))
    ):
        raise IntegrityError("Select an absolute, ordinary local project workspace path.")
    path = Path(os.path.abspath(path))
    plain_path(path)
    return path


def _marker(root: Path) -> Path:
    return root / "context_agents/corinthos/state.json"


def _legacy(root: Path) -> None:
    for name in (".corinthos", ".nukytera", ".agentforge"):
        if os.path.lexists(root / name):
            raise ConflictError(
                f"Legacy governed state ({name}) is incompatible with this runtime. "
                "It has been preserved. Select a separate prototype workspace."
            )


def _identity(root: Path) -> dict:
    path = _marker(root)
    plain_path(path, file=True)
    if not path.is_file():
        raise IntegrityError("Workspace is not initialized; run project init for this root first.")
    value = read_json(path)
    try:
        valid_id = str(uuid.UUID(value["project_id"])) == value["project_id"]
    except (KeyError, TypeError, ValueError, AttributeError):
        valid_id = False
    if (
        not isinstance(value, dict)
        or type(value.get("workspace_format")) is not int
        or value["workspace_format"] != WORKSPACE_FORMAT
        or not valid_id
        or not isinstance(value.get("root"), str)
        or os.path.normcase(value["root"]) != os.path.normcase(str(root))
    ):
        raise IntegrityError("Workspace metadata is incompatible or belongs to a different root.")
    return {
        "root": str(root),
        "project_id": value["project_id"],
        "workspace_format": WORKSPACE_FORMAT,
    }


def _resources():
    def visit(directory, prefix=""):
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            name = prefix + child.name
            if child.is_dir():
                yield from visit(child, name + "/")
            elif name != "provenance.json":
                yield name, child.read_bytes()

    return dict(visit(files("corinthos.resources").joinpath("blueprint")))


def _preflight(root: Path, documents: dict[str, bytes]) -> None:
    for name in (*_BASE_DIRS, "context_agents/corinthos"):
        plain_path(root / name)
    for name in documents:
        plain_path(root / name, file=True)
    plain_path(_marker(root), file=True)
    legacy_architecture = root / "context/ARQUITECTURE.md"
    architecture = root / "context/ARCHITECTURE.md"
    plain_path(legacy_architecture, file=True)
    if legacy_architecture.is_file() and architecture.is_file():
        if legacy_architecture.read_bytes() != architecture.read_bytes():
            raise ConflictError(
                "ARCHITECTURE.md and ARQUITECTURE.md differ; reconcile them before initialization."
            )


def initialize(root) -> dict:
    """Fill missing blueprint files; never overwrite existing project content."""
    path = _root(root)
    _legacy(path)
    documents = _resources()
    documents.update({f"context/{name}": b"" for name in _CONTEXT_FILES})
    documents["task/implementation-groups.md"] = b""
    _preflight(path, documents)
    if _marker(path).exists():
        _identity(path)
    for name in (*_BASE_DIRS, "context_agents/corinthos"):
        (path / name).mkdir(parents=True, exist_ok=True)
    with file_lock(path / "context_agents/corinthos/workspace.lock"):
        _legacy(path)
        _preflight(path, documents)
        legacy_architecture = path / "context/ARQUITECTURE.md"
        if legacy_architecture.is_file() and not (path / "context/ARCHITECTURE.md").exists():
            documents["context/ARCHITECTURE.md"] = legacy_architecture.read_bytes()
        for name, content in documents.items():
            destination = path / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            plain_path(destination, file=True)
            try:
                with destination.open("xb") as output:
                    output.write(content)
            except FileExistsError:
                plain_path(destination, file=True)
        if not _marker(path).exists():
            atomic_json(
                _marker(path),
                {
                    "workspace_format": WORKSPACE_FORMAT,
                    "project_id": str(uuid.uuid4()),
                    "root": str(path),
                },
            )
    return open_project(path)


def open_project(root) -> dict:
    """Read an initialized workspace without migration or metadata mutation."""
    path = _root(root)
    _legacy(path)
    identity = _identity(path)
    for name in _BASE_DIRS:
        plain_path(path / name)
        if not (path / name).is_dir():
            raise IntegrityError(
                "Workspace layout is incomplete; run project init to fill missing files."
            )
    return identity


def status(root) -> dict:
    """Return project identity and the canonical local task location."""
    return {**open_project(root), "task_root": "task", "continuity": "context_agents/corinthos"}


def group_paths(root, group, *, create: bool = False) -> tuple[Path, Path]:
    """Resolve one positive group's paths, optionally creating only missing artifacts."""
    identity = open_project(root)
    if type(group) is not int or not 1 <= group <= 1_000_000:
        raise IntegrityError("Group must be a positive integer no larger than 1000000.")
    path = Path(identity["root"])
    task = path / "task" / f"task_{group}"
    auxiliary = path / "task_agents" / f"task_{group}_agents"
    plain_path(task)
    plain_path(auxiliary)
    for name in TASK_FILES:
        plain_path(task / name, file=True)
    if create:
        with file_lock(path / "context_agents/corinthos/workspace.lock"):
            plain_path(task)
            plain_path(auxiliary)
            task.mkdir(exist_ok=True)
            auxiliary.mkdir(exist_ok=True)
            for name in TASK_FILES:
                destination = task / name
                plain_path(destination, file=True)
                try:
                    with destination.open("xb"):
                        pass
                except FileExistsError:
                    plain_path(destination, file=True)
    return task, auxiliary
