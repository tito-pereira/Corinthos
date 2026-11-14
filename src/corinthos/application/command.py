# SPDX-License-Identifier: MPL-2.0
"""Passive project and Work Item observations for the Command workspace."""

import json
import os
from dataclasses import asdict
from pathlib import Path

from corinthos.application.workspace import _PAGE_BUDGET, MAX_OFFSET
from corinthos.context.redaction import sanitize_text
from corinthos.contracts import ContractError, ContractStore, IntegrityError
from corinthos.contracts.artifacts import private_directory
from corinthos.external_import.service import _INTENT, _read_intent
from corinthos.project_registry import ProjectRegistryService, display_name
from corinthos.project_state import _inspect_sources
from corinthos.project_status import _absolute_root, inspect_project
from corinthos.work import WorkService
from corinthos.work.adaptive import AdaptiveWorkflowService
from corinthos.work.repository import WorkRepository

MAX_COMMAND_PROJECTS = 10
_MAX_FOCUS = 3
_MAX_ATTENTION = 8
_MAX_VALIDATION_HISTORY = 64
_OBSERVATION_TIMEOUT = 0.25
_SESSION_FIELDS = (
    "session_id",
    "work_item_id",
    "project_id",
    "provider",
    "state",
    "run_generation",
    "cleanup_unconfirmed",
    "context_projection_id",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "reported_cost",
    "reported_cost_currency",
)
_VALIDATION_STATES = {"SATISFIED", "BLOCKED"}
_ATTENTION = {
    "IMPORT_INCOMPLETE": ("ACTION", "IMPORT", "OVERVIEW", "External import needs recovery."),
    "IMPORT_INVALID": ("BLOCKED", "IMPORT", "OVERVIEW", "External import state is unavailable."),
    "PROJECT_UNAVAILABLE": ("BLOCKED", "PROJECT", "OVERVIEW", "Project data is unavailable."),
    "WORK_ITEM_BLOCKED": ("BLOCKED", "WORK_ITEM", "WORK", "The Work Item is blocked."),
    "PLANNING_REQUIRED": ("ACTION", "WORK_ITEM", "WORK", "The exact Work Item requires planning."),
    "VALIDATION_REQUIRED": (
        "ACTION",
        "WORK_ITEM",
        "VALIDATION",
        "Review independent Validation for the Work Item.",
    ),
    "HUMAN_APPROVAL_REQUIRED": (
        "ACTION",
        "WORK_ITEM",
        "WORK",
        "The Work Item requires explicit human approval.",
    ),
    "CLEANUP_UNCONFIRMED": ("BLOCKED", "SESSION", "AGENTS", "Session cleanup is unconfirmed."),
    "SESSION_ENDED_WITH_ISSUE": (
        "ACTION",
        "SESSION",
        "AGENTS",
        "Inspect the terminal Agent Session.",
    ),
    "VALIDATION_RESULT_BLOCKED": (
        "BLOCKED",
        "VALIDATION",
        "VALIDATION",
        "The latest result reports a blocked gate.",
    ),
    "VALIDATION_RUN_PENDING": (
        "ACTION",
        "VALIDATION",
        "VALIDATION",
        "The latest Validation run needs inspection.",
    ),
    "VALIDATION_PLAN_STALE": (
        "ACTION",
        "VALIDATION",
        "VALIDATION",
        "The latest run refers to a previous Validation plan.",
    ),
    "PROJECT_REVIEW_PENDING": (
        "ACTION",
        "PROJECT_PLANNING",
        "OVERVIEW",
        "A Project decision needs operator review.",
    ),
    "WORKFLOW_REVIEW_PENDING": (
        "ACTION",
        "WORKFLOW_PLANNING",
        "OVERVIEW",
        "A Workflow decision needs operator review.",
    ),
}
_ATTENTION_PRIORITY = {"BLOCKED": 0, "ACTION": 1, "INFO": 2}


def _session_view(session):
    return None if session is None else {name: getattr(session, name) for name in _SESSION_FIELDS}


def _summary_validation(connection, project_id, latest):
    outcome = None
    if latest is not None and latest.result is not None:
        document = ContractStore._lookup(connection, latest.result)
        if document is None:
            raise IntegrityError("The stored Validation result is unavailable.")
        metadata, payload = document["metadata"], document["payload"]
        if (
            latest.state != "PUBLISHED"
            or metadata["provenance"]["engine"] != "VALIDATION"
            or payload["target_and_execution"]["validation_run_id"] != latest.run_id
            or payload["validation_plan_ref"] != latest.plan.to_json()
        ):
            raise IntegrityError("The stored Validation run and result do not match.")
        outcome = payload["gate_assessment"]["state"]
        if outcome not in _VALIDATION_STATES:
            raise IntegrityError("The stored Validation gate outcome is unavailable.")
    active_plans = (
        ContractStore.active_references(connection, project_id, "VALIDATION", limit=3)
        if project_id is not None
        else []
    )
    # The exact outcome is historical and scoped to its candidate. A SATISFIED
    # run does not verify project-wide or current Work Item completion.
    return {
        "state": "BLOCKED" if outcome == "BLOCKED" and latest.plan_is_active else "NOT_VERIFIED",
        "latest_run": asdict(latest) if latest is not None else None,
        "active_plan_refs": [reference.to_json() for reference in active_plans],
        "outcome": outcome,
    }


def _candidate_items(connection, active, limit):
    rows = connection.execute(
        """SELECT * FROM work_items
           WHERE status IN ('ACTIVE', 'BLOCKED', 'OPEN') OR work_item_id = ?
           ORDER BY CASE WHEN work_item_id = ? THEN 0 WHEN status = 'ACTIVE' THEN 1
                         WHEN status = 'BLOCKED' THEN 2 ELSE 3 END, created_at, work_item_id
           LIMIT ?""",
        (active, active, limit),
    )
    repository = WorkRepository(connection)
    return [repository.item(row) for row in rows]


def _focus(connection, adaptive, item):
    flow = {}
    if adaptive is not None:
        evidence = (
            item.scope_level in {"TASK", "CHANGE"}
            or connection.execute(
                """SELECT count(*) FROM (
                 SELECT 1 FROM audit_events e JOIN revisions USING (revision_id)
                 JOIN contracts USING (contract_id) WHERE project_id = ?
                 AND contract_type != 'EVIDENCE_REPORT' LIMIT ?
               )""",
                (item.project_id, _MAX_VALIDATION_HISTORY + 1),
            ).fetchone()[0]
            <= _MAX_VALIDATION_HISTORY
        )
        flow = adaptive._view(connection, item, evidence=evidence)
        if not evidence:
            # Scope, planning and session facts still apply. Do not present a
            # stage or next action influenced by omitted Validation evidence.
            flow = flow | {"stage": None, "next_actions": []}
    objective = sanitize_text(item.objective)
    return {
        "authority": "OPERATIONAL",
        "work_item_id": item.work_item_id,
        "project_id": item.project_id,
        "objective": "".join(
            character if character.isprintable() else " " for character in objective
        )[:180],
        "status": item.status,
        "scope_level": item.scope_level,
        "stage": flow.get("stage"),
        "blockers": flow.get("blockers", []),
        "next_actions": flow.get("next_actions", []),
        "validation_state": flow.get("validation_state", "NOT_VERIFIED"),
        "validation_result_refs": flow.get("validation_result_refs", [])[:3],
        "latest_session": flow.get("latest_session"),
        "latest_projection": flow.get("latest_projection"),
        "provider_continuity": flow.get("provider_continuity"),
    }, any(
        gate["id"] == "HUMAN_APPROVAL" and gate["state"] == "REQUIRED"
        for gate in flow.get("gates", [])
    )


def _observations(root, connection, schema):
    from corinthos.application.reviews import pending_reviews

    sources = _inspect_sources(WorkService(root), connection, schema)
    project_id = sources.project_id
    session_summary = sources.sessions
    active = session_summary.active if session_summary is not None else None
    sessions = None
    if session_summary is not None:
        sessions = {
            "active": _session_view(active),
            "latest_terminal": _session_view(session_summary.latest_terminal),
            "total": session_summary.total,
        }
    adaptive = AdaptiveWorkflowService(root) if schema >= 9 else None
    focus = (
        [
            _focus(connection, adaptive, item)
            for item in _candidate_items(
                connection,
                active.work_item_id if active is not None else None,
                _MAX_FOCUS,
            )
        ]
        if schema >= 7
        else []
    )
    work_attention = []
    for item, requires_human in focus:
        owner = item["work_item_id"]
        work_project = item["project_id"]
        if item["status"] == "BLOCKED":
            work_attention.append(("WORK_ITEM_BLOCKED", owner, work_project))
        if item["stage"] in {"PLANNING_REQUIRED", "VALIDATION_REQUIRED"}:
            work_attention.append((item["stage"], owner, work_project))
        if requires_human and item["status"] not in {"COMPLETED", "CANCELLED"}:
            work_attention.append(("HUMAN_APPROVAL_REQUIRED", owner, work_project))
    # A blocked Work Item remains visible even if three ongoing items fill focus.
    if schema >= 7:
        work_attention.extend(
            ("WORK_ITEM_BLOCKED", row["work_item_id"], row["project_id"])
            for row in connection.execute(
                """SELECT work_item_id, project_id FROM work_items WHERE status = 'BLOCKED'
                   ORDER BY created_at, work_item_id LIMIT 3"""
            )
        )
    projection = sources.context["latest_projection"] if sources.context else None
    validation = _summary_validation(connection, project_id, sources.validation)
    decisions = (
        pending_reviews(connection, limit=3, include_workflow=schema >= 3) if schema >= 2 else []
    )
    return (
        {
            "project_id": project_id,
            "active_project": sources.active_project.to_json() if sources.active_project else None,
            "active_workflow": sources.active_workflow.to_json()
            if sources.active_workflow
            else None,
            "external_import": sources.external_import,
            "work_counts": asdict(sources.work_items) if sources.work_items is not None else None,
            "focus_work_items": [item for item, _ in focus],
            "sessions": sessions,
            "validation": validation,
            "latest_context": {
                "projection_id": projection["projection_id"],
                "projection_digest": projection["projection_digest"],
                "work_item_id": projection["work_item_id"],
                "provider": projection["target_provider"],
            }
            if projection
            else None,
            "lifecycle": asdict(sources.lifecycle) if sources.lifecycle else None,
        },
        work_attention,
        decisions,
    )


def _import_status(root, available, pointer):
    if pointer is not None:
        return "IMPORTED" if available else "INVALID"
    path = Path(root)
    if not os.path.lexists(path / ".corinthos"):
        return None
    try:
        intent_path = private_directory(path) / _INTENT
    except (ContractError, OSError):
        return None
    if not os.path.lexists(intent_path):
        return None
    try:
        return "INCOMPLETE" if available and _read_intent(path) is not None else "INVALID"
    except (ContractError, OSError, TypeError, ValueError):
        return "INVALID"


def _attention(card, work_attention, decisions):
    items = list(work_attention)
    project_id = card["project_id"]
    if card["error"]:
        items.append(("PROJECT_UNAVAILABLE", project_id, project_id))
    if card["import_status"] in {"INCOMPLETE", "INVALID"}:
        items.append(("IMPORT_" + card["import_status"], project_id, project_id))
    if card["sessions"] is not None:
        for session in (card["sessions"]["active"], card["sessions"]["latest_terminal"]):
            if session is None:
                continue
            if session["cleanup_unconfirmed"]:
                items.append(("CLEANUP_UNCONFIRMED", session["session_id"], session["project_id"]))
            if session["state"] in {"FAILED", "TIMED_OUT", "INTERRUPTED"}:
                items.append(
                    ("SESSION_ENDED_WITH_ISSUE", session["session_id"], session["project_id"])
                )
    run = card["validation"]["latest_run"]
    if run is not None:
        if not run["plan_is_active"]:
            items.append(("VALIDATION_PLAN_STALE", run["run_id"], run["plan"]["project_id"]))
        elif card["validation"]["state"] == "BLOCKED":
            items.append(("VALIDATION_RESULT_BLOCKED", run["run_id"], run["plan"]["project_id"]))
        elif run["state"] in {"FROZEN", "RUNNING"}:
            items.append(("VALIDATION_RUN_PENDING", run["run_id"], run["plan"]["project_id"]))
    items.extend(
        (decision["kind"] + "_REVIEW_PENDING", decision["session_id"], decision["project_id"])
        for decision in decisions
    )
    seen = list(dict.fromkeys(items))
    seen.sort(key=lambda item: _ATTENTION_PRIORITY[_ATTENTION[item[0]][0]])
    return [
        {
            "root": card["root"],
            "project_id": owner_project,
            "kind": code,
            "severity": severity,
            "owner_type": owner_type,
            "owner_id": identifier,
            "reason_code": code,
            "summary": summary,
            "navigation": navigation,
            "action_ids": [],
            "authority": "DERIVED",
        }
        for code, identifier, owner_project in seen[:_MAX_ATTENTION]
        for severity, owner_type, navigation, summary in [_ATTENTION[code]]
    ]


class CommandService:
    """Combine existing project-owned read models without refreshing or migrating state."""

    def project(self, root, *, last_accessed_at=None):
        root = str(root)
        value = {
            "root": root,
            "display_name": display_name(root),
            "last_accessed_at": last_accessed_at,
            "availability": "UNAVAILABLE",
            "initialized": False,
            "storage": "UNVERIFIED",
            "schema_version": None,
            "project_id": None,
            "error": None,
            "active_project": None,
            "active_workflow": None,
            "external_import": None,
            "import_status": None,
            "work_counts": None,
            "focus_work_items": [],
            "sessions": None,
            "validation": {
                "state": "NOT_VERIFIED",
                "latest_run": None,
                "active_plan_refs": [],
                "outcome": None,
            },
            "latest_context": None,
            "lifecycle": None,
        }
        work_attention, decisions = [], []
        try:
            status, sources = inspect_project(
                root,
                read=lambda connection, schema: _observations(root, connection, schema),
                timeout=_OBSERVATION_TIMEOUT,
            )
            value.update(
                initialized=status.initialized,
                storage=status.storage,
                schema_version=status.schema_version,
            )
            if not status.exists:
                value.update(availability="MISSING", error="PROJECT_MISSING")
            elif not status.is_directory or status.storage in {"INVALID", "UNVERIFIED"}:
                value["error"] = "PROJECT_STATE_UNAVAILABLE"
            else:
                value["availability"] = "AVAILABLE"
                if sources is not None:
                    observations, work_attention, decisions = sources
                    value.update(observations)
            if status.is_directory:
                value["import_status"] = _import_status(
                    status.root, value["availability"] == "AVAILABLE", value["external_import"]
                )
        except (ContractError, OSError, ValueError, RuntimeError, KeyError, TypeError):
            value["error"] = "PROJECT_STATE_UNAVAILABLE"
        value["attention"] = _attention(value, work_attention, decisions)
        return value

    def overview(self, *, limit=MAX_COMMAND_PROJECTS, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_COMMAND_PROJECTS
            or type(offset) is not int
            or not 0 <= offset <= MAX_OFFSET
        ):
            raise ValueError("The Command project page is invalid.")
        records = ProjectRegistryService()._saved()
        projects, remaining = [], _PAGE_BUDGET
        for record in records[offset : offset + limit]:
            value = self.project(**record)
            size = (
                len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                + 1
            )
            if projects and size > remaining:
                break
            projects.append(value)
            remaining -= size
        end = offset + len(projects)
        return {
            "projects": projects,
            "limit": limit,
            "offset": offset,
            "next_offset": end if end < len(records) else None,
        }


def command_overview(*, limit=MAX_COMMAND_PROJECTS, offset=0):
    return CommandService().overview(limit=limit, offset=offset)


def command_project(root):
    registry = ProjectRegistryService()
    normalized = str(_absolute_root(root))
    try:
        last_accessed = registry.last_accessed_at(normalized)
    except (ContractError, OSError):
        last_accessed = None
    return CommandService().project(normalized, last_accessed_at=last_accessed)
