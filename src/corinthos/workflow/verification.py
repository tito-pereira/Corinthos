# SPDX-License-Identifier: MPL-2.0
"""Actual local engineering checks and a consumer for published Validation results."""

import os
import queue
import stat
import subprocess
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from time import monotonic

from corinthos._paths import windows_component_is_reserved
from corinthos.adapters.process_control import start_process
from corinthos.contracts import ContractRef, IntegrityError, validate_schema
from corinthos.contracts.artifacts import is_link, private_directory
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.contracts.semantics import validate_semantics

_SNAPSHOT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".corinthos",
        ".nukytera",
        ".agentforge",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "build",
        "dist",
    }
)


@dataclass(frozen=True, slots=True)
class CommandCheck:
    check_id: str
    plan_item: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout: float = 60


@dataclass(frozen=True, slots=True)
class RequiredGate:
    validation_plan_ref: ContractRef
    result_ref: ContractRef | None
    gate: str
    scope: tuple[str, ...]
    target_ref: ContractRef


_AUDIT_SCOPES = {
    "MODULE": ("MODULE", "CHECKPOINT"),
    "INTEGRATION": ("WORKFLOW", "CHECKPOINT"),
    "CHECKPOINT": ("WORKFLOW", "CHECKPOINT"),
    "PLAYTEST_MILESTONE": ("WORKFLOW", "PLAYTEST_READINESS"),
    "PRODUCT": ("PROJECT", "PRODUCT_VALIDATION"),
}


def _managed_plan(plan):
    return any(
        item.startswith("bindings-sha256:")
        for item in plan["payload"]["execution_plan"]["procedures"]
    )


@dataclass(frozen=True, slots=True)
class AuditExpectation:
    """An exact formal scope whose plan can be published after configuration."""

    level: str
    target_ref: ContractRef
    scope: tuple[str, ...]

    def __post_init__(self):
        if (
            not isinstance(self.level, str)
            or self.level not in _AUDIT_SCOPES
            or not isinstance(self.target_ref, ContractRef)
            or self.target_ref.contract_type != _AUDIT_SCOPES[self.level][0]
            or not isinstance(self.scope, (list, tuple))
            or not self.scope
            or not all(isinstance(item, str) and ID_PATTERN.fullmatch(item) for item in self.scope)
            or len(set(self.scope)) != len(self.scope)
        ):
            raise IntegrityError(
                "A formal audit needs an exact target and valid scoped identifiers."
            )
        object.__setattr__(self, "scope", tuple(sorted(self.scope)))

    @property
    def gate(self):
        return _AUDIT_SCOPES[self.level][1]

    def to_json(self):
        return {
            "level": self.level,
            "target_ref": self.target_ref.to_json(),
            "scope": list(self.scope),
        }

    @classmethod
    def from_json(cls, value):
        if type(value) is not dict or set(value) != {"level", "target_ref", "scope"}:
            raise IntegrityError("A stored audit expectation has an invalid structure.")
        return cls(value["level"], ContractRef.from_json(value["target_ref"]), value["scope"])

    @classmethod
    def from_plan(cls, plan):
        scope = plan["payload"]["validation_scope"]
        return cls(
            scope["level"], ContractRef.from_json(scope["target_contract_ref"]), scope["target_ids"]
        )


@dataclass(frozen=True, slots=True)
class CheckpointSpec:
    checkpoint_id: str
    commands: tuple[CommandCheck, ...]
    deliverables: dict[str, list[str]]
    allowed_paths: tuple[str, ...]
    review_items: tuple[str, ...] = ()
    required_gates: tuple[RequiredGate, ...] = ()
    audit_expectations: tuple[AuditExpectation, ...] = ()

    def to_json(self):
        value = asdict(self)
        for key in ("commands", "allowed_paths", "review_items", "required_gates"):
            value[key] = list(value[key])
        for command in value["commands"]:
            command["argv"] = list(command["argv"])
        for item, gate in zip(value["required_gates"], self.required_gates, strict=True):
            item.update(
                validation_plan_ref=gate.validation_plan_ref.to_json(),
                result_ref=None if gate.result_ref is None else gate.result_ref.to_json(),
                target_ref=gate.target_ref.to_json(),
                scope=list(gate.scope),
            )
        if self.audit_expectations:
            value["audit_expectations"] = [item.to_json() for item in self.audit_expectations]
        else:
            del value["audit_expectations"]
        return value

    @classmethod
    def from_json(cls, value):
        return cls(
            value["checkpoint_id"],
            tuple(CommandCheck(**command) for command in value["commands"]),
            value["deliverables"],
            tuple(value["allowed_paths"]),
            tuple(value["review_items"]),
            tuple(
                RequiredGate(
                    ContractRef.from_json(gate["validation_plan_ref"]),
                    None
                    if gate["result_ref"] is None
                    else ContractRef.from_json(gate["result_ref"]),
                    gate["gate"],
                    tuple(gate["scope"]),
                    ContractRef.from_json(gate["target_ref"]),
                )
                for gate in value["required_gates"]
            ),
            tuple(AuditExpectation.from_json(item) for item in value.get("audit_expectations", ())),
        )


@dataclass(frozen=True, slots=True)
class TechnicalReview:
    actor_id: str
    accepted: bool
    criterion_ids: tuple[str, ...]
    artifact_identity: dict
    findings: tuple[str, ...] = ()


def relative_path(value, *, directory=False):
    if directory and value == ".":
        return value
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "\\" in value
        or ":" in value
    ):
        raise IntegrityError("Use a portable relative workspace path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(
            item in {".", ".."}
            or windows_component_is_reserved(item)
            or item.endswith((".", " "))
            or any(ord(char) < 32 for char in item)
            for item in path.parts
        )
    ):
        raise IntegrityError("The workspace path is unsafe.")
    if any(item in {".git", ".corinthos", ".nukytera", ".agentforge"} for item in path.parts):
        raise IntegrityError("Execution paths cannot target private state or Git internals.")
    return value


def safe_workspace_path(root, relative, *, directory=False):
    relative_path(relative, directory=directory)
    path = root
    for component in () if relative == "." else PurePosixPath(relative).parts:
        path = path / component
        info = path.lstat()
        if is_link(info):
            raise IntegrityError("Workspace verification cannot follow links.")
    if not path.resolve(strict=True).is_relative_to(root):
        raise IntegrityError("The verification path escapes its workspace.")
    return path


def _digest_file(path, *, max_bytes=67_108_864):
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise IntegrityError("Evidence requires an ordinary unlinked regular file.")
    if info.st_size > min(67_108_864, max_bytes):
        raise IntegrityError("A workspace file exceeds the bounded snapshot limit.")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise IntegrityError("A workspace file changed while opening its snapshot.")
        content = stream.read(info.st_size + 1)
        if (
            len(content) != info.st_size
            or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns
        ):
            raise IntegrityError("A workspace file changed during its snapshot.")
        digest = sha256(content).hexdigest()
    return {"sha256": digest, "size": info.st_size}


def workspace_snapshot(root, *, max_bytes=None, max_files=10_000, include_ignored=False):
    """Hash source files while excluding local state, environments and generated caches."""
    if (max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0)) or (
        type(max_files) is not int or max_files < 1
    ):
        raise IntegrityError("Snapshot limits require nonnegative bytes and positive file counts.")
    root = Path(root)
    git_metadata = root / ".git"
    git_root = os.path.lexists(git_metadata)
    if git_root and is_link(git_metadata.lstat()):
        raise IntegrityError("A source snapshot cannot use linked Git metadata.")
    files, total = {}, 0
    for parent, directories, filenames in os.walk(root, followlinks=False):
        current = Path(parent)
        kept = []
        for name in sorted(directories):
            if not include_ignored and (
                name in _SNAPSHOT_IGNORED_DIRECTORIES
                or name.startswith(".venv")
                or name.endswith(".egg-info")
            ):
                continue
            if is_link((current / name).lstat()):
                raise IntegrityError("A source snapshot cannot traverse linked directories.")
            kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            if not include_ignored and (
                name.endswith((".pyc", ".pyo"))
                or (current == root and (current / name) == git_metadata)
            ):
                continue
            path = current / name
            if len(files) >= max_files:
                raise IntegrityError("The workspace exceeds the bounded source inventory limit.")
            remaining = 67_108_864 if max_bytes is None else max_bytes - total
            files[path.relative_to(root).as_posix()] = _digest_file(path, max_bytes=remaining)
            total += files[path.relative_to(root).as_posix()]["size"]
    identity = {"snapshot_id": sha256(canonical_json(files).encode("utf-8")).hexdigest()}
    if not git_root:
        return {"artifact_identity": identity, "files": files}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        lines = result.stdout.decode("utf-8").splitlines()
        if (
            result.returncode == 0
            and len(lines) == 2
            and Path(lines[0]).resolve() == root.resolve()
            and len(lines[1]) == 40
            and all(char in "0123456789abcdef" for char in lines[1])
        ):
            identity["commit"] = lines[1]
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return {"artifact_identity": identity, "files": files}


def write_artifact(store, attempt_id, name, data):
    base = private_directory(store.database_path.parent.parent, "artifacts", create=True)
    parent = base
    for component in ("runtime", attempt_id):
        if not component or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for char in component
        ):
            raise IntegrityError("The runtime evidence identity is invalid.")
        parent = parent / component
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise IntegrityError("Runtime evidence directories must be ordinary local directories.")
    path = parent / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    relative = path.relative_to(base).as_posix()
    digest = sha256(data).hexdigest()
    store.verify_artifact(relative, digest)
    return {"path": relative, "sha256": digest, "size": len(data)}


def command_check(store, root, attempt_id, check, cancel, *, environment=None):
    """Run the already bound argv directly and retain bounded actual output privately."""
    cwd = safe_workspace_path(root, check.cwd, directory=True)
    started = monotonic()
    try:
        managed = start_process(
            list(check.argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            env=os.environ.copy() if environment is None else environment,
        )
        process = managed.process
    except OSError as error:
        return {
            "check_id": check.check_id,
            "plan_item": check.plan_item,
            "state": "FAILED",
            "error": "CHECK_START_FAILED",
            "cleanup_confirmed": getattr(error, "cleanup_confirmed", True),
            "exit_code": None,
        }
    chunks = queue.Queue(maxsize=16)
    stop = threading.Event()

    def read():
        try:
            while data := process.stdout.read(65_536):
                while not stop.is_set():
                    try:
                        chunks.put(data, timeout=0.05)
                        break
                    except queue.Full:
                        continue
            while not stop.is_set():
                try:
                    chunks.put(None, timeout=0.05)
                    break
                except queue.Full:
                    continue
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    output, state, ended, cleanup = bytearray(), None, False, True
    try:
        while not ended or process.poll() is None:
            if cancel.is_set():
                state = "INTERRUPTED"
                break
            if monotonic() - started >= check.timeout:
                state = "TIMEOUT"
                break
            try:
                data = chunks.get(timeout=0.05)
            except queue.Empty:
                continue
            if data is None:
                ended = True
            elif len(output) + len(data) > 8_388_608:
                state = "OUTPUT_LIMIT"
                break
            else:
                output.extend(data)
    finally:
        cleanup = managed.finish()
        stop.set()
        if cleanup:
            process.stdout.close()
        reader.join(timeout=0.2)
    artifact = write_artifact(
        store,
        attempt_id,
        "check-" + sha256(check.check_id.encode()).hexdigest()[:16] + ".log",
        bytes(output),
    )
    return {
        "check_id": check.check_id,
        "plan_item": check.plan_item,
        "state": state or ("PASSED" if process.returncode == 0 else "FAILED"),
        "exit_code": process.returncode,
        "cleanup_confirmed": cleanup,
        "duration_seconds": monotonic() - started,
        "evidence": artifact,
    }


class ValidationGateConsumer:
    """Consume exact published results, retaining missing formal gates as unverified."""

    def __init__(self, store):
        self.store = store

    def _history(self, kind, project_id, *, exclude=None):
        return [
            self.store.get(
                ContractRef(
                    row["contract_id"], row["revision_id"], row["contract_type"], row["project_id"]
                )
            )
            for row in self.store.list_events()
            if row["contract_type"] == kind
            and row["project_id"] == project_id
            and row["revision_id"] != exclude
        ]

    def scope_allowed(self, workflow, checkpoint_id, expectation):
        """Bind a formal scope at the first checkpoint that contains its prerequisites."""
        payload = workflow["payload"]
        reference = ContractRef.from_document(workflow)
        target = expectation.target_ref
        scope = set(expectation.scope)
        if expectation.level == "PRODUCT":
            return target == ContractRef.from_json(payload["project_contract_ref"]) and scope == {
                reference.project_id
            }
        checkpoints = {row["checkpoint_id"]: row for row in payload["checkpoints"]}
        if expectation.level == "MODULE":
            modules = {
                row["module_id"]
                for row in payload["modules"]
                if ContractRef.from_json(row["module_contract_ref"]) == target
            }
            if not modules or scope != modules:
                return False
            needed = {
                identifier
                for identifier, row in checkpoints.items()
                if modules.intersection(row["module_ids"])
            }
        elif target != reference:
            return False
        elif expectation.level == "CHECKPOINT":
            needed = scope
        elif expectation.level == "PLAYTEST_MILESTONE":
            milestones = {row["milestone_id"]: row for row in payload["playtest_milestones"]}
            if not scope <= milestones.keys():
                return False
            selected = _dependency_closure(milestones, scope)
            needed = {
                identifier
                for milestone in selected
                for identifier in milestones[milestone]["required_checkpoint_ids"]
            }
        else:
            modules = {row["module_id"]: row for row in payload["modules"]}
            interfaces = {}
            for module_id, row in modules.items():
                module = self.store.get(ContractRef.from_json(row["module_contract_ref"]))
                if module is None:
                    raise IntegrityError("An exact integration Module Contract is missing.")
                terms = module["payload"]
                for interface in (*terms["provided_interfaces"], *terms["consumed_interfaces"]):
                    interfaces.setdefault(interface["interface_id"], set()).add(module_id)
            if scope & modules.keys() & interfaces.keys() or not scope <= (
                modules.keys() | interfaces.keys()
            ):
                return False
            selected = scope & modules.keys()
            for interface_id in scope & interfaces.keys():
                selected.update(interfaces[interface_id])
            needed = {
                identifier
                for identifier, row in checkpoints.items()
                if selected.intersection(row["module_ids"])
            }
        if checkpoint_id not in checkpoints or not needed:
            return False
        available = _dependency_closure(checkpoints, (checkpoint_id,))
        return needed <= available and not any(
            needed <= _dependency_closure(checkpoints, (dependency,))
            for dependency in checkpoints[checkpoint_id]["depends_on"]
        )

    def _active_plans(self, project_id):
        identifiers = dict.fromkeys(
            row["contract_id"]
            for row in self.store.list_events()
            if row["contract_type"] == "VALIDATION" and row["project_id"] == project_id
        )
        for identifier in identifiers:
            active = self.store.resolve_active(identifier)
            if active is not None:
                yield active

    def requirements(self, workflow_ref, checkpoint_id, spec=None, *, retained=()):
        workflow = self.store.get(workflow_ref)
        if workflow is None or workflow_ref.contract_type != "WORKFLOW":
            raise IntegrityError("An exact Workflow is required to resolve formal audit scopes.")
        expected = dict.fromkeys((*retained, *(spec.audit_expectations if spec else ())))
        candidates, found = [], set()
        gates = {}
        for plan in self._active_plans(workflow_ref.project_id):
            expectation = AuditExpectation.from_plan(plan)
            managed = _managed_plan(plan)
            if expectation in expected or (
                expectation.gate == "CHECKPOINT"
                and self.scope_allowed(workflow, checkpoint_id, expectation)
            ):
                if managed:
                    found.add(expectation)
                    expected[expectation] = None
                candidates.append((plan, expectation, managed))
        for plan, expectation, managed in candidates:
            if expectation in expected and not managed:
                continue
            reference = ContractRef.from_document(plan)
            gates[reference] = RequiredGate(
                reference, None, expectation.gate, expectation.scope, expectation.target_ref
            )
        if spec:
            gates.update({item.validation_plan_ref: item for item in spec.required_gates})
        return (
            list(gates.values()),
            tuple(expected),
            tuple(item for item in expected if item not in found),
        )

    def applicable(self, workflow_ref, checkpoint_id):
        return self.requirements(workflow_ref, checkpoint_id)[0]

    def observe(self, workflow_ref, checkpoint_id, spec, artifact_identity, *, retained=()):
        gates, expectations, missing = self.requirements(
            workflow_ref, checkpoint_id, spec, retained=retained
        )
        observations = [self.consume(gate, artifact_identity) for gate in gates]
        observations.extend(
            {
                "validation_plan_ref": None,
                "result_ref": None,
                "gate": item.gate,
                "level": item.level,
                "scope": list(item.scope),
                "target_ref": item.target_ref.to_json(),
                "state": "NOT_VERIFIED",
                "reason": "No published Validation plan covers this required exact scope.",
            }
            for item in missing
        )
        return observations, expectations

    def consume(self, required, artifact_identity):
        observation = {
            "validation_plan_ref": required.validation_plan_ref.to_json(),
            "gate": required.gate,
            "scope": list(required.scope),
            "state": "NOT_VERIFIED",
            "result_ref": None,
        }
        try:
            plan = self.store.get(required.validation_plan_ref)
            if plan is None:
                observation["reason"] = "The exact required Validation plan has not been published."
                return observation
            if self.store.resolve_active(required.validation_plan_ref.contract_id) != plan:
                raise IntegrityError("The exact Validation plan is unavailable or stale.")
            scope = plan["payload"]["validation_scope"]
            if (
                scope["target_contract_ref"] != required.target_ref.to_json()
                or set(scope["target_ids"]) != set(required.scope)
                or plan["payload"]["gate_policy"]["gate"] != required.gate
            ):
                raise IntegrityError("The Validation plan has a different target, gate or scope.")
            for source in plan["payload"]["source_contract_refs"]:
                reference = ContractRef.from_json(source)
                active = self.store.resolve_active(reference.contract_id)
                if active is None or ContractRef.from_document(active) != reference:
                    raise IntegrityError("A direct Validation source is stale.")
            candidates = [
                document
                for document in self._history("VALIDATION_RESULT", required.target_ref.project_id)
                if document["payload"]["validation_plan_ref"]
                == required.validation_plan_ref.to_json()
                and document["payload"]["target_and_execution"]["artifact_identity"]
                == artifact_identity
            ]
            latest = candidates[-1] if candidates else None
            result = (
                self.store.get(required.result_ref) if required.result_ref is not None else latest
            )
            if (
                required.result_ref is not None
                and latest is not None
                and ContractRef.from_document(latest) != required.result_ref
            ):
                raise IntegrityError("A newer result exists for this exact plan and artifact.")
            if result is None:
                observation["reason"] = (
                    "A real result for this exact current artifact is unavailable."
                )
                return observation
            reference = ContractRef.from_document(result)
            observation["result_ref"] = reference.to_json()
            if (
                reference.contract_type != "VALIDATION_RESULT"
                or reference.project_id != required.target_ref.project_id
            ):
                raise IntegrityError("The supplied result has another project or type.")
            payload = result["payload"]
            execution = payload["target_and_execution"]
            if (
                payload["validation_plan_ref"] != required.validation_plan_ref.to_json()
                or execution["artifact_identity"] != artifact_identity
            ):
                raise IntegrityError("The result is bound to another plan or artifact.")
            if result["metadata"]["provenance"]["engine"] != "VALIDATION":
                raise IntegrityError("The result has no Validation provenance.")
            if _managed_plan(plan):
                from corinthos.validation.repository import ValidationRepository

                repository = ValidationRepository(self.store)
                with self.store._connection() as connection:
                    repository.require_current(connection, required.validation_plan_ref)
                if not repository.is_published_run(
                    required.validation_plan_ref, reference, execution["validation_run_id"]
                ):
                    observation["reason"] = (
                        "The result lacks an exact durable Validation execution record."
                    )
                    return observation
            if datetime.fromisoformat(result["metadata"]["created_at"]) > datetime.now(UTC):
                raise IntegrityError("The result is dated after the current observation.")
            for document in (plan, result):
                validate_schema(document)
                validate_semantics(
                    document,
                    self.store.get,
                    self.store.resolve_active,
                    history=lambda kind, project: self._history(
                        kind, project, exclude=reference.revision_id
                    ),
                    verify_artifact=self.store.verify_artifact,
                )
            gate = payload["gate_assessment"]
            if gate["gate"] != required.gate or gate["scope"] != scope["level"]:
                raise IntegrityError("The result has another gate or scope.")
            if gate["state"] != "SATISFIED" or execution["status"] != "COMPLETED":
                observation.update(
                    state="BLOCKED",
                    reason="The latest admissible Validation result blocks this gate.",
                    blocking_check_ids=gate["blocking_check_ids"],
                    finding_ids=[item["finding_id"] for item in payload["findings"]],
                )
                return observation
            observation.update(
                state="SATISFIED",
                reason="The exact published result and retained evidence satisfy this gate.",
            )
        except (ValueError, KeyError, TypeError, OSError, RecursionError):
            observation.update(
                state="BLOCKED",
                reason="Published Validation evidence is missing, stale, incomplete or inadmissible.",
            )
        return observation


def _dependency_closure(records, roots):
    selected, pending = set(), list(roots)
    while pending:
        identifier = pending.pop()
        if identifier not in selected:
            selected.add(identifier)
            pending.extend(records[identifier]["depends_on"])
    return selected
