# SPDX-License-Identifier: MPL-2.0
"""Resumable technology negotiation and exact contract planning."""

from copy import deepcopy
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from math import isfinite
from time import time
from uuid import uuid4

from corinthos.contracts import ContractRef, ContractStore, validate_schema
from corinthos.contracts.model import ID_PATTERN, ConflictError, IntegrityError, canonical_json
from corinthos.contracts.semantics import validate_semantics

from .repository import WorkflowRepository

_CLOSED = {"PUBLISHED", "CANCELLED"}
_STATES = _CLOSED | {
    "DRAFT",
    "NEEDS_CLARIFICATION",
    "DEFERRED",
    "READY_FOR_REVIEW",
    "AWAITING_EXPLICIT_APPROVAL",
    "PUBLISHING",
    "CONFLICT",
}
_POLICY = {
    "scheduling_policy": "DEPENDENCY_AWARE",
    "validation_policy": "REQUIRED_GATES",
    "recovery_policy": "PERSISTENT_STATE",
    "build_policy": "CUMULATIVE_SINGLE_ACTIVE",
}
_QUALITY = {
    "M1": (
        "instructions",
        "Record objectives, responsibilities, interfaces, deliverables and acceptance criteria.",
    ),
    "M2": (
        "technology",
        "Trace implemented technologies to the exact approved decisions and constraints.",
    ),
    "M3": (
        "tests and security",
        "Exercise acceptance criteria, invalid inputs, edge cases and relevant security boundaries.",
    ),
    "M4": (
        "documentation",
        "Document installation, public interfaces, use and reproducible verification.",
    ),
    "M5": (
        "maintainability",
        "Review readable responsibilities, duplication, failure handling and appropriate resource use.",
    ),
}


def _id(prefix):
    return f"{prefix}-{uuid4().hex}"


def _identifier(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise IntegrityError("A planning item requires a valid stable identifier.")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
        raise IntegrityError("Enter nonblank planning text of at most 20,000 characters.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise IntegrityError("Planning text contains unsupported control characters.")
    return value.strip()


def _ids(values):
    if not isinstance(values, list) or len(values) > 200:
        raise IntegrityError("Provide at most 200 stable planning identifiers.")
    result = [_identifier(value) for value in values]
    if len(set(result)) != len(result):
        raise IntegrityError("Planning identifiers cannot be duplicated.")
    return result


def _fields(operation, required, optional=()):
    if not isinstance(operation, dict) or set(operation) - {"action", *required, *optional}:
        raise IntegrityError("The planning operation has invalid fields.")
    if not set(required) <= operation.keys():
        raise IntegrityError("The planning operation is missing required fields.")


def _order(records, dependencies, label):
    graph = {key: set(dependencies(record)) for key, record in records.items()}
    if any(not values <= graph.keys() for values in graph.values()):
        raise IntegrityError(f"The {label} graph contains an unknown dependency.")
    try:
        return list(TopologicalSorter(graph).static_order())
    except CycleError:
        raise IntegrityError(f"The {label} graph contains a dependency cycle.") from None


def _obligations(project):
    payload = project["payload"]
    requirements = {item["requirement_id"]: item for item in payload["requirements"]}
    criteria = {
        criterion["criterion_id"]: criterion
        for item in requirements.values()
        for criterion in item["acceptance_criteria"]
    }
    constraints = {item["constraint_id"]: item for item in payload["constraints"]}
    return requirements | criteria, constraints


def _envelope(
    kind,
    contract_id,
    project_id,
    payload,
    actor_id,
    session_id,
    *,
    base=None,
    approval_kind="HUMAN",
    status="PUBLISHED",
):
    return {
        "metadata": {
            "contract_id": contract_id,
            "revision_id": _id("revision"),
            "contract_type": kind,
            "project_id": project_id,
            "version": 1 if base is None else base["metadata"]["version"] + 1,
            "schema_version": "1.0",
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
            "supersedes": None if base is None else ContractRef.from_document(base).to_json(),
            "standard_ref": {"standard_id": "nukytera-standard", "version": "0.1"},
            "provenance": {
                "engine": "WORKFLOW",
                "actor_id": actor_id,
                "source": "Workflow planning session " + session_id,
            },
            "approval": {
                "kind": approval_kind,
                "approval_id": _id("approval"),
                "actor_id": actor_id,
            },
        },
        "payload": deepcopy(payload),
    }


class WorkflowPlanner:
    """Prepare inspectable plans while publication remains with trusted authorization."""

    def __init__(self, project_root, assistance=None):
        self.store = ContractStore(project_root)
        self.repository = WorkflowRepository(self.store)
        self.assistance = assistance

    def _project(self, reference):
        reference = (
            reference if isinstance(reference, ContractRef) else ContractRef.from_json(reference)
        )
        if reference.contract_type != "PROJECT":
            raise IntegrityError("Planning requires an exact approved Project Contract.")
        project = self.store.get(reference)
        if (
            project is None
            or project["metadata"]["approval"] is None
            or project["metadata"]["approval"]["kind"] != "HUMAN"
        ):
            raise IntegrityError("The approved Project Contract is unavailable.")
        active = self.store.resolve_active(reference.contract_id)
        if active is None or ContractRef.from_document(active) != reference:
            raise ConflictError("The Project Contract changed. Reconcile before planning.")
        return project

    @classmethod
    def _snapshot(cls, snapshot):
        try:
            required = {
                "session_id",
                "project_id",
                "workflow_contract_id",
                "version",
                "state",
                "project_ref",
                "base_ref",
                "authorization_version",
                "mandate_id",
                "review",
                "published_refs",
                "compiled_refs",
                "draft",
                "reason",
                "created_at",
                "used_ids",
            }
            if type(snapshot) is not dict or set(snapshot) != required:
                raise ValueError
            for key in ("session_id", "project_id", "workflow_contract_id"):
                _identifier(snapshot[key])
            if not isinstance(snapshot["state"], str) or snapshot["state"] not in _STATES:
                raise ValueError
            project_ref = ContractRef.from_json(snapshot["project_ref"])
            if (
                project_ref.contract_type != "PROJECT"
                or project_ref.project_id != snapshot["project_id"]
            ):
                raise ValueError
            if snapshot["base_ref"] is not None:
                base = ContractRef.from_json(snapshot["base_ref"])
                if (
                    base.contract_type != "WORKFLOW"
                    or base.project_id != snapshot["project_id"]
                    or base.contract_id != snapshot["workflow_contract_id"]
                ):
                    raise ValueError
            if type(snapshot["version"]) is not int or snapshot["version"] < 1:
                raise ValueError
            if snapshot["authorization_version"] is not None and (
                type(snapshot["authorization_version"]) is not int
                or snapshot["authorization_version"] < 1
            ):
                raise ValueError
            _text(snapshot["reason"])
            if datetime.fromisoformat(snapshot["created_at"]).tzinfo is None:
                raise ValueError
            if type(snapshot["used_ids"]) is not list or any(
                not isinstance(item, str) or not ID_PATTERN.fullmatch(item)
                for item in snapshot["used_ids"]
            ):
                raise ValueError
            if snapshot.get("mandate_id") is not None:
                _identifier(snapshot["mandate_id"])
            draft = snapshot["draft"]
            if type(draft) is not dict or set(draft) != {
                "modules",
                "decisions",
                "checkpoints",
                "playtest_milestones",
            }:
                raise ValueError
            if any(type(records) is not dict for records in draft.values()):
                raise ValueError
            for key, module in draft["modules"].items():
                if key != module["module_id"]:
                    raise ValueError
                cls._module_shape(snapshot, module)
            for key, decision in draft["decisions"].items():
                if (
                    key != decision["decision_id"]
                    or decision["payload"]["ai_recommendations"]
                    != decision["original_recommendations"]
                ):
                    raise ValueError
                _identifier(key)
                validate_schema(
                    _envelope(
                        "TECHNOLOGY_DECISION",
                        decision["contract_id"],
                        snapshot["project_id"],
                        decision["payload"],
                        "draft-check",
                        snapshot["session_id"],
                        status="DRAFT",
                    )
                )
            for key, row in draft["checkpoints"].items():
                if row["checkpoint_id"] != key:
                    raise ValueError
            for key, row in draft["playtest_milestones"].items():
                if row["milestone_id"] != key:
                    raise ValueError
            cls._workflow_shape(snapshot)
            for key in ("published_refs", "compiled_refs"):
                if not isinstance(snapshot[key], list):
                    raise ValueError
                for reference in snapshot[key]:
                    ref = ContractRef.from_json(reference)
                    if ref.project_id != snapshot["project_id"] or ref.contract_type not in {
                        "TECHNOLOGY_DECISION",
                        "MODULE",
                        "WORKFLOW",
                    }:
                        raise ValueError
            review = snapshot["review"]
            if review is not None:
                if set(review) != {"documents", "sha256", "expires_at"} or not isinstance(
                    review["documents"], list
                ):
                    raise ValueError
                if (
                    type(review["expires_at"]) not in (int, float)
                    or not isfinite(review["expires_at"])
                    or review["expires_at"] <= 0
                ):
                    raise ValueError
                if (
                    review["sha256"]
                    != sha256(canonical_json(review["documents"]).encode()).hexdigest()
                ):
                    raise ValueError
                for document in review["documents"]:
                    validate_schema(document)
                    if document["metadata"]["project_id"] != snapshot["project_id"] or document[
                        "metadata"
                    ]["contract_type"] not in {"TECHNOLOGY_DECISION", "MODULE", "WORKFLOW"}:
                        raise ValueError
            if snapshot["state"] in {"AWAITING_EXPLICIT_APPROVAL", "PUBLISHING"} and review is None:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            raise IntegrityError(
                "The stored planning state is invalid. Preserve it for recovery."
            ) from None
        return snapshot

    def get(self, session_id):
        snapshot = self._snapshot(self.repository.get(session_id))
        if snapshot["state"] == "PUBLISHING":
            from .authorization import recover_publication

            return recover_publication(self, snapshot)
        return snapshot

    def list(self):
        return [self._snapshot(snapshot) for snapshot in self.repository.list()]

    def history(self, session_id):
        self.get(session_id)
        turns = self.repository.history(session_id)
        for turn in turns:
            self._snapshot(turn["snapshot"])
        return turns

    def _start(self, project, actor_id, *, base=None, reason="Initial engineering plan"):
        snapshot = {
            "session_id": _id("workflow-session"),
            "project_id": project["metadata"]["project_id"],
            "workflow_contract_id": _id("workflow")
            if base is None
            else base["metadata"]["contract_id"],
            "state": "DRAFT",
            "project_ref": ContractRef.from_document(project).to_json(),
            "base_ref": None if base is None else ContractRef.from_document(base).to_json(),
            "authorization_version": None,
            "mandate_id": None,
            "review": None,
            "published_refs": [],
            "compiled_refs": [],
            "draft": {"modules": {}, "decisions": {}, "checkpoints": {}, "playtest_milestones": {}},
            "reason": _text(reason),
            "created_at": datetime.now(UTC).isoformat(),
            "used_ids": [],
        }
        return snapshot

    def start(self, project_ref, *, actor_id):
        snapshot = self._start(self._project(project_ref), actor_id)
        return self.repository.create(snapshot, actor_id=actor_id, action="PLANNING_STARTED")

    def _editable(self, session_id, expected_version):
        snapshot = self.get(session_id)
        if snapshot["version"] != expected_version or snapshot["state"] in _CLOSED:
            raise ConflictError("The planning session changed or is closed. Reload before editing.")
        self._project(snapshot["project_ref"])
        return snapshot

    @staticmethod
    def _claim(snapshot, supplied, prefix, current):
        identifier = _id(prefix) if supplied is None else _identifier(supplied)
        if identifier in snapshot["used_ids"] and identifier not in current:
            raise IntegrityError("A planning identifier cannot be reused for another item.")
        if identifier not in snapshot["used_ids"]:
            snapshot["used_ids"].append(identifier)
        return identifier

    def edit(self, session_id, expected_version, operation, *, actor_id):
        snapshot = self._editable(session_id, expected_version)
        handlers = {
            "upsert_module": self._upsert_module,
            "set_provided_interfaces": self._interfaces,
            "set_consumed_interfaces": self._interfaces,
            "set_module_technology": self._technology_links,
            "set_verification": self._verification,
            "reset_verification": self._verification,
            "upsert_technology": self._technology,
            "select_technology": self._selection,
            "upsert_checkpoint": self._row,
            "upsert_milestone": self._row,
            "remove_module": self._remove,
            "remove_technology": self._remove,
            "remove_checkpoint": self._remove,
            "remove_milestone": self._remove,
            "resume": lambda _snapshot, op: _fields(op, set()),
        }
        if (
            not isinstance(operation, dict)
            or not isinstance(operation.get("action"), str)
            or operation["action"] not in handlers
        ):
            raise IntegrityError("Choose a supported structured planning operation.")
        try:
            handlers[operation["action"]](snapshot, operation)
            snapshot.update(
                review=None,
                authorization_version=None,
                mandate_id=None,
                compiled_refs=[],
                state="DRAFT",
            )
            self._snapshot({**snapshot, "version": expected_version})
        except (KeyError, TypeError, AttributeError, RecursionError):
            raise IntegrityError("The planning operation has invalid structured fields.") from None
        return self.repository.save(
            session_id,
            expected_version,
            snapshot,
            actor_id=actor_id,
            action=operation["action"].upper(),
        )

    def _upsert_module(self, snapshot, operation):
        _fields(
            operation,
            {"purpose_and_boundaries", "requirement_ids"},
            {"module_id", "constraint_ids", "constraint_exemptions", "depends_on"},
        )
        modules = snapshot["draft"]["modules"]
        identifier = self._claim(snapshot, operation.get("module_id"), "module", modules)
        project = self._project(snapshot["project_ref"])
        constraints = _obligations(project)[1]
        original = modules.get(identifier)
        module = (
            deepcopy(original)
            if original
            else {
                "module_id": identifier,
                "contract_id": _id("module-contract"),
                "base_ref": None,
                "provided_interfaces": [],
                "consumed_interfaces": [],
                "technology_decision_ids": [],
                "verification_mode": "GENERATED",
                "check_ids": {},
                "verification_spec": {},
            }
        )
        module.update(
            purpose_and_boundaries=deepcopy(operation["purpose_and_boundaries"]),
            requirement_ids=_ids(operation["requirement_ids"]),
            constraint_ids=_ids(operation.get("constraint_ids", list(constraints))),
            constraint_exemptions=deepcopy(operation.get("constraint_exemptions", {})),
            depends_on=_ids(operation.get("depends_on", module.get("depends_on", []))),
        )
        modules[identifier] = module
        self._refresh(snapshot, module)

    @staticmethod
    def _module(snapshot, identifier):
        _identifier(identifier)
        module = snapshot["draft"]["modules"].get(identifier)
        if module is None:
            raise IntegrityError("The planned module does not exist.")
        return module

    def _interfaces(self, snapshot, operation):
        _fields(operation, {"module_id", "interfaces"})
        module = self._module(snapshot, operation["module_id"])
        if not isinstance(operation["interfaces"], list) or len(operation["interfaces"]) > 200:
            raise IntegrityError("Provide at most 200 interface definitions or links.")
        field = (
            "provided_interfaces"
            if operation["action"] == "set_provided_interfaces"
            else "consumed_interfaces"
        )
        module[field] = deepcopy(operation["interfaces"])
        self._refresh(snapshot, module)

    def _technology_links(self, snapshot, operation):
        _fields(operation, {"module_id", "decision_ids"})
        self._module(snapshot, operation["module_id"])["technology_decision_ids"] = _ids(
            operation["decision_ids"]
        )

    def _verification(self, snapshot, operation):
        reset = operation["action"] == "reset_verification"
        _fields(operation, {"module_id"} if reset else {"module_id", "verification_spec"})
        module = self._module(snapshot, operation["module_id"])
        module["verification_mode"] = "GENERATED" if reset else "CUSTOM"
        if not reset:
            module["verification_spec"] = deepcopy(operation["verification_spec"])
        self._refresh(snapshot, module)

    def _technology(self, snapshot, operation):
        _fields(operation, {"payload"}, {"decision_id"})
        decisions = snapshot["draft"]["decisions"]
        identifier = self._claim(snapshot, operation.get("decision_id"), "decision", decisions)
        payload = deepcopy(operation["payload"])
        validate_schema(
            _envelope(
                "TECHNOLOGY_DECISION",
                "draft-decision",
                snapshot["project_id"],
                payload,
                "draft-check",
                snapshot["session_id"],
                status="DRAFT",
            )
        )
        previous = decisions.get(identifier)
        if previous and payload["ai_recommendations"] != previous["original_recommendations"]:
            raise IntegrityError(
                "Keep original recommendations and ranking. Evaluate an outside choice separately."
            )
        decisions[identifier] = {
            "decision_id": identifier,
            "contract_id": previous["contract_id"] if previous else _id("technology"),
            "base_ref": None if previous is None else previous["base_ref"],
            "payload": payload,
            "original_recommendations": deepcopy(payload["ai_recommendations"]),
        }

    @staticmethod
    def _selection(snapshot, operation):
        _fields(operation, {"decision_id", "selected_option", "decision_rationale"})
        _identifier(operation["decision_id"])
        decision = snapshot["draft"]["decisions"].get(operation["decision_id"])
        if decision is None:
            raise IntegrityError("The technology negotiation does not exist.")
        decision["payload"]["selected_option"] = deepcopy(operation["selected_option"])
        decision["payload"]["decision_rationale"] = _text(operation["decision_rationale"])

    def _row(self, snapshot, operation):
        checkpoint = operation["action"] == "upsert_checkpoint"
        field, name, key = (
            ("checkpoints", "checkpoint", "checkpoint_id")
            if checkpoint
            else ("playtest_milestones", "milestone", "milestone_id")
        )
        _fields(operation, {name})
        row = deepcopy(operation[name])
        if not isinstance(row, dict):
            raise IntegrityError("Provide a structured checkpoint or milestone.")
        identifier = self._claim(snapshot, row.get(key), name, snapshot["draft"][field])
        row[key] = identifier
        snapshot["draft"][field][identifier] = row
        self._workflow_shape(snapshot)

    @staticmethod
    def _remove(snapshot, operation):
        name = operation["action"].removeprefix("remove_")
        field = {
            "module": "modules",
            "technology": "decisions",
            "checkpoint": "checkpoints",
            "milestone": "playtest_milestones",
        }[name]
        key = "decision_id" if name == "technology" else name + "_id"
        _fields(operation, {key})
        _identifier(operation[key])
        if snapshot["draft"][field].pop(operation[key], None) is None:
            raise IntegrityError("The planning item does not exist.")

    @staticmethod
    def _sources(snapshot, identifiers):
        return [
            {"contract_ref": snapshot["project_ref"], "obligation_id": identifier}
            for identifier in identifiers
        ]

    def _refresh(self, snapshot, module):
        if module["verification_mode"] != "GENERATED":
            return
        project = self._project(snapshot["project_ref"])
        requirements, constraints = _obligations(project)
        spec = {
            key: []
            for key in (
                "requirement_checks",
                "constraint_checks",
                "interface_checks",
                "quality_checks",
            )
        }

        def check(label, criterion):
            check_id = module["check_ids"].setdefault(label, _id("check"))
            return {
                "check_id": check_id,
                "criterion": criterion,
                "verification_methods": ["Run focused checks and review the exact obligation."],
                "evidence_required": [
                    "Observed check results and a technical review tied to the implementation commit."
                ],
                "blocking": True,
            }

        for field, identifiers, items in (
            ("requirement_checks", module["requirement_ids"], requirements),
            ("constraint_checks", module["constraint_ids"], constraints),
        ):
            for identifier in identifiers:
                if identifier not in items:
                    raise IntegrityError("A module names an unknown Project obligation.")
                item = items[identifier]
                criterion = item.get("criterion", item.get("description"))
                if "acceptance_criteria" in item:
                    criterion += " Acceptance: " + " ".join(
                        row["criterion"] for row in item["acceptance_criteria"]
                    )
                spec[field].append(
                    {
                        **check(field + ":" + identifier, criterion),
                        "source_ref": self._sources(snapshot, [identifier])[0],
                    }
                )
        interfaces = [
            (module["module_id"], row["interface_id"], row["interface_revision"])
            for row in module["provided_interfaces"]
        ]
        interfaces += [
            (row["provider_module_id"], row["interface_id"], row["required_interface_revision"])
            for row in module["consumed_interfaces"]
        ]
        for provider, identifier, revision in interfaces:
            spec["interface_checks"].append(
                {
                    **check(
                        f"interface:{provider}:{identifier}:{revision}",
                        "Verify the specified inputs, outputs, errors, side effects and behavioral guarantees for this exact interface revision.",
                    ),
                    "provider_module_id": provider,
                    "interface_id": identifier,
                    "interface_revision": revision,
                }
            )
        for rule, (category, criterion) in _QUALITY.items():
            spec["quality_checks"].append(
                {
                    **check(rule, criterion),
                    "category": category,
                    "standard_rule_ref": {
                        "standard_id": "nukytera-standard",
                        "version": "0.1",
                        "rule_id": rule,
                    },
                }
            )
        spec["evidence_expectations"] = [
            "Identify the exact Module/Project revisions, implementation commit or snapshot, check results, documentation and technical review. Retain evidence under private storage."
        ]
        module["verification_spec"] = spec

    @classmethod
    def _module_shape(cls, snapshot, module):
        for key in ("module_id", "contract_id"):
            _identifier(module[key])
        for key in ("requirement_ids", "constraint_ids", "depends_on", "technology_decision_ids"):
            _ids(module[key])
        if (
            module["verification_mode"] not in {"GENERATED", "CUSTOM"}
            or type(module["check_ids"]) is not dict
            or type(module["constraint_exemptions"]) is not dict
        ):
            raise IntegrityError("Module planning metadata is invalid.")
        for identifier, rationale in module["constraint_exemptions"].items():
            _identifier(identifier)
            _text(rationale)
        consumers = []
        for consumer in module["consumed_interfaces"]:
            if set(consumer) != {
                "provider_module_id",
                "interface_id",
                "required_interface_revision",
            }:
                raise IntegrityError("Select the provider module and exact interface revision.")
            for value in consumer.values():
                _identifier(value)
            consumers.append(
                {
                    **consumer,
                    "provider_contract_ref": ContractRef(
                        "draft-provider", "draft-provider-r1", "MODULE", snapshot["project_id"]
                    ).to_json(),
                }
            )
        payload = {
            "module_id": module["module_id"],
            "purpose_and_boundaries": module["purpose_and_boundaries"],
            "requirement_refs": cls._sources(snapshot, module["requirement_ids"]),
            "constraint_refs": cls._sources(snapshot, module["constraint_ids"]),
            "provided_interfaces": module["provided_interfaces"],
            "consumed_interfaces": consumers,
            "technology_decision_refs": [],
            "verification_spec": module["verification_spec"],
        }
        validate_schema(
            _envelope(
                "MODULE",
                module["contract_id"],
                snapshot["project_id"],
                payload,
                "draft-check",
                snapshot["session_id"],
                status="DRAFT",
            )
        )

    @staticmethod
    def _workflow_shape(snapshot):
        payload = {
            "project_contract_ref": snapshot["project_ref"],
            "modules": [
                {
                    "module_id": "draft-module",
                    "module_contract_ref": ContractRef(
                        "draft-module", "draft-module-r1", "MODULE", snapshot["project_id"]
                    ).to_json(),
                    "requirement_ids": [],
                    "depends_on": [],
                }
            ],
            "technology_decisions": [],
            "checkpoints": list(snapshot["draft"]["checkpoints"].values()),
            "playtest_milestones": list(snapshot["draft"]["playtest_milestones"].values()),
            "execution_policy": _POLICY,
        }
        if not payload["checkpoints"]:
            payload["checkpoints"] = [
                {
                    "checkpoint_id": "draft-checkpoint",
                    "title": "Draft",
                    "objective": "Draft",
                    "module_ids": [],
                    "requirement_ids": [],
                    "depends_on": [],
                    "deliverables": ["Draft"],
                    "completion_criteria": [
                        {"criterion_id": "draft-criterion", "criterion": "Draft"}
                    ],
                    "verification_plan": ["Draft"],
                }
            ]
        validate_schema(
            _envelope(
                "WORKFLOW",
                snapshot["workflow_contract_id"],
                snapshot["project_id"],
                payload,
                "draft-check",
                snapshot["session_id"],
                status="DRAFT",
            )
        )

    def _candidate(self, snapshot, kind, contract_id, payload, actor_id, approval_kind, base_ref):
        base = None if base_ref is None else self.store.get(ContractRef.from_json(base_ref))
        active = self.store.resolve_active(contract_id)
        known = [
            ContractRef.from_json(reference)
            for reference in snapshot["published_refs"]
            if reference["contract_id"] == contract_id and reference["contract_type"] == kind
        ]
        if active is not None and ContractRef.from_document(active) in known:
            base = active
        elif base_ref is None and active is not None:
            raise ConflictError(
                "The planned contract identity is already used by another publication."
            )
        if base_ref is not None:
            if base is None or active != base:
                raise ConflictError("A planning source changed. Reconcile before review.")
        if base is not None:
            if base["payload"] == payload:
                return base, False
        if approval_kind == "DELEGATED" and kind != "WORKFLOW":
            raise IntegrityError(
                "A bounded replan cannot introduce a changed technology or Module Contract."
            )
        document = _envelope(
            kind,
            contract_id,
            snapshot["project_id"],
            payload,
            actor_id,
            snapshot["session_id"],
            base=base,
            approval_kind=approval_kind,
        )
        validate_schema(document)
        return document, True

    def _compile(self, snapshot, actor_id, approval_kind):
        project = self._project(snapshot["project_ref"])
        draft = snapshot["draft"]
        modules = draft["modules"]
        base_workflow = (
            None
            if snapshot["base_ref"] is None
            else self.store.get(ContractRef.from_json(snapshot["base_ref"]))
        )
        if snapshot["base_ref"] is not None and base_workflow is None:
            raise IntegrityError("The original Workflow revision is unavailable.")

        def ordered_keys(field, key):
            original = (
                []
                if base_workflow is None
                else [item[key] for item in base_workflow["payload"][field]]
            )
            return [identifier for identifier in original if identifier in draft[field]] + [
                identifier for identifier in draft[field] if identifier not in original
            ]

        if not modules or not draft["checkpoints"]:
            raise IntegrityError("Define at least one module and one observable checkpoint.")
        module_order = _order(
            modules,
            lambda row: [
                *row["depends_on"],
                *(item["provider_module_id"] for item in row["consumed_interfaces"]),
            ],
            "module",
        )
        _order(draft["checkpoints"], lambda row: row["depends_on"], "checkpoint")
        milestone_order = _order(
            draft["playtest_milestones"], lambda row: row["depends_on"], "milestone"
        )
        if base_workflow is not None:
            preferred = ordered_keys("playtest_milestones", "milestone_id")
            earlier = set()
            for identifier in preferred:
                if not set(draft["playtest_milestones"][identifier]["depends_on"]) <= earlier:
                    break
                earlier.add(identifier)
            else:
                milestone_order = preferred
        changed, all_documents, decision_documents, module_documents = [], [], {}, {}
        used_decisions = {
            identifier
            for module in modules.values()
            for identifier in module["technology_decision_ids"]
        }
        if used_decisions != draft["decisions"].keys():
            raise IntegrityError(
                "Associate every planned technology decision with its modules and resolve missing decisions."
            )
        for identifier, row in draft["decisions"].items():
            if (
                not row["payload"]["ai_recommendations"]
                or row["payload"]["selected_option"] is None
            ):
                raise IntegrityError(
                    "A technology decision needs viable options and a selected choice."
                )
            if not set(row["payload"]["decision_scope"]["module_ids"]) <= modules.keys():
                raise IntegrityError("A technology decision names an unknown planned module.")
            document, new = self._candidate(
                snapshot,
                "TECHNOLOGY_DECISION",
                row["contract_id"],
                row["payload"],
                actor_id,
                approval_kind,
                row["base_ref"],
            )
            decision_documents[identifier] = document
            all_documents.append(document)
            if new:
                changed.append(document)
        _, constraints = _obligations(project)
        assigned_constraints = set()
        for identifier in module_order:
            row = modules[identifier]
            assigned_constraints.update(row["constraint_ids"])
            if not set(row["constraint_ids"]) <= constraints.keys():
                raise IntegrityError("A module names an unknown Project constraint.")
            if (
                not constraints.keys() - set(row["constraint_ids"])
                <= row["constraint_exemptions"].keys()
            ):
                raise IntegrityError(
                    "Explain why omitted global constraints do not apply to this module."
                )
            consumers = []
            previous = (
                None
                if row["base_ref"] is None
                else self.store.get(ContractRef.from_json(row["base_ref"]))
            )
            for consumer in row["consumed_interfaces"]:
                provider = module_documents[consumer["provider_module_id"]]
                reference = ContractRef.from_document(provider).to_json()
                if previous is not None:
                    old = next(
                        (
                            item
                            for item in previous["payload"]["consumed_interfaces"]
                            if all(item[key] == value for key, value in consumer.items())
                        ),
                        None,
                    )
                    if old is not None:
                        old_provider = self.store.get(
                            ContractRef.from_json(old["provider_contract_ref"])
                        )
                        if old_provider is not None:

                            def find(document):
                                return next(
                                    (
                                        item
                                        for item in document["payload"]["provided_interfaces"]
                                        if item["interface_id"] == consumer["interface_id"]
                                        and item["interface_revision"]
                                        == consumer["required_interface_revision"]
                                    ),
                                    None,
                                )

                            if find(old_provider) == find(provider):
                                reference = old["provider_contract_ref"]
                consumers.append({**consumer, "provider_contract_ref": reference})
            payload = {
                "module_id": identifier,
                "purpose_and_boundaries": row["purpose_and_boundaries"],
                "requirement_refs": self._sources(snapshot, row["requirement_ids"]),
                "constraint_refs": self._sources(snapshot, row["constraint_ids"]),
                "provided_interfaces": row["provided_interfaces"],
                "consumed_interfaces": consumers,
                "technology_decision_refs": [
                    ContractRef.from_document(decision_documents[key]).to_json()
                    for key in row["technology_decision_ids"]
                ],
                "verification_spec": row["verification_spec"],
            }
            if previous is not None:
                for field in ("requirement_refs", "constraint_refs"):
                    if [item["obligation_id"] for item in previous["payload"][field]] == [
                        item["obligation_id"] for item in payload[field]
                    ]:
                        payload[field] = previous["payload"][field]
            document, new = self._candidate(
                snapshot,
                "MODULE",
                row["contract_id"],
                payload,
                actor_id,
                approval_kind,
                row["base_ref"],
            )
            module_documents[identifier] = document
            all_documents.append(document)
            if new:
                changed.append(document)
        if assigned_constraints != constraints.keys():
            raise IntegrityError(
                "The plan leaves a global Project constraint without a responsible module."
            )
        requirements = project["payload"]["requirements"]
        workflow_modules = []
        for identifier in ordered_keys("modules", "module_id"):
            row = modules[identifier]
            assigned = set(row["requirement_ids"])
            requirement_ids = [
                item["requirement_id"]
                for item in requirements
                if item["requirement_id"] in assigned
                or any(
                    criterion["criterion_id"] in assigned
                    for criterion in item["acceptance_criteria"]
                )
            ]
            dependencies = list(row["depends_on"])
            dependencies.extend(
                item["provider_module_id"]
                for item in row["consumed_interfaces"]
                if item["provider_module_id"] not in dependencies
            )
            workflow_modules.append(
                {
                    "module_id": identifier,
                    "module_contract_ref": ContractRef.from_document(
                        module_documents[identifier]
                    ).to_json(),
                    "requirement_ids": requirement_ids,
                    "depends_on": dependencies,
                }
            )
        associations = [
            {
                "module_id": identifier,
                "decision_contract_ref": ContractRef.from_document(
                    decision_documents[key]
                ).to_json(),
            }
            for identifier, row in modules.items()
            for key in row["technology_decision_ids"]
        ]
        if base_workflow is not None:
            original = base_workflow["payload"]["technology_decisions"]
            associations = [item for item in original if item in associations] + [
                item for item in associations if item not in original
            ]
        payload = {
            "project_contract_ref": snapshot["project_ref"],
            "modules": workflow_modules,
            "technology_decisions": associations,
            "checkpoints": [
                draft["checkpoints"][key] for key in ordered_keys("checkpoints", "checkpoint_id")
            ],
            "playtest_milestones": [draft["playtest_milestones"][key] for key in milestone_order],
            "execution_policy": deepcopy(_POLICY),
        }
        self._checkpoint_dependencies(payload)
        document, new = self._candidate(
            snapshot,
            "WORKFLOW",
            snapshot["workflow_contract_id"],
            payload,
            actor_id,
            approval_kind,
            snapshot["base_ref"],
        )
        all_documents.append(document)
        if new:
            changed.append(document)
        if approval_kind == "DELEGATED":
            if snapshot["base_ref"] is None:
                raise IntegrityError("A delegated replan requires an existing approved Workflow.")
            before = deepcopy(
                self.store.get(ContractRef.from_json(snapshot["base_ref"]))["payload"]
            )
            after = deepcopy(payload)
            for plan in (before, after):
                for checkpoint in plan["checkpoints"]:
                    checkpoint["depends_on"] = []
            if before != after:
                raise IntegrityError(
                    "Delegation permits only checkpoint dependency changes under the same approved obligations."
                )
        overlay = {ContractRef.from_document(item): item for item in all_documents}

        def lookup(reference):
            return overlay.get(reference) or self.store.get(reference)

        def active(contract_id):
            return next(
                (item for item in all_documents if item["metadata"]["contract_id"] == contract_id),
                None,
            ) or self.store.resolve_active(contract_id)

        events = self.store.list_events()

        def history(kind, project_id):
            return [
                self.store.get(
                    ContractRef(
                        row["contract_id"],
                        row["revision_id"],
                        row["contract_type"],
                        row["project_id"],
                    )
                )
                for row in events
                if row["contract_type"] == kind and row["project_id"] == project_id
            ]

        for candidate in all_documents:
            validate_semantics(
                candidate,
                lookup,
                active,
                history=history,
                verify_artifact=self.store.verify_artifact,
            )
        return changed, all_documents

    @staticmethod
    def _checkpoint_dependencies(payload):
        checkpoints = {row["checkpoint_id"]: row for row in payload["checkpoints"]}
        modules = {row["module_id"]: row for row in payload["modules"]}
        for checkpoint in checkpoints.values():
            ancestors, pending = set(), list(checkpoint["depends_on"])
            while pending:
                key = pending.pop()
                if key not in ancestors:
                    ancestors.add(key)
                    pending.extend(checkpoints[key]["depends_on"])
            available = set(checkpoint["module_ids"]) | {
                identifier for key in ancestors for identifier in checkpoints[key]["module_ids"]
            }
            for identifier in checkpoint["module_ids"]:
                if (
                    identifier not in modules
                    or not set(modules[identifier]["depends_on"]) <= available
                ):
                    raise IntegrityError(
                        "A consumer checkpoint needs its provider in the same checkpoint or a prerequisite."
                    )

    def assess(self, session_id):
        snapshot = self.get(session_id)
        issues = []
        try:
            self._compile(snapshot, "planning-assessment", "HUMAN")
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            issues.append(
                str(error)
                if isinstance(error, IntegrityError)
                else "Complete the planning fields, exact references and coverage before review."
            )
        unknowns = [
            "Natural-language consistency, technology feasibility and architectural fitness require human review.",
            "Verification specifications are plans. Product implementation and independent Validation are NOT_VERIFIED.",
        ]
        if self.assistance is not None:
            try:
                response = self.assistance(deepcopy(snapshot))
                if not isinstance(response, list):
                    raise ValueError
                unknowns.extend("Advisory assistance: " + _text(item) for item in response)
            except Exception:
                unknowns.append(
                    "Optional assistance was unavailable or returned invalid advisory content."
                )
        return {"issues": issues, "unknowns": unknowns}

    def review(self, session_id, expected_version, *, actor_id, approval_kind="HUMAN"):
        if approval_kind not in {"HUMAN", "DELEGATED"}:
            raise IntegrityError(
                "Choose HUMAN review or a genuinely authorized bounded delegation."
            )
        snapshot = self._editable(session_id, expected_version)
        documents, all_documents = self._compile(snapshot, actor_id, approval_kind)
        if not documents:
            raise ConflictError("Unchanged planning content does not create contract revisions.")
        snapshot["review"] = {
            "documents": documents,
            "sha256": sha256(canonical_json(documents).encode()).hexdigest(),
            "expires_at": time() + 300.0,
        }
        snapshot["compiled_refs"] = [
            ContractRef.from_document(item).to_json() for item in all_documents
        ]
        snapshot["published_refs"] = [
            ContractRef.from_document(item).to_json()
            for item in all_documents
            if item not in documents
        ]
        snapshot["state"] = "AWAITING_EXPLICIT_APPROVAL"
        snapshot["authorization_version"] = None
        snapshot["mandate_id"] = None
        self._snapshot(snapshot)
        return self.repository.save(
            session_id,
            expected_version,
            snapshot,
            actor_id=actor_id,
            action="EXACT_PLAN_REVIEW_PREPARED",
        )

    def defer(self, session_id, expected_version, *, actor_id):
        return self._transition(session_id, expected_version, "DEFERRED", actor_id)

    def cancel(self, session_id, expected_version, *, actor_id):
        return self._transition(session_id, expected_version, "CANCELLED", actor_id)

    def _transition(self, session_id, expected_version, state, actor_id):
        snapshot = self._editable(session_id, expected_version)
        snapshot.update(
            state=state,
            review=None,
            authorization_version=None,
            mandate_id=None,
            compiled_refs=[],
        )
        return self.repository.save(
            session_id, expected_version, snapshot, actor_id=actor_id, action="PLANNING_" + state
        )

    def revise(self, workflow_ref, reason, *, actor_id):
        reference = (
            workflow_ref
            if isinstance(workflow_ref, ContractRef)
            else ContractRef.from_json(workflow_ref)
        )
        base = self.store.get(reference)
        if (
            reference.contract_type != "WORKFLOW"
            or base is None
            or self.store.resolve_active(reference.contract_id) != base
        ):
            raise ConflictError("Revision planning requires the exact active Workflow.")
        project = self._project(base["payload"]["project_contract_ref"])
        snapshot = self._start(project, actor_id, base=base, reason=reason)
        draft = snapshot["draft"]
        decision_ids = {}
        for association in base["payload"]["technology_decisions"]:
            ref = ContractRef.from_json(association["decision_contract_ref"])
            if ref.contract_id not in draft["decisions"]:
                document = self.store.get(ref)
                if document is None:
                    raise IntegrityError("A published technology source is unavailable.")
                draft["decisions"][ref.contract_id] = {
                    "decision_id": ref.contract_id,
                    "contract_id": ref.contract_id,
                    "base_ref": ref.to_json(),
                    "payload": document["payload"],
                    "original_recommendations": deepcopy(document["payload"]["ai_recommendations"]),
                }
            decision_ids[ref] = ref.contract_id
        for row in base["payload"]["modules"]:
            ref = ContractRef.from_json(row["module_contract_ref"])
            document = self.store.get(ref)
            if document is None:
                raise IntegrityError("A published Module source is unavailable.")
            payload = document["payload"]
            draft["modules"][row["module_id"]] = {
                "module_id": row["module_id"],
                "contract_id": ref.contract_id,
                "base_ref": ref.to_json(),
                "purpose_and_boundaries": payload["purpose_and_boundaries"],
                "requirement_ids": [item["obligation_id"] for item in payload["requirement_refs"]],
                "constraint_ids": [item["obligation_id"] for item in payload["constraint_refs"]],
                "constraint_exemptions": {},
                "depends_on": row["depends_on"],
                "provided_interfaces": payload["provided_interfaces"],
                "consumed_interfaces": [
                    {key: value for key, value in item.items() if key != "provider_contract_ref"}
                    for item in payload["consumed_interfaces"]
                ],
                "technology_decision_ids": [
                    decision_ids[ContractRef.from_json(item)]
                    for item in payload["technology_decision_refs"]
                ],
                "verification_mode": "CUSTOM",
                "verification_spec": payload["verification_spec"],
                "check_ids": {},
            }
        for field, key in (
            ("checkpoints", "checkpoint_id"),
            ("playtest_milestones", "milestone_id"),
        ):
            draft[field] = {item[key]: deepcopy(item) for item in base["payload"][field]}
        snapshot["used_ids"] = list(
            dict.fromkeys(key for records in draft.values() for key in records)
        )
        self._snapshot({**snapshot, "version": 1})
        return self.repository.create(snapshot, actor_id=actor_id, action="PLAN_REVISION_STARTED")
