# SPDX-License-Identifier: MPL-2.0
"""Bounded, passive Project State from the existing owners of project facts."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from corinthos.context.repository import ContextRepository
from corinthos.contracts import ContractError, ContractRef, ContractStore
from corinthos.lifecycle.repository import LifecycleRepository, LifecycleSummary
from corinthos.project_registry import ProjectRegistryService, display_name
from corinthos.project_status import ProjectStorage, inspect_project
from corinthos.sessions.model import SessionSummary
from corinthos.sessions.repository import SessionRepository
from corinthos.validation.repository import ValidationRepository, ValidationSummary
from corinthos.work import WorkCounts, WorkService

_MAX_REFERENCES = 16
_MISSING_SOURCES = (
    "PROJECT_IDENTITY",
    "PROJECT_CONTRACT",
    "WORKFLOW",
    "MODULES",
    "TECHNOLOGY_DECISIONS",
    "VALIDATION",
    "LIFECYCLE",
)


@dataclass(frozen=True)
class _ProjectSources:
    project_id: str | None
    active_project: ContractRef | None
    active_workflow: ContractRef | None
    modules: list[ContractRef]
    technology_decisions: list[ContractRef]
    validation: ValidationSummary | None
    lifecycle: LifecycleSummary | None
    work_items: WorkCounts | None
    unavailable_sources: list[str]
    sessions: SessionSummary | None
    context: dict | None
    external_import: dict | None


@dataclass(frozen=True)
class ProjectSummary(_ProjectSources):
    root: str
    exists: bool
    is_directory: bool
    initialized: bool
    storage: ProjectStorage
    schema_version: int | None
    display_name: str
    last_accessed_at: str | None


def _unavailable(
    work_items: WorkCounts | None,
    sessions: SessionSummary | None = None,
    context: dict | None = None,
) -> _ProjectSources:
    unavailable = list(_MISSING_SOURCES)
    if work_items is None:
        unavailable.append("WORK_ITEMS")
    return _ProjectSources(
        None, None, None, [], [], None, None, work_items, unavailable, sessions, context, None
    )


def _inspect_sources(
    work: WorkService, connection: sqlite3.Connection, schema: int
) -> _ProjectSources:
    project = ContractStore.active_references(connection, None, "PROJECT", limit=2)
    project_id = (
        project[0].project_id
        if len(project) == 1
        else WorkService.project_identity(connection, schema_version=schema)
    )
    sessions = SessionRepository.snapshot_summary(connection) if schema >= 8 else None
    context = ContextRepository.snapshot_summary(connection) if schema >= 9 else None
    if project_id is None:
        return _unavailable(
            work.counts(connection=connection) if schema >= 7 else None, sessions, context
        )

    workflow = ContractStore.active_references(connection, project_id, "WORKFLOW", limit=2)
    modules = ContractStore.active_references(
        connection, project_id, "MODULE", limit=_MAX_REFERENCES + 1
    )
    decisions = ContractStore.active_references(
        connection, project_id, "TECHNOLOGY_DECISION", limit=_MAX_REFERENCES + 1
    )
    validation = (
        ValidationRepository.project_summary(connection, project_id) if schema >= 5 else None
    )
    lifecycle = LifecycleRepository.project_summary(connection, project_id) if schema >= 6 else None
    work_items = work.counts(connection=connection) if schema >= 7 else None
    from corinthos.external_import.storage import pointer

    imported = pointer(connection, project_id) if schema >= 9 else None
    unavailable = [
        name
        for name, absent in (
            ("PROJECT_CONTRACT", len(project) != 1),
            ("WORKFLOW", len(workflow) != 1),
            ("MODULES_TRUNCATED", len(modules) > _MAX_REFERENCES),
            ("TECHNOLOGY_DECISIONS_TRUNCATED", len(decisions) > _MAX_REFERENCES),
            ("VALIDATION", validation is None),
            ("LIFECYCLE", lifecycle is None),
            ("WORK_ITEMS", work_items is None),
        )
        if absent
    ]
    return _ProjectSources(
        project_id,
        project[0] if len(project) == 1 else None,
        workflow[0] if len(workflow) == 1 else None,
        modules[:_MAX_REFERENCES],
        decisions[:_MAX_REFERENCES],
        validation,
        lifecycle,
        work_items,
        unavailable,
        sessions,
        context,
        imported,
    )


class ProjectStateService:
    """Reconstruct the summary for one root without migration, recency writes or probes."""

    def summary(self, root: str | Path) -> ProjectSummary:
        work = WorkService(root)
        status, sources = inspect_project(
            root, read=lambda connection, schema: _inspect_sources(work, connection, schema)
        )
        if sources is None:
            sources = _unavailable(None)
        try:
            last_accessed = ProjectRegistryService().last_accessed_at(status.root)
        except (ContractError, OSError):
            last_accessed = None
            sources.unavailable_sources.append("REGISTRY")
        return ProjectSummary(
            **vars(status),
            **vars(sources),
            display_name=display_name(status.root),
            last_accessed_at=last_accessed,
        )
