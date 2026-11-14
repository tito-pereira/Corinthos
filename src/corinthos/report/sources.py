# SPDX-License-Identifier: MPL-2.0
"""Capture exact report sources in one read transaction without source effects."""

import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from corinthos._paths import windows_component_is_reserved
from corinthos.contracts import ConflictError, ContractRef, ContractStore, IntegrityError
from corinthos.contracts.artifacts import _COMPONENT, is_link, private_directory
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.contracts.schema import validate_schema
from corinthos.validation.repository import ValidationRepository
from corinthos.workflow.verification import (
    RequiredGate,
    ValidationGateConsumer,
    _managed_plan,
    workspace_snapshot,
)

from .fileguard import prepare as _prepare_files
from .fileguard import require_file_guard as require_file_guard
from .lifecycle import project as project_lifecycle
from .rendering import safe_prose

_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_ROWS = 50_000
_MAX_EVIDENCE = 2_048
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
_MAX_LOG_BYTES = 1_048_576
_MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
_MAX_CANDIDATE_FILES = 8_192
_KINDS = {"CHECKPOINT", "MILESTONE", "PRODUCT", "HISTORY", "EVOLUTION"}
_SOURCE_QUERIES = {
    "lc_records": "SELECT * FROM lc_records WHERE project_id=? ORDER BY rowid",
    "lc_projects": "SELECT * FROM lc_projects WHERE project_id=? ORDER BY rowid",
    "lc_events": "SELECT * FROM lc_events WHERE project_id=? ORDER BY sequence",
    "contracts": "SELECT * FROM contracts WHERE project_id = ? AND contract_type != 'EVIDENCE_REPORT' ORDER BY rowid",
    "revisions": "SELECT r.* FROM revisions r JOIN contracts c USING(contract_id) WHERE c.project_id = ? AND c.contract_type != 'EVIDENCE_REPORT' ORDER BY r.rowid",
    "audit_events": "SELECT e.* FROM audit_events e JOIN revisions r USING(revision_id) JOIN contracts c USING(contract_id) WHERE c.project_id = ? AND c.contract_type != 'EVIDENCE_REPORT' ORDER BY e.sequence",
    "review_documents": (
        "SELECT r.*, e.sequence, e.occurred_at FROM revisions r "
        "JOIN contracts c USING(contract_id) JOIN audit_events e USING(revision_id) "
        "WHERE c.project_id = ? AND c.contract_type = 'EVIDENCE_REPORT' "
        "AND json_extract(r.document, '$.metadata.approval.kind') = 'HUMAN' "
        "AND json_extract(r.document, '$.payload.version_and_delivery_lineage.acceptance') "
        "IN ('ACCEPTED', 'WITH_RESERVATIONS') "
        "AND json_extract(r.document, '$.payload.feedback_and_acceptance.review_approval_id') "
        "= json_extract(r.document, '$.metadata.approval.approval_id') ORDER BY e.sequence"
    ),
    "pm_sessions": "SELECT * FROM pm_sessions WHERE project_id = ? ORDER BY rowid",
    "pm_turns": "SELECT t.* FROM pm_turns t JOIN pm_sessions s USING(session_id) WHERE s.project_id = ? ORDER BY t.rowid",
    "pm_approvals": "SELECT * FROM pm_approvals WHERE project_id = ? ORDER BY rowid",
    "wf_sessions": "SELECT * FROM wf_sessions WHERE project_id = ? ORDER BY rowid",
    "wf_turns": "SELECT t.* FROM wf_turns t JOIN wf_sessions s USING(session_id) WHERE s.project_id = ? ORDER BY t.rowid",
    "wf_approvals": "SELECT * FROM wf_approvals WHERE project_id = ? ORDER BY rowid",
    "wf_mandates": "SELECT * FROM wf_mandates WHERE project_id = ? ORDER BY rowid",
    "wf_executions": "SELECT * FROM wf_executions WHERE project_id = ? ORDER BY rowid",
    "wf_checkpoints": "SELECT p.* FROM wf_checkpoints p JOIN wf_executions e USING(execution_id) WHERE e.project_id = ? ORDER BY p.rowid",
    "wf_attempts": "SELECT a.* FROM wf_attempts a JOIN wf_executions e USING(execution_id) WHERE e.project_id = ? ORDER BY a.rowid",
    "wf_workspace_leases": "SELECT l.* FROM wf_workspace_leases l JOIN wf_executions e USING(execution_id) WHERE e.project_id = ? ORDER BY l.rowid",
    "wf_execution_events": "SELECT v.* FROM wf_execution_events v JOIN wf_executions e USING(execution_id) WHERE e.project_id = ? ORDER BY v.sequence",
    "val_plan_bindings": "SELECT b.* FROM val_plan_bindings b JOIN revisions r USING(revision_id) JOIN contracts c USING(contract_id) WHERE c.project_id = ? ORDER BY b.rowid",
    "val_runs": "SELECT v.* FROM val_runs v JOIN revisions r ON r.revision_id = v.plan_revision_id JOIN contracts c USING(contract_id) WHERE c.project_id = ? ORDER BY v.rowid",
    "val_events": "SELECT e.* FROM val_events e JOIN val_runs v USING(run_id) JOIN revisions r ON r.revision_id = v.plan_revision_id JOIN contracts c USING(contract_id) WHERE c.project_id = ? ORDER BY e.sequence",
}


def _digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _date(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise IntegrityError("Report cutoffs and source dates require a timezone.") from None


def _reference(value):
    return value if isinstance(value, ContractRef) else ContractRef.from_json(value)


def _identity(value):
    if type(value) is not dict or set(value) - {"commit", "build_id", "snapshot_id", "artifact_id"}:
        raise IntegrityError("A report needs an exact supported artifact identity.")
    for key, item in value.items():
        pattern = r"(?:[0-9a-f]{40}|[0-9a-f]{64})" if key == "commit" else ID_PATTERN
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise IntegrityError("The report artifact identity is invalid.")
    if value and not set(value).intersection({"commit", "build_id", "snapshot_id"}):
        raise IntegrityError("A report artifact needs a commit, build or snapshot identity.")
    return dict(value)


def _candidate_snapshot(root, store=None, identity=None):
    path = Path(root)
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise IntegrityError("A report candidate requires an ordinary existing directory.")
    sealed = identity is not None and "build_id" in identity
    inventory = workspace_snapshot(
        path.resolve(strict=True),
        max_bytes=_MAX_CANDIDATE_BYTES,
        max_files=_MAX_CANDIDATE_FILES,
        include_ignored=sealed,
    )
    if sealed:
        from corinthos.lifecycle.builds import BuildService

        verified = BuildService(store).identity_for_candidate(path, identity["build_id"])
        if verified != identity:
            raise IntegrityError("The sealed report candidate has another complete build identity.")
        inventory["artifact_identity"] = verified
    return inventory


class _ReadView(ContractStore):
    """Keep existing read APIs and the managed-run type boundary on a pinned connection."""

    def __init__(self, store, connection, cutoff, project_id):
        self._project_root = store.database_path.parent.parent
        self._database_path = store.database_path
        self.connection = connection
        self.cutoff = cutoff
        self.project_id = project_id
        self._verified_artifacts = set()
        self._artifact_bytes = 0

    @contextmanager
    def _connection(self, *, create=False):
        if create:
            raise IntegrityError("A report source view cannot create state.")
        yield self.connection

    @contextmanager
    def _transaction(self, *, create=False):
        raise IntegrityError("A report source view cannot write source state.")
        yield

    def list_events(self):
        return [
            {**dict(row), "event_type": "CONTRACT_PUBLISHED", "activated": bool(row["activated"])}
            for row in self.connection.execute(
                "SELECT sequence, e.revision_id, contract_id, contract_type, project_id, "
                "supersedes_revision_id AS previous_revision_id, activated, occurred_at "
                "FROM audit_events e JOIN revisions USING(revision_id) "
                "JOIN contracts USING(contract_id) WHERE project_id = ? "
                "AND contract_type != 'EVIDENCE_REPORT' ORDER BY sequence",
                (self.project_id,),
            )
            if _date(row["occurred_at"]) <= self.cutoff
        ]

    def verify_artifact(self, relative_path, digest):
        key = (relative_path, digest)
        if key in self._verified_artifacts:
            return
        if len(self._verified_artifacts) >= _MAX_EVIDENCE:
            raise IntegrityError("Current report proof exceeds its bounded reference budget.")
        reference = {"path": relative_path, "sha256": digest}
        path, size = _artifact_path(self, reference)
        if size > _MAX_EVIDENCE_BYTES - self._artifact_bytes:
            raise IntegrityError("Current report proof exceeds its bounded verification budget.")
        self._artifact_bytes += size
        _read_artifact(path, reference, size)
        self._verified_artifacts.add(key)


def _rows(connection, project_id):
    result, size, count = {}, 0, 0
    for table, query in _SOURCE_QUERIES.items():
        saved = []
        for row in connection.execute(query, (project_id,)):
            item = dict(row)
            count += 1
            size += len(canonical_json(item).encode("utf-8"))
            if count > _MAX_SOURCE_ROWS or size > _MAX_SOURCE_BYTES:
                raise IntegrityError("Report source capture exceeds its bounded record budget.")
            saved.append(item)
        result[table] = saved
    return result


def _watermark(rows):
    return {table: {"rows": len(items), "sha256": _digest(items)} for table, items in rows.items()}


def _historical_watermark(rows, cutoff, revision_ids):
    selected = {
        name: [row for row in rows[name] if _date(row["occurred_at"]) <= cutoff]
        for name in ("audit_events", "pm_turns", "wf_turns", "wf_execution_events", "val_events")
    }
    selected["val_plan_bindings"] = [
        row for row in rows["val_plan_bindings"] if row["revision_id"] in revision_ids
    ]
    selected["review_documents"] = [
        row for row in rows["review_documents"] if _review_before(row, cutoff)
    ]
    selected["lc_events"] = [
        row for row in rows["lc_events"] if _date(row["occurred_at"]) <= cutoff
    ]
    selected["lc_records"] = [
        {
            key: row[key]
            for key in (
                "record_id",
                "kind",
                "project_id",
                "logical_key",
                "origin",
                "origin_sha256",
                "created_at",
            )
        }
        for row in rows["lc_records"]
        if _date(row["created_at"]) <= cutoff
    ]
    return _watermark(selected)


def _review_before(row, cutoff):
    return (
        _date(row["occurred_at"]) <= cutoff
        and _date(json.loads(row["document"])["metadata"]["created_at"]) <= cutoff
    )


def _artifact_path(store, reference):
    relative = reference.get("path")
    if not isinstance(relative, str):
        raise IntegrityError("An evidence source needs a portable artifact path.")
    parts = relative.split("/")
    if any(
        not _COMPONENT.fullmatch(part) or part.endswith(".") or windows_component_is_reserved(part)
        for part in parts
    ):
        raise IntegrityError("An evidence source has an unsafe artifact path.")
    path = private_directory(store.database_path.parent.parent, "artifacts")
    for part in parts:
        path /= part
        info = path.lstat()
        if is_link(info):
            raise IntegrityError("Report evidence cannot follow links.")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise IntegrityError("Report evidence requires an unlinked regular file.")
    return path, info.st_size


def _read_artifact(path, reference, size):
    info = path.lstat()
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise IntegrityError("An artifact changed while opening the report source.")
        raw = stream.read(size + 1)
        if len(raw) != size or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise IntegrityError("An artifact changed during report inspection.")
    if sha256(raw).hexdigest() != reference["sha256"]:
        raise IntegrityError("The retained report source digest changed.")
    return raw


def _evidence_state(store, reference, *, max_bytes=_MAX_EVIDENCE_BYTES):
    size = 0
    try:
        path, size = _artifact_path(store, reference)
        if size > max_bytes:
            raise IntegrityError("Report evidence exceeds its bounded verification budget.")
        raw = _read_artifact(path, reference, size)
        result = {
            "ref": reference,
            "state": "VERIFIED",
            "reason": "Exact retained evidence digest verified.",
            "size": size,
        }
        plan = store.get(_reference(reference["execution_context"]["validation_plan_ref"]))
        if plan is not None and _managed_plan(plan):
            envelope = json.loads(raw)
            if canonical_json(envelope).encode("utf-8") != raw or any(
                envelope.get(key) != value
                for key, value in reference.items()
                if key not in {"path", "sha256"}
            ):
                raise IntegrityError("Managed evidence content has another source or execution.")
            observed = envelope.get("observations", {})
            command, review = observed.get("command", {}), observed.get("review", {})
            if type(command) is not dict or type(review) is not dict:
                raise IntegrityError("Managed evidence observations are malformed.")
            result["observation"] = {
                "command_backed": command.get("state") in {"CONFORMANT", "NONCONFORMANT"},
                "command_shared": "shared_observation_from" in command,
                "review_observed": review.get("state")
                in {"CONFORMANT", "NONCONFORMANT", "NOT_VERIFIED", "NOT_APPLICABLE"},
            }
        return result
    except FileNotFoundError:
        return {
            "ref": reference,
            "state": "UNAVAILABLE",
            "reason": "Referenced evidence is unavailable.",
            "size": size,
        }
    except (ValueError, KeyError, OSError, AttributeError, RecursionError):
        return {
            "ref": reference,
            "state": "CORRUPT",
            "reason": "Referenced evidence is unsafe, altered or unreadable.",
            "size": size,
        }


def _proof(store, results):
    references = {}
    for result in results:
        for item in result["payload"]["evidence_refs"]:
            prior = references.get(item["evidence_id"])
            if prior is not None and prior != item:
                raise IntegrityError(
                    "Validation sources reuse an evidence identity inconsistently."
                )
            references[item["evidence_id"]] = item
    if len(references) > _MAX_EVIDENCE:
        raise IntegrityError("Report evidence exceeds its bounded reference budget.")
    observations, size = [], 0
    for item in references.values():
        observation = _evidence_state(store, item, max_bytes=_MAX_EVIDENCE_BYTES - size)
        size += observation["size"]
        if size > _MAX_EVIDENCE_BYTES:
            raise IntegrityError("Report evidence exceeds its total verification budget.")
        observations.append(observation)
    return observations


def _log_summary(store, reference):
    try:
        path, size = _artifact_path(store, reference)
        if size > _MAX_LOG_BYTES:
            return None
        raw = _read_artifact(path, reference, size)
        text = raw.decode("utf-8").replace("\r\n", "\n")
        found = re.search(r"^Ran (\d+) tests? in [^\r\n]+$", text, re.MULTILINE)
        if found is not None:
            return {
                "framework": "unittest",
                "reported_tests": int(found[1]),
                "successful": bool(re.search(r"^OK\s*$", text, re.MULTILINE)),
            }
        found = re.search(r"\b(\d+) passed(?:, [^\r\n]+)? in \d", text)
        if found is not None:
            return {"framework": "pytest", "reported_passed": int(found[1])}
    except (ValueError, KeyError, OSError):
        pass
    return None


def _details(store, data, cache):
    result = {}
    for key in ("reason", "decision", "from_state", "to_state", "state", "provider"):
        if isinstance(data.get(key), str):
            result[key] = safe_prose(data[key])
    if type(data.get("artifact_identity")) is dict:
        result["artifact_identity"] = _identity(data["artifact_identity"])
    inspection = data.get("inspection", {})
    if type(inspection) is not dict:
        inspection = {}
    if isinstance(inspection.get("summary"), str):
        result["inspection"] = {"summary": safe_prose(inspection["summary"])}
    review = data.get("technical_review", {})
    if type(review) is dict and type(review.get("accepted")) is bool:
        result["technical_review"] = {
            "accepted": review["accepted"],
            "findings": [safe_prose(item) for item in review.get("findings", [])[:20]]
            if isinstance(review.get("findings", []), list)
            else ["Unrecognized technical review prose was omitted."],
        }
    checks = []
    recorded_checks = data.get("checks", [])
    for check in recorded_checks[:100] if isinstance(recorded_checks, list) else []:
        if type(check) is not dict:
            continue
        item = {
            key: check[key]
            for key in ("check_id", "state", "exit_code", "cleanup_confirmed")
            if key in check
        }
        reference = check.get("evidence")
        if type(reference) is dict and set(reference) >= {"path", "sha256"}:
            key = (reference["path"], reference["sha256"])
            if key not in cache:
                cache[key] = _log_summary(store, reference)
            item["source_artifact"] = {field: reference[field] for field in ("path", "sha256")}
            if cache[key] is not None:
                item["observed_log_summary"] = cache[key]
        checks.append(item)
    if checks:
        result["checks"] = checks
    for key in ("gates", "formal_gates"):
        if isinstance(data.get(key), list):
            result[key] = [
                {
                    field: row[field]
                    for field in ("validation_plan_ref", "result_ref", "state", "gate", "scope")
                    if field in row
                }
                for row in data[key][:100]
                if type(row) is dict
            ]
    return result


def _operations(store, rows, cutoff, historical):
    result, cache = [], {}
    for name in ("pm_turns", "wf_turns"):
        for row in rows[name]:
            if _date(row["occurred_at"]) <= cutoff:
                result.append(
                    {
                        "type": name.upper(),
                        **{
                            key: row[key]
                            for key in ("turn_id", "session_id", "version", "occurred_at")
                        },
                        "action": safe_prose(row["action"]),
                    }
                )
    for row in rows["wf_execution_events"]:
        if _date(row["occurred_at"]) <= cutoff and row["kind"] != "LEASE_RENEWED":
            data = json.loads(row["data"])
            result.append(
                {
                    "type": "WORKFLOW_EVENT",
                    **{
                        key: row[key]
                        for key in (
                            "sequence",
                            "event_id",
                            "execution_id",
                            "checkpoint_id",
                            "attempt_id",
                            "occurred_at",
                        )
                    },
                    "kind": safe_prose(row["kind"]),
                    "details": _details(store, data, cache),
                }
            )
    if not historical:
        for row in rows["wf_executions"]:
            result.append(
                {
                    "type": "WORKFLOW_EXECUTION",
                    **{key: row[key] for key in ("execution_id", "version", "state", "created_at")},
                    "workflow_ref": json.loads(row["workflow_ref"]),
                }
            )
        for row in rows["wf_checkpoints"]:
            result.append(
                {
                    "type": "WORKFLOW_CHECKPOINT",
                    **{
                        key: row[key]
                        for key in ("execution_id", "checkpoint_id", "state", "attempt_id")
                    },
                }
            )
    events = {}
    for row in rows["val_events"]:
        if _date(row["occurred_at"]) <= cutoff:
            events.setdefault(row["run_id"], []).append(row)
            if row["kind"] in {
                "RUN_CLAIMED",
                "AUDIT_STOPPED",
                "RESULT_FROZEN",
                "RESULT_PUBLISHED",
                "RUN_RECOVERED",
            }:
                data = json.loads(row["data"])
                result.append(
                    {
                        "type": "VALIDATION_EVENT",
                        **{key: row[key] for key in ("sequence", "run_id", "kind", "occurred_at")},
                        "details": _details(store, data, cache),
                    }
                )
    for row in rows["val_runs"]:
        observed = events.get(row["run_id"], [])
        if not observed:
            continue
        kinds = {event["kind"] for event in observed}
        state = (
            "PUBLISHED"
            if "RESULT_PUBLISHED" in kinds
            else "FROZEN"
            if "RESULT_FROZEN" in kinds
            else "RUNNING"
        )
        result.append(
            {
                "type": "VALIDATION_RUN",
                "run_id": row["run_id"],
                "plan_ref": json.loads(row["plan_ref"]),
                "previous_run_id": row["previous_run_id"],
                "state": state,
                "started_at": row["started_at"],
                "finished_at": row["finished_at"] if state != "RUNNING" else None,
                "observed_checks": sum(event["kind"] == "CHECK_OBSERVED" for event in observed),
                "result_revision_id": row["result_revision_id"] if state == "PUBLISHED" else None,
            }
        )
    return result


def _executions(rows, documents, cutoff, historical, observed_at):
    result = []
    for row in rows["wf_executions"]:
        if _date(row["created_at"]) > cutoff:
            continue
        reference = _reference(json.loads(row["workflow_ref"]))
        workflow = documents.get(reference.revision_id)
        if workflow is None or ContractRef.from_document(workflow) != reference:
            continue
        state, completed_at = row["state"], None
        source = "wf_executions"
        states = {item["checkpoint_id"]: "PENDING" for item in workflow["payload"]["checkpoints"]}
        events = [
            event
            for event in rows["wf_execution_events"]
            if event["execution_id"] == row["execution_id"]
            and _date(event["occurred_at"]) <= cutoff
        ]
        for event in events:
            kind, checkpoint = event["kind"], event["checkpoint_id"]
            data = json.loads(event["data"])
            if kind == "EXECUTION_RECONCILED":
                for identifier in data.get("preserved_checkpoint_ids", []):
                    if identifier in states:
                        states[identifier] = "COMPLETED"
            if checkpoint in states:
                if kind.startswith("ATTEMPT_") and kind.removeprefix("ATTEMPT_") in {
                    "VERIFYING",
                    "COMPLETED",
                    "BLOCKED",
                    "INTERRUPTED",
                }:
                    states[checkpoint] = kind.removeprefix("ATTEMPT_")
                elif kind == "ATTEMPT_CLAIMED":
                    states[checkpoint] = data.get("state", "UNKNOWN")
                elif kind == "CHECKPOINT_READY":
                    states[checkpoint] = "READY"
                elif kind == "CHECKPOINT_BLOCKED":
                    states[checkpoint] = "BLOCKED"
                elif kind == "RECOVERY_STARTED":
                    states[checkpoint] = "RECOVERING"
                elif kind == "RECOVERY_INSPECTED":
                    states[checkpoint] = data.get("to_state", "UNKNOWN")
            if completed_at is None and states and set(states.values()) == {"COMPLETED"}:
                completed_at = event["occurred_at"]
        if historical:
            state = "COMPLETED" if states and set(states.values()) == {"COMPLETED"} else "UNKNOWN"
            if any(event["kind"] == "EXECUTION_SUPERSEDED" for event in events):
                state = "SUPERSEDED"
            source = "wf_execution_events"
        result.append(
            {
                "execution_id": row["execution_id"],
                "workflow_ref": reference.to_json(),
                "state": state,
                "source": source,
                "observed_at": observed_at,
                "as_of": cutoff.isoformat(),
                "completion_recorded": completed_at is not None,
                "completed_at": completed_at,
            }
        )
    return result


def _closure(plan, documents, active, project):
    refs = plan["payload"]["source_contract_refs"]
    for raw in refs:
        ref = _reference(raw)
        if ref.revision_id not in documents or active.get(ref.contract_id) != raw:
            return False
        if ref.contract_type == "PROJECT" and ref != ContractRef.from_document(project):
            return False
    return True


def capture(
    store,
    project_ref,
    *,
    kind,
    artifact_identity=None,
    candidate_root=None,
    checkpoint_ids=(),
    milestone_id=None,
    as_of=None,
):
    """Collect a detached coherent source image and a restart-resolvable private manifest."""
    if not isinstance(store, ContractStore):
        raise IntegrityError("Report collection requires the existing ContractStore.")
    project_ref = _reference(project_ref)
    if project_ref.contract_type != "PROJECT" or kind not in _KINDS:
        raise IntegrityError("Choose a Project Contract and a supported report kind.")
    checkpoints = tuple(dict.fromkeys(checkpoint_ids))
    if any(not isinstance(item, str) or not ID_PATTERN.fullmatch(item) for item in checkpoints):
        raise IntegrityError("Report checkpoint identifiers are invalid.")
    if (
        kind == "CHECKPOINT"
        and not checkpoints
        or kind == "MILESTONE"
        and (not isinstance(milestone_id, str) or not ID_PATTERN.fullmatch(milestone_id))
    ):
        raise IntegrityError("A scoped report needs its exact checkpoint set or milestone.")
    inventory = (
        _candidate_snapshot(candidate_root, store, artifact_identity)
        if candidate_root is not None
        else None
    )
    identity = _identity(artifact_identity or (inventory["artifact_identity"] if inventory else {}))
    if inventory is not None and identity != inventory["artifact_identity"]:
        raise IntegrityError(
            "The report candidate differs from its requested full artifact identity."
        )
    with store._connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            connection.execute("SELECT count(*) FROM audit_events").fetchone()
            captured_at = datetime.now(UTC)
            cutoff = captured_at if as_of is None else _date(as_of)
            if cutoff > captured_at:
                raise IntegrityError("A report cannot claim a future source cutoff.")
            historical = as_of is not None
            view = _ReadView(store, connection, cutoff, project_ref.project_id)
            rows = _rows(connection, project_ref.project_id)
            publications = [
                event
                for event in view.list_events()
                if event["project_id"] == project_ref.project_id
            ]
            documents, active, metadata = {}, {}, []
            for event in publications:
                ref = ContractRef(
                    event["contract_id"],
                    event["revision_id"],
                    event["contract_type"],
                    event["project_id"],
                )
                document = view.get(ref)
                if document is None or _date(document["metadata"]["created_at"]) > cutoff:
                    continue
                documents[ref.revision_id] = document
                metadata.append(
                    {
                        "ref": ref.to_json(),
                        "sha256": _digest(document),
                        "publication_sequence": event["sequence"],
                        "published_at": event["occurred_at"],
                    }
                )
                if event["activated"]:
                    active[ref.contract_id] = ref.to_json()
            reviews, review_metadata = [], []
            for row in rows["review_documents"]:
                if not _review_before(row, cutoff):
                    continue
                ref = ContractRef.from_document(json.loads(row["document"]))
                document = view.get(ref)
                if document is None:
                    raise IntegrityError("A published human review source is unavailable.")
                reviews.append(document)
                review_metadata.append(
                    {
                        "ref": ref.to_json(),
                        "sha256": _digest(document),
                        "publication_sequence": row["sequence"],
                        "published_at": row["occurred_at"],
                    }
                )
            project = documents.get(project_ref.revision_id)
            if (
                project is None
                or ContractRef.from_document(project) != project_ref
                or active.get(project_ref.contract_id) != project_ref.to_json()
            ):
                raise IntegrityError(
                    "The requested Project revision is not the approved active source at this cutoff."
                )
            validate_schema(project)
            level = {"CHECKPOINT": "CHECKPOINT", "MILESTONE": "PLAYTEST_MILESTONE"}.get(
                kind, "PRODUCT"
            )
            ids = (
                set(checkpoints)
                if kind == "CHECKPOINT"
                else {milestone_id}
                if kind == "MILESTONE"
                else {project_ref.project_id}
            )
            workflow = next(
                (raw for raw in active.values() if raw["contract_type"] == "WORKFLOW"), None
            )
            target = project_ref.to_json() if level == "PRODUCT" else workflow
            plans = [
                document
                for document in documents.values()
                if document["metadata"]["contract_type"] == "VALIDATION"
                and active.get(document["metadata"]["contract_id"])
                == ContractRef.from_document(document).to_json()
                and document["payload"]["validation_scope"]["level"] == level
                and set(document["payload"]["validation_scope"]["target_ids"]) == ids
                and document["payload"]["validation_scope"]["target_contract_ref"] == target
            ]
            results = [
                document
                for document in documents.values()
                if document["metadata"]["contract_type"] == "VALIDATION_RESULT"
            ]
            selected, gates, selected_plans, gaps = [], [], [], []
            for plan in plans:
                ref = ContractRef.from_document(plan)
                matching = [
                    result
                    for result in results
                    if result["payload"]["validation_plan_ref"] == ref.to_json()
                    and result["payload"]["target_and_execution"]["artifact_identity"] == identity
                ]
                result = matching[-1] if matching else None
                gate = {
                    "plan_ref": ref.to_json(),
                    "result_ref": ContractRef.from_document(result).to_json() if result else None,
                    "recorded_state": result["payload"]["gate_assessment"]["state"]
                    if result
                    else None,
                    "eligibility": "NOT_VERIFIED",
                    "reason": "No published result covers this exact plan and whole candidate.",
                }
                if not _closure(plan, documents, active, project):
                    gate["reason"] = (
                        "The plan source closure is stale or incompatible with the report source cutoff."
                    )
                elif result is not None:
                    selected.append(result)
                    selected_plans.append(plan)
                    if historical:
                        gate.update(
                            eligibility=result["payload"]["gate_assessment"]["state"],
                            reason="Recorded scoped gate at the historical cutoff. Evidence availability is observed separately now.",
                        )
                        if _managed_plan(plan) and not ValidationRepository(view).is_published_run(
                            ref,
                            ContractRef.from_document(result),
                            result["payload"]["target_and_execution"]["validation_run_id"],
                        ):
                            gate.update(
                                eligibility="NOT_VERIFIED",
                                reason="The recorded result lacks its exact durable Validation execution.",
                            )
                    else:
                        observed = ValidationGateConsumer(view).consume(
                            RequiredGate(
                                ref,
                                ContractRef.from_document(result),
                                plan["payload"]["gate_policy"]["gate"],
                                tuple(plan["payload"]["validation_scope"]["target_ids"]),
                                _reference(
                                    plan["payload"]["validation_scope"]["target_contract_ref"]
                                ),
                            ),
                            identity,
                        )
                        valid_block = (
                            result["payload"]["gate_assessment"]["state"] == "BLOCKED"
                            and observed.get("reason")
                            == "The latest admissible Validation result blocks this gate."
                        )
                        gate.update(
                            eligibility="BLOCKED"
                            if valid_block
                            else "SATISFIED"
                            if observed["state"] == "SATISFIED"
                            else "NOT_VERIFIED",
                            reason=observed.get("reason", "Current scoped proof is unavailable."),
                        )
                gates.append(gate)
            evidence = _proof(view, selected)
            unavailable = {
                row["ref"]["evidence_id"] for row in evidence if row["state"] != "VERIFIED"
            }
            for gate in gates:
                if gate["result_ref"] is not None:
                    result = documents[gate["result_ref"]["revision_id"]]
                    if unavailable.intersection(
                        item["evidence_id"] for item in result["payload"]["evidence_refs"]
                    ):
                        gate.update(
                            eligibility="NOT_VERIFIED",
                            reason="Required retained evidence is unavailable or corrupt at this observation.",
                        )
            if not plans:
                gaps.append(
                    "No published Validation plan covers the exact requested report gate and target set."
                )
            if not identity:
                gaps.append("No exact report candidate artifact identity is available.")
            if not historical and inventory is None:
                gaps.append("Current candidate bytes were not inventoried for this report.")
                for gate in gates:
                    if gate["eligibility"] == "SATISFIED":
                        gate.update(
                            eligibility="NOT_VERIFIED",
                            reason="Current candidate bytes were not inventoried.",
                        )
            if historical:
                gaps.append(
                    "Historical Workflow current-state rows are not backdated. Immutable operation events describe the available history."
                )
            operations = _operations(view, rows, cutoff, historical)
            executions = _executions(rows, documents, cutoff, historical, captured_at.isoformat())
            lifecycle = project_lifecycle(rows, cutoff.isoformat())
            watermark = (
                _historical_watermark(rows, cutoff, documents) if historical else _watermark(rows)
            )
            candidate = (
                {"root": str(Path(candidate_root).resolve(strict=True)), "inventory": inventory}
                if inventory
                else None
            )
            if candidate is not None and "build_id" in identity:
                from corinthos.lifecycle.builds import BuildService

                build = BuildService(store).get(identity["build_id"])
                candidate["build_manifest"] = {
                    "path": build["origin"]["manifest_path"],
                    "sha256": build["origin"]["manifest_sha256"],
                    "limit": 4 * 1024 * 1024,
                }
            manifest = {
                "version": 1,
                "mode": "AS_OF" if historical else "CURRENT",
                "project_ref": project_ref.to_json(),
                "as_of": cutoff.isoformat(),
                "captured_at": captured_at.isoformat(),
                "source_documents": metadata,
                "review_documents": review_metadata,
                "source_watermark": watermark,
                "event_positions": {
                    name: max(
                        (
                            row["sequence"]
                            for row in rows[name]
                            if _date(row["occurred_at"]) <= cutoff
                        ),
                        default=0,
                    )
                    for name in (
                        "audit_events",
                        "wf_execution_events",
                        "val_events",
                        "review_documents",
                        "lc_events",
                    )
                },
                "candidate": candidate,
                "evidence": evidence,
                "operations": operations,
                "executions": executions,
                "gates": gates,
                "lifecycle": lifecycle,
            }
            requested_plans = {plan["metadata"]["revision_id"] for plan in plans}
            supporting = {}
            for result in results:
                plan_id = result["payload"]["validation_plan_ref"]["revision_id"]
                local_plan = documents.get(plan_id)
                if (
                    local_plan is not None
                    and plan_id not in requested_plans
                    and local_plan["payload"]["validation_scope"]["level"]
                    in {"MODULE", "INTEGRATION", "CHECKPOINT"}
                    and result["payload"]["target_and_execution"]["artifact_identity"] == identity
                ):
                    supporting[plan_id] = result
            snapshot = {
                "project": project,
                "documents": list(documents.values()),
                "review_documents": reviews,
                "active": active,
                "selected_results": selected,
                "selected_plans": selected_plans,
                "supporting_results": list(supporting.values()),
                "gates": gates,
                "evidence": evidence,
                "operations": operations,
                "executions": executions,
                "lifecycle": lifecycle,
                "gaps": gaps,
                "artifact_identity": identity,
                "as_of": cutoff.isoformat(),
                "captured_at": captured_at.isoformat(),
                "mode": manifest["mode"],
                "watermark": watermark,
                "manifest": manifest,
            }
        finally:
            connection.execute("ROLLBACK")
    if inventory is not None and _candidate_snapshot(candidate_root, store, identity) != inventory:
        raise ConflictError("The report candidate changed during source capture.")
    return snapshot


def prepare_file_guard(store, manifest, *, extra_files=()):
    """Verify bounded source bytes outside the writer and return stable metadata."""

    def verify():
        candidate = manifest.get("candidate")
        if (
            candidate is not None
            and _candidate_snapshot(
                candidate["root"], store, candidate["inventory"]["artifact_identity"]
            )
            != candidate["inventory"]
        ):
            raise ConflictError("The report candidate changed after capture.")
        observed = _proof(
            store, [{"payload": {"evidence_refs": [row["ref"] for row in manifest["evidence"]]}}]
        )
        if [(row["state"], row["size"]) for row in observed] != [
            (row["state"], row["size"]) for row in manifest["evidence"]
        ]:
            raise ConflictError("Required report evidence changed after capture.")

    build_manifest = (
        manifest.get("candidate", {}).get("build_manifest") if manifest.get("candidate") else None
    )
    return _prepare_files(
        store,
        manifest,
        verify,
        extra_files=tuple(extra_files) + ((build_manifest,) if build_manifest else ()),
    )


def require_unchanged(
    store, connection, snapshot_or_manifest, *, extra_files=(), ignored_record_ids=()
):
    """Recheck pinned source state before a writer reservation and return its file guard."""
    manifest = snapshot_or_manifest.get("manifest", snapshot_or_manifest)
    project_ref = _reference(manifest["project_ref"])
    rows = _rows(connection, project_ref.project_id)
    if ignored_record_ids:
        ignored = set(ignored_record_ids)
        actual = {row["record_id"]: row for row in rows["lc_records"]}
        if any(
            item not in actual or actual[item]["kind"] not in {"REVIEW", "OPERATION"}
            for item in ignored
        ):
            raise IntegrityError(
                "Review source exclusions require exact same-project review records."
            )
        rows["lc_records"] = [row for row in rows["lc_records"] if row["record_id"] not in ignored]
        rows["lc_events"] = [row for row in rows["lc_events"] if row["record_id"] not in ignored]
    watermark = (
        _watermark(rows)
        if manifest["mode"] == "CURRENT"
        else _historical_watermark(
            rows,
            _date(manifest["as_of"]),
            {source["ref"]["revision_id"] for source in manifest["source_documents"]},
        )
    )
    for table in ("lc_records", "lc_projects", "lc_events"):
        if table not in manifest["source_watermark"] and watermark.get(table, {}).get("rows") == 0:
            watermark.pop(table, None)
    if watermark != manifest["source_watermark"]:
        raise ConflictError(
            "Report sources changed after capture. Generate a new coherent snapshot."
        )
    for source in manifest["source_documents"] + manifest.get("review_documents", []):
        document = store._lookup(connection, _reference(source["ref"]))
        if document is None or _digest(document) != source["sha256"]:
            raise IntegrityError("A captured immutable report source is unavailable or altered.")
    view = _ReadView(store, connection, datetime.now(UTC), project_ref.project_id)
    return prepare_file_guard(view, manifest, extra_files=extra_files)
