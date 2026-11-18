# SPDX-License-Identifier: MPL-2.0
"""Phase 2 application operations shared by the CLI and the desktop RPC."""

import json
from dataclasses import asdict

from corinthos.application import project_status
from corinthos.contracts import ContractStore, StoreError

_PAGE_BUDGET = 48 * 1024
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_OFFSET = 1_000_000
_PROJECT_SUMMARY_FIELDS = (
    "root",
    "exists",
    "is_directory",
    "initialized",
    "storage",
    "schema_version",
    "display_name",
    "last_accessed_at",
    "project_id",
    "active_project",
    "active_workflow",
    "modules",
    "technology_decisions",
    "validation",
    "lifecycle",
    "work_items",
    "unavailable_sources",
)


def _page_args(limit, offset):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("The page limit must be between 1 and 25.")
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise ValueError("The page offset must be between 0 and 1000000.")


def _page(key, records, limit, offset):
    """Reserve room for the RPC envelope and worst-case escaping of its request ID."""
    items = []
    remaining = _PAGE_BUDGET
    for record in records:
        value = record if isinstance(record, (str, dict)) else asdict(record)
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if items and size > remaining:
            break
        items.append(value)
        remaining -= size
    return {
        key: items,
        "limit": limit,
        "offset": offset,
        "next_offset": (
            offset + len(items)
            if (len(items) < len(records) or len(records) == limit)
            and offset + len(items) <= MAX_OFFSET
            else None
        ),
    }


def projects_list(*, limit=DEFAULT_LIMIT, offset=0):
    from corinthos.project_registry import ProjectRegistryService

    _page_args(limit, offset)
    records = ProjectRegistryService().list(limit=limit, offset=offset)
    return _page("projects", records, limit, offset)


def project_summary(root):
    from corinthos.project_state import ProjectStateService

    result = asdict(ProjectStateService().summary(root))
    return {key: result[key] for key in _PROJECT_SUMMARY_FIELDS}


def project_open(root):
    """Record explicit access, migrating a verified older project before returning its summary."""
    from corinthos.project_registry import ProjectRegistryService

    status = project_status(root)
    if not status.is_directory:
        raise StoreError("The project directory is unavailable.")
    if status.storage in {"INVALID", "UNVERIFIED"}:
        raise StoreError("The project state is unavailable.")
    if status.schema_version in {6, 7, 8}:
        ContractStore(status.root)
    ProjectRegistryService().remember(status.root)
    return project_summary(status.root)


def project_create(root, display_name):
    """Create/register private state without publishing project identity or authority."""
    from corinthos.project_creation import create_project

    return create_project(root, display_name)


def project_inspect(root):
    """Passively classify a desktop onboarding selection."""
    from .onboarding import inspect_project

    return inspect_project(root)


def project_open_checked(root, inspection_digest, *, migrate=False):
    """Execute a current inspected selection with explicit migration consent."""
    from .onboarding import open_checked

    return open_checked(root, inspection_digest, migrate=migrate)


def _work(root):
    from corinthos.work import WorkService

    return WorkService(root)


def _work_item(item):
    from corinthos.work.model import TRANSITIONS

    data = asdict(item)
    for key in ("acceptance_criteria", "affected_modules", "dependencies"):
        data[key] = list(data[key])
    data["allowed_transitions"] = sorted(TRANSITIONS[item.status])
    return data


def work_create(root, *, scope_level, objective, acceptance_criteria, **options):
    return _work_item(
        _work(root).create(
            scope_level=scope_level,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            **options,
        )
    )


def work_get(root, work_item_id):
    return _work_item(_work(root).get(work_item_id))


def work_list(root, *, scope_level=None, status=None, limit=DEFAULT_LIMIT, offset=0):
    _page_args(limit, offset)
    items = _work(root).list(scope_level=scope_level, status=status, limit=limit, offset=offset)
    return _page("items", [_work_item(item) for item in items], limit, offset)


def work_update(root, work_item_id, **changes):
    return _work_item(_work(root).update(work_item_id, **changes))


def work_status(root, work_item_id, status, *, expected_updated_at=None):
    return _work_item(
        _work(root).transition(work_item_id, status, expected_updated_at=expected_updated_at)
    )


def work_tree(root, *, parent_work_item_id=None, limit=DEFAULT_LIMIT, offset=0):
    _page_args(limit, offset)
    nodes = _work(root).tree(parent_work_item_id=parent_work_item_id, limit=limit, offset=offset)
    return _page("nodes", nodes, limit, offset)


def work_dependencies(root, work_item_id, *, limit=DEFAULT_LIMIT, offset=0):
    _page_args(limit, offset)
    ids = _work(root).dependencies(work_item_id, limit=limit, offset=offset)
    return _page("dependency_ids", ids, limit, offset)
