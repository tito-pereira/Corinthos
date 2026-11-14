# SPDX-License-Identifier: MPL-2.0
"""SQLite queries for the Work Item service within its project snapshot."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from heapq import heapify, heappop, heappush

from corinthos.contracts import ContractRef, IntegrityError
from corinthos.contracts.store import _REVISIONS, _stored_document

from .model import (
    AffectedModule,
    ScopeLevel,
    WorkCounts,
    WorkError,
    WorkItem,
    WorkProvenance,
    WorkStatus,
    WorkTreeNode,
)

_MAX_DEPENDENCY_GRAPH = 10_000


class WorkRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def project_ids(self, schema_version: int) -> tuple[list[str], list[str]]:
        canonical = [
            row[0]
            for row in self.connection.execute(
                """SELECT DISTINCT project_id FROM contracts
                   WHERE contract_type = 'PROJECT' AND active_revision_id IS NOT NULL
                   ORDER BY project_id"""
            )
        ]
        existing = (
            [
                row[0]
                for row in self.connection.execute(
                    "SELECT DISTINCT project_id FROM work_items ORDER BY project_id"
                )
            ]
            if schema_version >= 7
            else []
        )
        if schema_version >= 9:
            from corinthos.external_import.storage import imported_project_ids

            existing = sorted(set(existing) | set(imported_project_ids(self.connection)))
        return canonical, existing

    def row(self, work_item_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise WorkError("Work Item was not found in this project.")
        return row

    def item(self, row: sqlite3.Row) -> WorkItem:
        work_item_id = row["work_item_id"]
        project_id = row["project_id"]
        criteria = json.loads(row["acceptance_criteria"])
        if (
            not isinstance(criteria, list)
            or not 1 <= len(criteria) <= 8
            or any(not isinstance(value, str) or not 1 <= len(value) <= 300 for value in criteria)
        ):
            raise IntegrityError("The saved Work Item acceptance criteria are invalid.")
        return WorkItem(
            work_item_id=work_item_id,
            project_id=project_id,
            parent_work_item_id=row["parent_work_item_id"],
            scope_level=ScopeLevel(row["scope_level"]),
            objective=row["objective"],
            acceptance_criteria=tuple(criteria),
            status=WorkStatus(row["status"]),
            validation_policy=row["validation_policy"],
            provenance=WorkProvenance(row["creator_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            affected_modules=self.modules(work_item_id, project_id),
            dependencies=self.direct_dependencies(work_item_id),
        )

    def list(
        self,
        *,
        project_id: str | None,
        status: WorkStatus | None,
        scope_level: ScopeLevel | None,
        limit: int,
        offset: int,
    ) -> list[WorkItem]:
        rows = self.connection.execute(
            """SELECT * FROM work_items
               WHERE (? IS NULL OR project_id = ?) AND (? IS NULL OR status = ?)
                 AND (? IS NULL OR scope_level = ?)
               ORDER BY created_at, work_item_id LIMIT ? OFFSET ?""",
            (project_id, project_id, status, status, scope_level, scope_level, limit, offset),
        )
        return [self.item(row) for row in rows]

    def insert(self, values: tuple) -> None:
        self.connection.execute(
            """INSERT INTO work_items (
                   work_item_id, project_id, parent_work_item_id, scope_level, objective,
                   acceptance_criteria, validation_policy, creator_id, created_at, updated_at, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            values,
        )

    def save_fields(
        self,
        work_item_id: str,
        *,
        parent: str | None,
        objective: str,
        criteria: str,
        validation_policy: str,
        updated_at: str,
    ) -> None:
        self.connection.execute(
            """UPDATE work_items SET parent_work_item_id = ?, objective = ?, acceptance_criteria = ?,
               validation_policy = ?, updated_at = ? WHERE work_item_id = ?""",
            (parent, objective, criteria, validation_policy, updated_at, work_item_id),
        )

    def save_status(
        self, work_item_id: str, status: WorkStatus, updated_at: str, completed_at: str | None
    ) -> None:
        self.connection.execute(
            """UPDATE work_items SET status = ?, updated_at = ?, completed_at = ?
               WHERE work_item_id = ?""",
            (status, updated_at, completed_at, work_item_id),
        )

    def save_scope(self, work_item_id: str, scope: ScopeLevel, updated_at: str) -> None:
        self.connection.execute(
            "UPDATE work_items SET scope_level = ?, updated_at = ? WHERE work_item_id = ?",
            (scope, updated_at, work_item_id),
        )

    def counts(self, project_id: str | None) -> WorkCounts:
        rows = self.connection.execute(
            """SELECT status, scope_level, COUNT(*) FROM work_items
               WHERE (? IS NULL OR project_id = ?) GROUP BY status, scope_level""",
            (project_id, project_id),
        )
        by_status = {status.value: 0 for status in WorkStatus}
        by_scope = {scope.value: 0 for scope in ScopeLevel}
        for status, scope, count in rows:
            by_status[status] += count
            by_scope[scope] += count
        return WorkCounts(sum(by_status.values()), by_status, by_scope)

    def has_ancestor(self, start: str, target: str) -> bool:
        return (
            self.connection.execute(
                """WITH RECURSIVE ancestors(id) AS (
                   SELECT ? UNION
                   SELECT work_items.parent_work_item_id FROM work_items
                   JOIN ancestors ON work_items.work_item_id = ancestors.id
                   WHERE work_items.parent_work_item_id IS NOT NULL
               ) SELECT 1 FROM ancestors WHERE id = ?""",
                (start, target),
            ).fetchone()
            is not None
        )

    def tree(
        self, parent: str | None, project_id: str | None, limit: int, offset: int
    ) -> list[WorkTreeNode]:
        # SQLite orders the recursive queue by path, giving depth-first delivery.
        # ORDER BY applies in the CTE so reading a page need not sort every row.
        rows = self.connection.execute(
            """WITH RECURSIVE branch(id, depth, path) AS (
                   SELECT work_item_id, 0, created_at || work_item_id FROM work_items
                   WHERE parent_work_item_id IS ? AND (? IS NULL OR project_id = ?)
                   UNION ALL
                   SELECT item.work_item_id, depth + 1,
                          path || '/' || item.created_at || item.work_item_id
                   FROM work_items AS item JOIN branch ON item.parent_work_item_id = branch.id
                   ORDER BY 3 LIMIT ?
               )
               SELECT item.*, branch.depth
               FROM branch CROSS JOIN work_items AS item ON item.work_item_id = branch.id
               LIMIT ? OFFSET ?""",
            (parent, project_id, project_id, limit + offset, limit, offset),
        )
        return [
            WorkTreeNode(
                row["work_item_id"],
                row["parent_work_item_id"],
                row["depth"],
                ScopeLevel(row["scope_level"]),
                WorkStatus(row["status"]),
                row["objective"],
            )
            for row in rows
        ]

    def has_dependency(self, start: str, target: str) -> bool:
        return (
            self.connection.execute(
                """WITH RECURSIVE dependencies(id) AS (
                   SELECT ? UNION
                   SELECT dependency_id FROM work_item_dependencies
                   JOIN dependencies ON work_item_id = dependencies.id
               ) SELECT 1 FROM dependencies WHERE id = ?""",
                (start, target),
            ).fetchone()
            is not None
        )

    def direct_dependencies(self, work_item_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """SELECT dependency_id FROM work_item_dependencies
               WHERE work_item_id = ? ORDER BY dependency_id LIMIT 17""",
            (work_item_id,),
        ).fetchall()
        if len(rows) > 16:
            raise IntegrityError("The saved Work Item has too many direct dependencies.")
        return tuple(row[0] for row in rows)

    def replace_dependencies(self, project_id: str, work_item_id: str, values: tuple[str, ...]):
        self.connection.execute(
            "DELETE FROM work_item_dependencies WHERE work_item_id = ?", (work_item_id,)
        )
        self.connection.executemany(
            "INSERT INTO work_item_dependencies VALUES (?, ?, ?)",
            ((project_id, work_item_id, target) for target in values),
        )

    def dependencies(self, work_item_id: str, limit: int, offset: int) -> list[str]:
        graph = self.connection.execute(
            """WITH RECURSIVE reachable(id) AS (
                   SELECT ? UNION
                   SELECT dependency_id FROM work_item_dependencies
                   JOIN reachable ON work_item_id = reachable.id LIMIT ?
               )
               SELECT id, dependency_id FROM reachable
               LEFT JOIN work_item_dependencies ON work_item_id = reachable.id""",
            (work_item_id, _MAX_DEPENDENCY_GRAPH + 1),
        )
        dependents = defaultdict(set)
        prerequisites: dict[str, set[str]] = {}
        for item, dependency in graph:
            adjacent = prerequisites.setdefault(item, set())
            if dependency is not None:
                adjacent.add(dependency)
                dependents[dependency].add(item)
        if len(prerequisites) > _MAX_DEPENDENCY_GRAPH:
            raise WorkError("The dependency view exceeds its graph limit.")
        ready = [item for item, adjacent in prerequisites.items() if not adjacent]
        heapify(ready)
        ordered = []
        while ready:
            item = heappop(ready)
            ordered.append(item)
            for dependent in dependents[item]:
                remainder = prerequisites[dependent]
                remainder.remove(item)
                if not remainder:
                    heappush(ready, dependent)
        if len(ordered) != len(prerequisites):
            raise IntegrityError("Saved Work Item dependencies contain a cycle.")
        # All reachable records are prerequisites of the requested item.
        assert ordered[-1] == work_item_id
        return ordered[:-1][offset : offset + limit]

    def modules(self, work_item_id: str, project_id: str) -> tuple[AffectedModule, ...]:
        rows = self.connection.execute(
            """SELECT * FROM work_item_modules WHERE work_item_id = ? ORDER BY module_id LIMIT 13""",
            (work_item_id,),
        ).fetchall()
        if len(rows) > 12:
            raise IntegrityError("The saved Work Item has too many affected modules.")
        return tuple(
            AffectedModule(
                row["module_id"],
                None
                if row["module_revision_id"] is None
                else ContractRef(
                    row["module_contract_id"], row["module_revision_id"], "MODULE", project_id
                ),
            )
            for row in rows
        )

    def module_reference(self, project_id: str, module_id: str) -> ContractRef | None:
        rows = self.connection.execute(
            _REVISIONS
            + """ WHERE project_id = ? AND contract_type = 'MODULE'
                 AND active_revision_id = revision_id
                 AND json_extract(document, '$.payload.module_id') = ?
                 ORDER BY contract_id LIMIT 2""",
            (project_id, module_id),
        ).fetchall()
        if len(rows) == 2:
            raise WorkError("The Module ID matches more than one active Module Contract.")
        return ContractRef.from_document(_stored_document(rows[0])) if rows else None

    def replace_modules(
        self, project_id: str, work_item_id: str, modules: tuple[AffectedModule, ...]
    ):
        self.connection.execute(
            "DELETE FROM work_item_modules WHERE work_item_id = ?", (work_item_id,)
        )
        self.connection.executemany(
            "INSERT INTO work_item_modules VALUES (?, ?, ?, ?, ?)",
            (
                (
                    project_id,
                    work_item_id,
                    module.module_id,
                    module.module_ref.contract_id if module.module_ref else None,
                    module.module_ref.revision_id if module.module_ref else None,
                )
                for module in modules
            ),
        )
