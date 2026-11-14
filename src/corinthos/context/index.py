# SPDX-License-Identifier: MPL-2.0
"""Build a bounded, rebuildable source index from exact project owner records."""

import json
from collections import deque
from dataclasses import asdict
from itertools import islice

from corinthos.contracts import ContractRef, ContractStore, IntegrityError
from corinthos.contracts.artifacts import verify_artifact
from corinthos.contracts.model import iter_references
from corinthos.lifecycle.repository import LifecycleRepository
from corinthos.sessions.model import TERMINAL_STATES
from corinthos.validation.repository import ValidationRepository
from corinthos.work.repository import WorkRepository

from .model import MAX_CAPTURE_BYTES, ContextError, Source, canonical_bytes, digest
from .redaction import redact, sanitize_text, secret_path

_TERMINAL = tuple(sorted(TERMINAL_STATES))
_TERMINAL_SQL = ", ".join("?" for _ in _TERMINAL)
_PRIORITIES = {
    reason: order
    for order, reason in enumerate(
        (
            "WORK_ITEM",
            "PROJECT_BASE",
            "PARENT",
            "DEPENDENCY",
            "AFFECTED_MODULE",
            "WORKFLOW",
            "TECH_DECISION",
            "VALIDATION",
            "LIFECYCLE",
            "PRIOR_SESSION",
            "HANDOFF",
            "EVIDENCE",
            "IMPORT_REPOSITORY",
            "IMPORT_MANIFEST",
            "IMPORT_DOCUMENT",
            "IMPORT_LEXICAL",
            "IMPORT_RELATED_TEST",
        )
    )
}


def source_key(source, *, required_handoff_id=None):
    required = source.reason == "HANDOFF" and source.identity == required_handoff_id
    priority = _PRIORITIES["WORK_ITEM" if required else source.reason]
    distance = source.capture.get("distance", 0) if source.reason in {"PARENT", "DEPENDENCY"} else 0
    if source.reason.startswith("IMPORT_"):
        rank = source.capture.get("selector_rank", 0)
        if type(rank) is not int or rank < 0:
            raise ContextError("Stored imported Context has an invalid selector rank.")
        distance = rank
    return priority, required, distance, source.kind, source.identity, source.revision or ""


def _source(kind, authority, identity, revision, reason, raw):
    # A redacted identifier cannot be an exact durable identity. Keep the omission
    # explicit without preserving the sensitive value or hashing it.
    if sanitize_text(identity) != identity or (
        revision is not None and sanitize_text(revision) != revision
    ):
        if reason == "WORK_ITEM":
            raise ContextError("The Work Item identity is not admissible as Context.")
        return None
    clean, redaction = redact(raw)
    if len(canonical_bytes(clean)) > MAX_CAPTURE_BYTES:
        clean = {
            "excluded": "CAPTURE_BUDGET",
            "redacted_capture_digest": digest(clean),
            **({"distance": clean["distance"]} if "distance" in clean else {}),
        }
        redaction = tuple(sorted({*redaction, "CAPTURE_BUDGET"}))
    return Source(0, kind, authority, identity, revision, digest(clean), reason, redaction, clean)


def handoff_source(record):
    return _source(
        "SESSION_HANDOFF",
        "DERIVED",
        record.handoff_id,
        record.digest,
        "HANDOFF",
        record.canonical_payload,
    )


def session_events(connection, session_id, generation, limit):
    """Read the last exact normalized events of one generation, in sequence order."""
    rows = list(
        connection.execute(
            """SELECT sequence, kind, occurred_at, payload, native_kind FROM agent_session_events
               WHERE session_id = ? AND run_generation = ? ORDER BY sequence DESC LIMIT ?""",
            (session_id, generation, limit),
        )
    )
    try:
        return [
            {
                "sequence": row["sequence"],
                "kind": row["kind"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload"]),
                "native_kind": row["native_kind"],
            }
            for row in reversed(rows)
        ]
    except (ValueError, TypeError, RecursionError):
        raise ContextError("Recorded Agent Session observations are unreadable.") from None


def session_facts(row):
    """Whitelist observed session facts, omitting execution bindings and native IDs."""
    names = (
        "session_id",
        "work_item_id",
        "provider",
        "provider_version",
        "model",
        "state",
        "run_generation",
        "started_at",
        "finished_at",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "duration_ms",
        "reported_cost",
        "reported_cost_currency",
    )
    return {name: row[name] for name in names}


class ContextIndex:
    """Derive only structured links reachable from the selected Work Item."""

    def __init__(self, connection, repository):
        self.connection = connection
        self.repository = repository
        self.work = WorkRepository(connection)

    def _work_source(self, item, reason, distance=None):
        capture = asdict(item)
        if distance is not None:
            capture["distance"] = distance
        return _source(
            "WORK_ITEM", "OPERATIONAL", item.work_item_id, item.updated_at, reason, capture
        )

    def _related_work(self, item):
        parent = item.parent_work_item_id
        seen = {item.work_item_id}
        for distance in range(1, 7):
            if parent is None or parent in seen:
                break
            ancestor = self._same_project(item, parent)
            yield self._work_source(ancestor, "PARENT", distance)
            seen.add(parent)
            parent = ancestor.parent_work_item_id

        queue = deque((identity, 1) for identity in sorted(item.dependencies))
        visited = {item.work_item_id}
        remaining = 8
        while queue and remaining:
            identity, distance = queue.popleft()
            if identity in visited:
                continue
            dependency = self._same_project(item, identity)
            if identity not in seen:
                yield self._work_source(dependency, "DEPENDENCY", distance)
            visited.add(identity)
            remaining -= 1
            queue.extend((child, distance + 1) for child in sorted(dependency.dependencies))

    def _same_project(self, work, related):
        item = self.work.item(self.work.row(related))
        if item.project_id != work.project_id:
            raise ContextError("A Work Item relation crosses project boundaries.")
        return item

    def _document(self, ref):
        document = ContractStore._lookup(self.connection, ref)
        if document is None:
            raise ContextError("A selected exact contract revision is unavailable.")
        return document

    def _contract_source(self, ref, document, reason):
        capture = {"ref": ref.to_json(), "document": document}
        clean, categories = redact(capture)
        if not categories:
            row = self.connection.execute(
                "SELECT document_sha256 FROM revisions WHERE revision_id = ?", (ref.revision_id,)
            ).fetchone()
            clean["exact_document_sha256"] = row[0]
        # Keep the redaction categories associated with the full admissible capture.
        return (
            _source(
                ref.contract_type.value,
                "CANONICAL",
                ref.contract_id,
                ref.revision_id,
                reason,
                capture,
            )
            if categories
            else _source(
                ref.contract_type.value,
                "CANONICAL",
                ref.contract_id,
                ref.revision_id,
                reason,
                clean,
            )
        )

    def _active(self, project_id, kind, limit=1):
        return ContractStore.active_references(self.connection, project_id, kind, limit=limit)

    def _referencing(self, project_id, kind, revisions, limit):
        if not revisions:
            return []
        marks = ", ".join("?" for _ in revisions)
        rows = self.connection.execute(
            f"""SELECT DISTINCT c.contract_id, r.revision_id FROM contract_references AS links
                JOIN revisions AS r ON r.revision_id = links.source_revision_id
                JOIN contracts AS c ON c.contract_id = r.contract_id
                WHERE links.target_revision_id IN ({marks}) AND c.project_id = ?
                  AND c.contract_type = ?
                ORDER BY r.revision_id LIMIT ?""",
            (*sorted(revisions), project_id, kind, limit),
        )
        return [
            ContractRef(contract_id, revision_id, kind, project_id)
            for contract_id, revision_id in rows
        ]

    def _contracts(self, item):
        selected = {}

        def add(ref, reason):
            if ref.project_id != item.project_id:
                raise ContextError("A selected exact contract belongs to another project.")
            if ref not in selected:
                selected[ref] = (self._document(ref), reason)
            return selected[ref][0]

        for base in self._active(item.project_id, "PROJECT"):
            add(base, "PROJECT_BASE")
        modules = {module.module_id for module in item.affected_modules}
        exact_modules = []
        for module in item.affected_modules:
            if module.module_ref is not None:
                exact_modules.append(module.module_ref)
                add(module.module_ref, "AFFECTED_MODULE")
        for ref in self._active(item.project_id, "WORKFLOW"):
            workflow = self._document(ref)
            if any(row["module_id"] in modules for row in workflow["payload"]["modules"]):
                add(ref, "WORKFLOW")
        for ref in exact_modules:
            document = selected[ref][0]
            linked = sorted(
                iter_references(document["payload"]),
                key=lambda value: (value.contract_type, value.contract_id, value.revision_id),
            )
            for decision in [
                value for value in linked if value.contract_type == "TECHNOLOGY_DECISION"
            ][:8]:
                add(decision, "TECH_DECISION")

        relevant_revisions = {ref.revision_id for ref in selected}
        summary = ValidationRepository.project_summary(self.connection, item.project_id)
        if summary is not None and (
            relevant_revisions
            & {ref.revision_id for ref in iter_references(self._document(summary.plan)["payload"])}
        ):
            add(summary.plan, "VALIDATION")
            if summary.result is not None:
                add(summary.result, "VALIDATION")
            yield _source(
                "VALIDATION_RUN",
                "OPERATIONAL",
                summary.run_id,
                summary.result.revision_id if summary.result else summary.plan.revision_id,
                "VALIDATION",
                {
                    "run_id": summary.run_id,
                    "state": summary.state,
                    "plan": summary.plan.to_json(),
                    "result": summary.result.to_json() if summary.result else None,
                    "plan_is_active": summary.plan_is_active,
                },
            )
        for ref in self._active(item.project_id, "VALIDATION", limit=16):
            plan = self._document(ref)
            if relevant_revisions & {link.revision_id for link in iter_references(plan["payload"])}:
                add(ref, "VALIDATION")
        plans = {ref.revision_id for ref in selected if ref.contract_type == "VALIDATION"}
        for ref in self._referencing(item.project_id, "VALIDATION_RESULT", plans, 4):
            add(ref, "VALIDATION")
        results = {ref.revision_id for ref in selected if ref.contract_type == "VALIDATION_RESULT"}
        for ref in self._referencing(item.project_id, "EVIDENCE_REPORT", results, 4):
            add(ref, "EVIDENCE")

        for ref, (document, reason) in selected.items():
            yield self._contract_source(ref, document, reason)
        evidence = []
        for ref, (document, _) in sorted(selected.items(), key=lambda row: row[0].revision_id):
            payload = document["payload"]
            if ref.contract_type == "VALIDATION_RESULT":
                evidence.append((ref, payload["evidence_refs"]))
            elif ref.contract_type == "EVIDENCE_REPORT":
                evidence.append((ref, payload["artifact_and_evidence_index"]))

        items = (
            (ref, row)
            for ref, rows in evidence
            for row in sorted(rows, key=lambda item: item.get("evidence_id", ""))[:4]
        )
        yield from self._evidence(islice(items, 16))

    def _evidence(self, records):
        for ref, row in records:
            path, sha = row.get("path"), row.get("sha256")
            if not path or not sha:
                continue
            identity = row["evidence_id"]
            if secret_path(path) or sanitize_text(path) != path:
                yield _source(
                    "ARTIFACT",
                    "OPERATIONAL",
                    identity,
                    ref.revision_id,
                    "EVIDENCE",
                    {"path": path},
                )
                continue
            try:
                verify_artifact(self.repository.root, path, sha, max_bytes=2 * 1024 * 1024)
            except IntegrityError:
                yield _source(
                    "ARTIFACT",
                    "OPERATIONAL",
                    identity,
                    ref.revision_id,
                    "EVIDENCE",
                    {"path": path, "status": "UNAVAILABLE"},
                )
                continue
            yield _source(
                "ARTIFACT",
                "OPERATIONAL",
                identity,
                ref.revision_id,
                "EVIDENCE",
                {"path": path, "sha256": sha, "owner": ref.to_json(), "status": "VERIFIED"},
            )

    def _continuity(self, item, required_handoff):
        lifecycle = LifecycleRepository.project_summary(self.connection, item.project_id)
        if lifecycle is not None:
            yield _source(
                "LIFECYCLE",
                "OPERATIONAL",
                lifecycle.tracker_id,
                str(lifecycle.version),
                "LIFECYCLE",
                asdict(lifecycle),
            )
        row = self.connection.execute(
            f"""SELECT * FROM agent_sessions WHERE project_id = ? AND work_item_id = ?
                AND state IN ({_TERMINAL_SQL})
                ORDER BY finished_at DESC, session_id DESC LIMIT 1""",
            (item.project_id, item.work_item_id, *_TERMINAL),
        ).fetchone()
        if row is not None:
            capture = session_facts(row)
            observations = session_events(
                self.connection, row["session_id"], row["run_generation"], 4
            )
            cutoff = observations[-1]["sequence"] if observations else None
            capture["event_cutoff"] = cutoff
            capture["observations"] = observations
            yield _source(
                "AGENT_SESSION",
                "OPERATIONAL",
                row["session_id"],
                f"{row['run_generation']}:{cutoff}",
                "PRIOR_SESSION",
                capture,
            )
        if required_handoff is not None:
            yield required_handoff
            return
        handoffs = self.repository.list_handoffs(
            self.connection, work_item_id=item.work_item_id, session_id=None, limit=1, offset=0
        )
        if handoffs:
            from .handoff import verify_handoff

            yield handoff_source(verify_handoff(handoffs[0]))

    def select(self, work_item_id, *, required_handoff=None):
        item = self.work.item(self.work.row(work_item_id))
        if sanitize_text(item.project_id) != item.project_id:
            raise ContextError("The project identity is not admissible as Context.")
        candidates = (
            self._work_source(item, "WORK_ITEM"),
            *self._related_work(item),
            *self._contracts(item),
            *self._continuity(item, required_handoff),
        )
        identity = (item.project_id, item.work_item_id)
        return identity, sorted((row for row in candidates if row is not None), key=source_key)
