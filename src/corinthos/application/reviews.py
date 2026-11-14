# SPDX-License-Identifier: MPL-2.0
"""Observe frozen local decisions and defer an exactly selected review."""

import json
import re
from math import isfinite

from corinthos.application.workspace import _page, _page_args
from corinthos.context.redaction import redact, sanitize_text
from corinthos.contracts import ConflictError, ContractRef, ContractStore, IntegrityError
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.project_manager.approval import local_actor_id
from corinthos.project_manager.presentation import render_fields, render_review
from corinthos.project_manager.repository import SessionRepository, _read_snapshot
from corinthos.project_manager.service import ProjectManager, _validate_snapshot
from corinthos.project_status import _absolute_root, inspect_project
from corinthos.workflow.presentation import render_plan_review
from corinthos.workflow.repository import WorkflowRepository
from corinthos.workflow.service import WorkflowPlanner

_OWNERS = {
    "PROJECT": (SessionRepository._sessions_table, ProjectManager, "project approve"),
    "WORKFLOW": (WorkflowRepository._sessions_table, WorkflowPlanner, "workflow publish"),
}
_PENDING = ("AWAITING_EXPLICIT_APPROVAL", "READY_FOR_REVIEW", "NEEDS_CLARIFICATION", "DRAFT")
_EXACT = _PENDING[0]
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PRESENTATION_LIMIT = 28 * 1024
_DOCUMENT_BYTES = 96 * 1024


def _summary(kind, session_id, project_id, state, version, digest, expires_at):
    if (
        not all(
            isinstance(value, str) and ID_PATTERN.fullmatch(value)
            for value in (session_id, project_id)
        )
        or type(version) is not int
        or not 1 <= version <= 2**53 - 1
        or (digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)))
        or (
            expires_at is not None
            and (
                type(expires_at) not in {int, float} or not isfinite(expires_at) or expires_at <= 0
            )
        )
        or (state == _EXACT and (digest is None or expires_at is None))
    ):
        raise IntegrityError("The saved decision metadata is invalid.")
    return dict(
        kind=kind,
        session_id=session_id,
        project_id=project_id,
        state=state,
        version=version,
        digest=digest,
        expires_at=expires_at,
    )


def pending_reviews(connection, limit, offset=0, *, include_workflow=True):
    """Read bounded rows from the two existing decision owners within a snapshot."""
    queries = []
    for kind, (table, _, _) in _OWNERS.items():
        if kind == "WORKFLOW" and not include_workflow:
            continue
        queries.append(
            f"""SELECT '{kind}' AS kind, session_id, project_id, version,
                       json_extract(snapshot, '$.state') AS state,
                       json_extract(snapshot, '$.review.sha256') AS digest,
                       json_extract(snapshot, '$.review.expires_at') AS expires_at
                FROM {table}
                WHERE json_extract(snapshot, '$.state') IN (?, ?, ?, ?)"""
        )
    sql = " UNION ALL ".join(queries)
    rows = connection.execute(
        f"""SELECT * FROM ({sql}) ORDER BY CASE state
                    WHEN 'AWAITING_EXPLICIT_APPROVAL' THEN 0
                    WHEN 'READY_FOR_REVIEW' THEN 1
                    WHEN 'NEEDS_CLARIFICATION' THEN 2 ELSE 3 END,
                    kind, session_id LIMIT ? OFFSET ?""",
        (*(_PENDING * len(queries)), limit, offset),
    )
    return [_summary(**dict(row)) for row in rows]


def _inspect(root, read):
    status, result = inspect_project(root, read=read)
    if status.storage in {"INVALID", "UNVERIFIED"}:
        raise IntegrityError("The decision state is unavailable.")
    return result


def review_list(root, *, limit=10, offset=0):
    _page_args(limit, offset)
    rows = _inspect(
        root,
        lambda connection, schema: (
            pending_reviews(connection, limit, offset, include_workflow=schema >= 3)
            if schema >= 2
            else []
        ),
    )
    return _page("reviews", rows or [], limit, offset)


def _get(connection, schema, kind, session_id):
    table, _, _ = _OWNERS[kind]
    if schema < (2 if kind == "PROJECT" else 3):
        raise IntegrityError("The saved decision is unavailable.")
    row = connection.execute(
        f"SELECT * FROM {table} WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise IntegrityError("The saved decision is unavailable.")
    snapshot = _read_snapshot(row)
    # Workflow validation here is pure. Constructing a planner opens a writable store.
    if kind == "PROJECT":
        return _validate_snapshot(snapshot)
    return WorkflowPlanner._snapshot(snapshot)


def _inputs(kind, session_id):
    if (
        not isinstance(kind, str)
        or kind not in _OWNERS
        or not isinstance(session_id, str)
        or not ID_PATTERN.fullmatch(session_id)
    ):
        raise ValueError("Select an exact saved decision.")


def _facts(kind, snapshot):
    review = snapshot["review"] or {}
    return _summary(
        kind,
        snapshot["session_id"],
        snapshot["project_id"],
        snapshot["state"],
        snapshot["version"],
        review.get("sha256"),
        review.get("expires_at"),
    )


def _prior(connection, metadata):
    reference = metadata.get("supersedes")
    if reference is None:
        return None
    document = ContractStore._lookup(connection, ContractRef.from_json(reference))
    if document is None:
        raise IntegrityError("The reviewed predecessor is unavailable.")
    return document


def _render(connection, kind, snapshot):
    review = snapshot["review"]
    if snapshot["state"] != _EXACT or review is None:
        return None, []
    documents = [review["document"]] if kind == "PROJECT" else review["documents"]
    previous = [_prior(connection, document["metadata"]) for document in documents]
    feasibility = snapshot["feasibility"] if kind == "PROJECT" else None
    source = (
        ContractStore._lookup(connection, ContractRef.from_json(snapshot["source_prr"]))
        if kind == "PROJECT" and snapshot["source_prr"] is not None
        else None
    )
    if kind == "PROJECT" and snapshot["source_prr"] is not None and source is None:
        raise IntegrityError("The exact source revision request is unavailable.")
    review_data = [documents, previous, feasibility, source]
    if len(canonical_json(review_data).encode("utf-8")) > _DOCUMENT_BYTES:
        return None, []
    _, categories = redact(review_data)
    if categories:
        return None, list(categories)
    if kind == "PROJECT":
        presentation = render_review(
            documents[0],
            base_document=previous[0],
            sha256=review["sha256"],
            expires_at=review["expires_at"],
            warnings=("Technical feasibility and semantic completeness require human review.",),
        )
        if source is not None:
            presentation = (
                "Source Project Revision Request\n"
                + render_fields(source, 2)
                + "\n\n"
                + presentation
            )
        presentation += "\n\nRecorded preliminary feasibility (human claims)\n" + render_fields(
            feasibility, 2
        )
    else:
        presentation = render_plan_review(
            snapshot, previous=[item for item in previous if item is not None]
        )
    if sanitize_text(presentation) != presentation:
        return None, ["REVIEW_REQUIRES_TERMINAL"]
    if len(json.dumps(presentation, ensure_ascii=True)) > _PRESENTATION_LIMIT:
        return None, []
    return presentation, []


def review_get(root, kind, session_id):
    _inputs(kind, session_id)

    def read(connection, schema):
        snapshot = _get(connection, schema, kind, session_id)
        presentation, redactions = _render(connection, kind, snapshot)
        return {
            "review": _facts(kind, snapshot),
            "presentation": presentation,
            "redactions": redactions,
        }

    result = _inspect(root, read)
    if result is None:
        raise IntegrityError("The saved decision is unavailable.")
    action = _OWNERS[kind][2]
    # The root is a separate display field so it is never interpolated into a shell command.
    return {
        **result,
        "approve_command": f"corinthos {action} {session_id}",
        "can_defer": result["review"]["state"] == _EXACT,
    }


def review_defer(root, kind, session_id, version, digest):
    _inputs(kind, session_id)
    if (
        type(version) is not int
        or not 1 <= version <= 2**53 - 1
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise ValueError("Supply the exact review version and digest.")
    root = _absolute_root(root)
    if ContractStore.inspect_existing(root) != ContractStore.current_schema_version():
        raise IntegrityError("Open the current project before deferring the review.")
    current = _inspect(root, lambda connection, schema: _get(connection, schema, kind, session_id))
    if current is None:
        raise IntegrityError("The saved decision is unavailable.")
    selected = _facts(kind, current)
    if (
        selected["state"] != _EXACT
        or selected["version"] != version
        or selected["digest"] != digest
    ):
        raise ConflictError("The exact review changed. Read it again.")
    manager = _OWNERS[kind][1](root)
    return _facts(kind, manager.defer(session_id, version, actor_id=local_actor_id()))
