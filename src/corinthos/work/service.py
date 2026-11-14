# SPDX-License-Identifier: MPL-2.0
"""Project-local Work Item operations with atomic relationship updates."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from corinthos.bootstrap import _read_profile, _root
from corinthos.contracts import ConflictError, ContractStore
from corinthos.contracts.artifacts import private_directory
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.project_status import _absolute_root

from .model import (
    TRANSITIONS,
    AffectedModule,
    ScopeLevel,
    WorkCounts,
    WorkError,
    WorkItem,
    WorkStatus,
    WorkTreeNode,
)
from .repository import WorkRepository

MAX_OBJECTIVE_CHARS = 600
MAX_CRITERION_CHARS = 300
MAX_CRITERIA = 8
MAX_MODULES = 12
MAX_DEPENDENCIES = 16
MAX_PAGE_SIZE = 25
MAX_OFFSET = 1_000_000
_WORK_ID = re.compile(r"work-[0-9a-f]{32}")
_UNSET = object()


def _id(value, label: str, pattern=ID_PATTERN) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkError(f"Invalid {label}.")
    return value


def _text(value, label: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= limit
        or not value.strip()
        or any(not (char.isprintable() or char in "\n\t") for char in value)
    ):
        raise WorkError(f"Invalid {label}.")
    return value


def _choice(kind, value, label: str):
    try:
        return kind(value)
    except (ValueError, TypeError):
        raise WorkError(f"Unknown {label}.") from None


def _strings(values, label: str, limit: int, validate, *, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not minimum <= len(values) <= limit:
        raise WorkError(f"Invalid {label} list length.")
    return tuple(validate(value, label) for value in values)


def _criteria(values) -> str:
    return canonical_json(
        list(
            _strings(
                values,
                "acceptance criterion",
                MAX_CRITERIA,
                lambda value, label: _text(value, label, MAX_CRITERION_CHARS),
                minimum=1,
            )
        )
    )


def _references(values, label: str, limit: int, pattern=ID_PATTERN) -> tuple[str, ...]:
    identifiers = _strings(values, label, limit, lambda value, label: _id(value, label, pattern))
    if len(identifiers) != len(set(identifiers)):
        raise WorkError(f"Duplicate {label}.")
    return identifiers


def _policy(value) -> str:
    value = _id(value, "validation policy")
    if len(value) > 64:
        raise WorkError("Invalid validation policy.")
    return value


def _page(limit, offset) -> None:
    if (
        type(limit) is not int
        or type(offset) is not int
        or not 1 <= limit <= MAX_PAGE_SIZE
        or not 0 <= offset <= MAX_OFFSET
    ):
        raise WorkError("Invalid Work Item page.")


def _timestamp(previous: str | None = None) -> str:
    now = datetime.now(UTC)
    if previous is not None:
        now = max(now, datetime.fromisoformat(previous) + timedelta(microseconds=1))
    return now.isoformat(timespec="microseconds")


def _empty_counts() -> WorkCounts:
    return WorkCounts(
        0,
        {status.value: 0 for status in WorkStatus},
        {scope.value: 0 for scope in ScopeLevel},
    )


class WorkService:
    """Plan work within a caller-authorized project root."""

    def __init__(self, root: str | Path):
        self.root = root

    @contextmanager
    def _read(self) -> Iterator[WorkRepository | None]:
        root = _root(_absolute_root(self.root), missing_ok=True)
        if root is None or not os.path.lexists(root / ".corinthos"):
            yield None
            return
        with ContractStore.inspect_snapshot(root) as (connection, version):
            yield (
                WorkRepository(connection)
                if connection is not None and version is not None and version >= 7
                else None
            )

    def _write_store(self, *, create: bool = False) -> ContractStore:
        root = _root(_absolute_root(self.root))
        if not os.path.lexists(root / ".corinthos"):
            raise WorkError("Initialize or open the project before creating Work Items.")
        private = private_directory(root)
        if not os.path.lexists(private / "state.db") and (
            not create or _read_profile(private / "bootstrap.json") is None
        ):
            raise WorkError("Initialize or open the project before changing Work Items.")
        return ContractStore(root)

    @contextmanager
    def _write(self, *, create: bool = False) -> Iterator[WorkRepository]:
        with self._write_store(create=create)._transaction() as connection:
            yield WorkRepository(connection)

    @staticmethod
    def project_identity(
        connection: sqlite3.Connection | None, *, schema_version: int | None
    ) -> str | None:
        """Return a single known local identity for a verified database snapshot."""
        if connection is None or schema_version is None:
            return None
        contracts, work = WorkRepository(connection).project_ids(schema_version)
        ids = set(contracts) | set(work)
        return ids.pop() if len(ids) == 1 else None

    @staticmethod
    def _create_project(
        repository: WorkRepository, project_id: str | None, parent: str | None
    ) -> str:
        if parent is not None:
            parent_project = repository.row(parent)["project_id"]
            if project_id is not None and project_id != parent_project:
                raise WorkError("The parent Work Item belongs to another project.")
            return parent_project
        contracts, work = repository.project_ids(ContractStore.current_schema_version())
        known = set(contracts) | set(work)
        if project_id is not None:
            if project_id not in known:
                raise WorkError("The project identity is not known at this root.")
            return project_id
        if not known:
            return f"project-{uuid4().hex}"
        # Continue a bootstrap-only Work Item namespace if a first contract
        # later introduces a distinct canonical identity at the same root.
        if len(work) == 1 and len(contracts) <= 1:
            return work[0]
        if len(known) == 1:
            return known.pop()
        raise WorkError("Choose a project identity for this root.")

    @staticmethod
    def _mutable(
        repository: WorkRepository, work_item_id: str, expected_updated_at: str | None
    ) -> sqlite3.Row:
        row = repository.row(work_item_id)
        if expected_updated_at is not None and row["updated_at"] != expected_updated_at:
            raise ConflictError("The Work Item changed. Reload before retrying.")
        if not TRANSITIONS[WorkStatus(row["status"])]:
            raise WorkError("Terminal Work Items are immutable.")
        return row

    @staticmethod
    def _dependencies(
        repository: WorkRepository, work_item_id: str, project_id: str, values: tuple[str, ...]
    ) -> None:
        for target in values:
            if target == work_item_id:
                raise WorkError("A Work Item cannot depend on itself.")
            if repository.row(target)["project_id"] != project_id:
                raise WorkError("Work Item dependencies must belong to the same project.")
            if repository.has_dependency(target, work_item_id):
                raise WorkError("The Work Item dependency would create a cycle.")
        repository.replace_dependencies(project_id, work_item_id, values)

    @staticmethod
    def _modules(
        repository: WorkRepository, work_item_id: str, project_id: str, values: tuple[str, ...]
    ) -> None:
        current = {
            module.module_id: module for module in repository.modules(work_item_id, project_id)
        }
        modules = tuple(
            current[module_id]
            if module_id in current
            else AffectedModule(module_id, repository.module_reference(project_id, module_id))
            for module_id in values
        )
        repository.replace_modules(project_id, work_item_id, modules)

    def create(
        self,
        *,
        scope_level: str,
        objective: str,
        acceptance_criteria: list[str] | tuple[str, ...],
        parent_work_item_id: str | None = None,
        validation_policy: str = "STANDARD",
        affected_modules: list[str] | tuple[str, ...] = (),
        depends_on: list[str] | tuple[str, ...] = (),
        actor_id: str = "local-operator",
        project_id: str | None = None,
    ) -> WorkItem:
        scope = _choice(ScopeLevel, scope_level, "Work Item scope")
        objective = _text(objective, "objective", MAX_OBJECTIVE_CHARS)
        criteria = _criteria(acceptance_criteria)
        if parent_work_item_id is not None:
            _id(parent_work_item_id, "parent Work Item ID", _WORK_ID)
        if project_id is not None:
            _id(project_id, "project ID")
        policy = _policy(validation_policy)
        actor = _id(actor_id, "Work Item creator ID")
        modules = _references(affected_modules, "affected module ID", MAX_MODULES)
        dependencies = _references(depends_on, "dependency ID", MAX_DEPENDENCIES, _WORK_ID)
        with self._write(create=True) as repository:
            project = self._create_project(repository, project_id, parent_work_item_id)
            item_id = f"work-{uuid4().hex}"
            timestamp = _timestamp()
            repository.insert(
                (
                    item_id,
                    project,
                    parent_work_item_id,
                    scope,
                    objective,
                    criteria,
                    policy,
                    actor,
                    timestamp,
                    timestamp,
                )
            )
            self._dependencies(repository, item_id, project, dependencies)
            self._modules(repository, item_id, project, modules)
            return repository.item(repository.row(item_id))

    def get(self, work_item_id: str) -> WorkItem:
        _id(work_item_id, "Work Item ID", _WORK_ID)
        with self._read() as repository:
            if repository is None:
                raise WorkError("Work Item was not found in this project.")
            return repository.item(repository.row(work_item_id))

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        scope_level: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[WorkItem]:
        _page(limit, offset)
        if project_id is not None:
            _id(project_id, "project ID")
        status = None if status is None else _choice(WorkStatus, status, "Work Item status")
        scope_level = (
            None if scope_level is None else _choice(ScopeLevel, scope_level, "Work Item scope")
        )
        with self._read() as repository:
            if repository is None:
                return []
            return repository.list(
                project_id=project_id,
                status=status,
                scope_level=scope_level,
                limit=limit,
                offset=offset,
            )

    def update(
        self,
        work_item_id: str,
        *,
        objective=_UNSET,
        acceptance_criteria=_UNSET,
        validation_policy=_UNSET,
        parent_work_item_id=_UNSET,
        affected_modules=_UNSET,
        depends_on=_UNSET,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        _id(work_item_id, "Work Item ID", _WORK_ID)
        if objective is not _UNSET:
            objective = _text(objective, "objective", MAX_OBJECTIVE_CHARS)
        criteria = _UNSET if acceptance_criteria is _UNSET else _criteria(acceptance_criteria)
        if validation_policy is not _UNSET:
            validation_policy = _policy(validation_policy)
        if parent_work_item_id is not _UNSET and parent_work_item_id is not None:
            _id(parent_work_item_id, "parent Work Item ID", _WORK_ID)
        if affected_modules is not _UNSET:
            affected_modules = _references(affected_modules, "affected module ID", MAX_MODULES)
        if depends_on is not _UNSET:
            depends_on = _references(depends_on, "dependency ID", MAX_DEPENDENCIES, _WORK_ID)

        with self._write() as repository:
            row = self._mutable(repository, work_item_id, expected_updated_at)
            project = row["project_id"]
            if parent_work_item_id is not _UNSET and parent_work_item_id is not None:
                parent = repository.row(parent_work_item_id)
                if parent["project_id"] != project:
                    raise WorkError("The parent Work Item belongs to another project.")
                if repository.has_ancestor(parent_work_item_id, work_item_id):
                    raise WorkError("The parent Work Item would create a cycle.")
            if depends_on is not _UNSET:
                self._dependencies(repository, work_item_id, project, depends_on)
            if affected_modules is not _UNSET:
                self._modules(repository, work_item_id, project, affected_modules)
            repository.save_fields(
                work_item_id,
                parent=row["parent_work_item_id"]
                if parent_work_item_id is _UNSET
                else parent_work_item_id,
                objective=row["objective"] if objective is _UNSET else objective,
                criteria=row["acceptance_criteria"] if criteria is _UNSET else criteria,
                validation_policy=row["validation_policy"]
                if validation_policy is _UNSET
                else validation_policy,
                updated_at=_timestamp(row["updated_at"]),
            )
            return repository.item(repository.row(work_item_id))

    def transition(
        self,
        work_item_id: str,
        status: str,
        *,
        expected_updated_at: str | None = None,
        _guard=None,
    ) -> WorkItem:
        _id(work_item_id, "Work Item ID", _WORK_ID)
        status = _choice(WorkStatus, status, "Work Item status")
        with self._write() as repository:
            row = self._mutable(repository, work_item_id, expected_updated_at)
            if status not in TRANSITIONS[WorkStatus(row["status"])]:
                raise WorkError("Invalid Work Item status transition.")
            if _guard is not None:
                _guard(repository.connection, repository.item(row))
            timestamp = _timestamp(row["updated_at"])
            completed_at = (
                timestamp if status in {WorkStatus.COMPLETED, WorkStatus.CANCELLED} else None
            )
            repository.save_status(work_item_id, status, timestamp, completed_at)
            return repository.item(repository.row(work_item_id))

    def counts(
        self,
        project_id: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkCounts:
        if project_id is not None:
            _id(project_id, "project ID")
        if connection is not None:
            return WorkRepository(connection).counts(project_id)
        with self._read() as repository:
            return _empty_counts() if repository is None else repository.counts(project_id)

    def tree(
        self,
        *,
        parent_work_item_id: str | None = None,
        project_id: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[WorkTreeNode]:
        _page(limit, offset)
        if parent_work_item_id is not None:
            _id(parent_work_item_id, "parent Work Item ID", _WORK_ID)
        if project_id is not None:
            _id(project_id, "project ID")
        with self._read() as repository:
            if repository is None:
                if parent_work_item_id is not None:
                    raise WorkError("Work Item was not found in this project.")
                return []
            if parent_work_item_id is not None:
                parent = repository.row(parent_work_item_id)
                if project_id is not None and parent["project_id"] != project_id:
                    raise WorkError("The parent Work Item belongs to another project.")
            return repository.tree(parent_work_item_id, project_id, limit, offset)

    def dependencies(
        self,
        work_item_id: str,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> list[str]:
        _id(work_item_id, "Work Item ID", _WORK_ID)
        _page(limit, offset)
        with self._read() as repository:
            if repository is None:
                raise WorkError("Work Item was not found in this project.")
            repository.row(work_item_id)
            return repository.dependencies(work_item_id, limit, offset)

    def _change_relation(
        self,
        item: str,
        target: str,
        *,
        module: bool,
        remove: bool,
        expected_updated_at: str | None,
    ) -> WorkItem:
        # The optimistic token turns this read/replace into a compare-and-swap.
        current = self.get(item)
        if expected_updated_at is not None and expected_updated_at != current.updated_at:
            raise ConflictError("The Work Item changed. Reload before retrying.")
        field = "affected_modules" if module else "depends_on"
        values = (
            list(ref.module_id for ref in current.affected_modules)
            if module
            else list(current.dependencies)
        )
        if remove:
            if target not in values:
                raise WorkError("The Work Item relation does not exist.")
            values.remove(target)
        else:
            values.append(target)
        return self.update(item, **{field: values}, expected_updated_at=current.updated_at)

    def add_dependency(
        self,
        work_item_id: str,
        target: str,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        return self._change_relation(
            work_item_id,
            target,
            module=False,
            remove=False,
            expected_updated_at=expected_updated_at,
        )

    def remove_dependency(
        self,
        work_item_id: str,
        target: str,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        return self._change_relation(
            work_item_id,
            target,
            module=False,
            remove=True,
            expected_updated_at=expected_updated_at,
        )

    def set_modules(
        self,
        work_item_id: str,
        modules: list[str] | tuple[str, ...],
        *,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        return self.update(
            work_item_id,
            affected_modules=modules,
            expected_updated_at=expected_updated_at,
        )

    def add_module(
        self,
        work_item_id: str,
        module_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        return self._change_relation(
            work_item_id,
            module_id,
            module=True,
            remove=False,
            expected_updated_at=expected_updated_at,
        )

    def remove_module(
        self,
        work_item_id: str,
        module_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> WorkItem:
        return self._change_relation(
            work_item_id,
            module_id,
            module=True,
            remove=True,
            expected_updated_at=expected_updated_at,
        )
