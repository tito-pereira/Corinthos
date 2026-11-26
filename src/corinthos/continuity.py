# SPDX-License-Identifier: MPL-2.0
"""Compact project continuity and guarded cleanup of temporary skill artifacts."""

import shutil
from pathlib import Path

from corinthos.errors import ConflictError, IntegrityError
from corinthos.filesystem import atomic_json, file_lock, plain_path, read_json
from corinthos.workspace import group_paths, open_project

FIELDS = ("completed", "pending", "blockers", "verification", "resume")


def directory(root):
    return Path(open_project(root)["root"]) / "context_agents/corinthos"


def show(root):
    value = read_json(directory(root) / "continuity.json", {key: "" for key in FIELDS})
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise IntegrityError("Unsupported continuity document.")
    if any(not isinstance(value[key], str) or len(value[key]) > 16000 for key in FIELDS):
        raise IntegrityError("Continuity fields must be strings of at most 16000 characters.")
    return value


def save(root, **fields):
    if not fields or fields.keys() - set(FIELDS):
        raise IntegrityError("Supply completed, pending, blockers, verification or resume.")
    target = directory(root)
    with file_lock(target / "project.lock"):
        value = show(root)
        value.update(fields)
        if any(not isinstance(v, str) or len(v) > 16000 for v in value.values()):
            raise IntegrityError("Compact continuity to at most 16000 characters per field.")
        atomic_json(target / "continuity.json", value)
    return value


def consolidate(root, summary):
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
        raise IntegrityError("Supply a concise cleanup summary of at most 8000 characters.")
    current = show(root)
    completed = current["completed"]
    if summary not in completed:
        completed = (completed + "\n" + summary).strip()
    if len(completed) > 16000:
        raise IntegrityError("Consolidate completed context before cleanup; nothing was removed.")
    return save(root, completed=completed)


def cleanup(root, kind, *, summary, group=None, resolved_decisions=False):
    """Save knowledge first; no target is removed when validation/consolidation fails."""
    root = Path(open_project(root)["root"])
    if kind not in {"reports", "group", "planning"}:
        raise IntegrityError("Select reports, group or planning cleanup.")
    from corinthos.runtime import list_runs

    if any(run["status"] in {"STARTING", "RUNNING"} for run in list_runs(root)):
        raise ConflictError("Stop project workers before cleanup.")
    if kind in {"reports", "group"}:
        task, auxiliary = group_paths(root, group)
        targets = (
            [
                task / name
                for name in (
                    "task-description.md",
                    "implement-report.md",
                    "review-report.md",
                    "task-improvement.md",
                )
            ]
            if kind == "reports"
            else [task, auxiliary]
        )
    else:
        targets = [root / "task/implementation-groups.md", root / "task_agents"]
        if resolved_decisions:
            targets.append(root / "context/DECISIONS.md")
    for target in targets:
        plain_path(target, file=target.suffix == ".md")
        if target.exists() and target.is_dir():
            for path in target.rglob("*"):
                plain_path(path, file=not path.is_dir())
    # Serialize cleanup with new admissions; consolidation uses the same reentrant-free lock separately.
    consolidate(root, summary)
    with file_lock(directory(root) / "project.lock"):
        from corinthos.runtime import _read_runs

        if any(run["status"] in {"STARTING", "RUNNING"} for run in _read_runs(root).values()):
            raise ConflictError("A worker started before cleanup; nothing was removed.")
        for target in targets:
            plain_path(target, file=target.suffix == ".md")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.write_text("", encoding="utf-8")
        if kind == "planning":
            (root / "task_agents").mkdir(exist_ok=True)
    return {
        "cleaned": kind,
        "continuity": show(root),
        "decisions_preserved": not resolved_decisions,
    }
