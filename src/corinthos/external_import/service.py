# SPDX-License-Identifier: MPL-2.0
"""Import an explicitly confirmed local Git repository into private project state."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from corinthos import user_state
from corinthos.bootstrap import _root, initialize_private
from corinthos.contracts import ConflictError, ContractError, ContractStore, IntegrityError
from corinthos.contracts.artifacts import private_directory
from corinthos.contracts.migration import _promote
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.lifecycle.repository import LifecycleRepository
from corinthos.project_registry import ProjectRegistryService
from corinthos.project_status import _absolute_root, project_status
from corinthos.user_state import _checked_file, _ordinary

from . import storage

_INTENT = "external-import-intent.json"
_STAGE_PREFIX = ".corinthos-import-"


def _view(root, state, project_id=None, entry=None, *, error=None):
    entry = entry or {}
    return {
        "root": str(root),
        "status": state,
        "label": "CONFIRMED" if state == "IMPORTED" else None,
        "project_id": project_id,
        "snapshot_digest": entry.get("snapshot_digest"),
        "discovery_digest": entry.get("discovery_digest"),
        "summary": entry.get("summary"),
        "error": error,
    }


def _read_intent(root):
    path = private_directory(root) / _INTENT
    try:
        info = _ordinary(path, stat.S_ISREG)
        if info is None:
            return None
        if info.st_size > 8192:
            raise ValueError
        with _checked_file(path, info) as source:
            content = source.read(8193)
        value = json.loads(content)
        if (
            set(value) != {"project_id", "discovery_digest", "confirmations", "version"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or not ID_PATTERN.fullmatch(value["project_id"])
            or not isinstance(value["confirmations"], list)
            or canonical_json(value).encode("utf-8") != content
        ):
            raise ValueError
        storage.require_digest(value["discovery_digest"])
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise IntegrityError(
            "The existing import intent is invalid. Preserve private state."
        ) from None


def _new_intent(root, digest, confirmations):
    value = {
        "version": 1,
        "project_id": f"project-{uuid4().hex}",
        "discovery_digest": digest,
        "confirmations": confirmations,
    }
    selected = root.stat()
    candidates = [Path(path) for key in ("TMPDIR", "TEMP", "TMP") if (path := os.environ.get(key))]
    for candidate in [*candidates, Path("/tmp"), root.parent]:
        if not candidate.is_absolute():
            continue
        try:
            scratch = _root(candidate)
            if scratch.is_relative_to(root) or scratch.stat().st_dev != selected.st_dev:
                continue
            temporary = TemporaryDirectory(dir=scratch, prefix=_STAGE_PREFIX)
            break
        except (ContractError, OSError):
            continue
    else:
        raise IntegrityError(
            "Import requires private staging outside the repository on the same filesystem."
        )
    with temporary as directory:
        staged = Path(directory) / ".corinthos"
        staged.mkdir(mode=0o700)
        with (staged / _INTENT).open("xb") as file:
            file.write(canonical_json(value).encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
        if _root(root) != root or not os.path.samestat(root.stat(), selected):
            raise IntegrityError("The selected repository moved before import initialization.")
        try:
            _promote(staged, root / ".corinthos")
        except ConflictError:
            return _read_intent(root)
    return value


def _summary(observation):
    return {
        "policy_version": observation["policy_version"],
        "head": observation["head"],
        "branch": observation["branch"],
        "dirty": observation["dirty"],
        "tracked_count": observation["tracked_count"],
        "languages": observation["languages"],
        "manifest_count": len(observation["manifests"]),
        "doc_count": len(observation["docs"]),
        "test_count": len(observation["tests"]),
        "candidate_count": len(observation["candidates"]),
        "ambiguity_codes": [
            a["code"] for a in observation["ambiguities"] if a["code"] != "EXISTING_STATE"
        ],
    }


def _check_inputs(digest, confirmations):
    storage.require_digest(digest)
    if (
        not isinstance(confirmations, (list, tuple))
        or len(confirmations) > 32
        or any(not isinstance(code, str) or not 1 <= len(code) <= 64 for code in confirmations)
        or len(set(confirmations)) != len(confirmations)
    ):
        raise ValueError("Supply unique import ambiguity confirmation codes.")
    return sorted(confirmations)


def _reject(observation, digest, confirmations):
    if observation["discovery_digest"] != digest:
        return "REPOSITORY_CHANGED"
    if not observation["commit_allowed"]:
        return "UNSAFE_REPOSITORY_STATE"
    cases = {
        a["code"]: a["required"]
        for a in observation["ambiguities"]
        if a["code"] != "EXISTING_STATE"
    }
    if any(code not in cases for code in confirmations):
        return "INVALID_CONFIRMATIONS"
    if any(required and code not in confirmations for code, required in cases.items()):
        return "CONFIRMATION_REQUIRED"
    return None


def _remember(root, project_id, entry):
    try:
        if user_state.state_root().resolve().is_relative_to(root):
            raise IntegrityError(
                "The per-user Project Registry cannot be inside the imported repository."
            )
        ProjectRegistryService().remember(root)
    except (ContractError, OSError):
        return _view(root, "IMPORTED", project_id, entry, error="REGISTRY_UNAVAILABLE")
    return _view(root, "IMPORTED", project_id, entry)


def _entry(snapshot):
    observation = snapshot["observation"]
    return {
        "snapshot_digest": snapshot["snapshot_digest"],
        "discovery_digest": observation["discovery_digest"],
        "summary": _summary(observation),
    }


def _save_snapshot(root, project_id, observation, confirmations):
    # The scan result's managed-state flag is about the caller, not the repository evidence.
    evidence = observation | {
        "existing_state": False,
        "ambiguities": [
            row for row in observation["ambiguities"] if row["code"] != "EXISTING_STATE"
        ],
    }
    return _entry(storage.save_snapshot(root, project_id, evidence, confirmations))


class ExternalProjectImportService:
    """Use ephemeral discovery and append-only lifecycle pointers to immutable files."""

    def inspect(self, root):
        from .discovery import scan

        path, observation = scan(root)
        return {"root": str(path), "label": "DISCOVERED", **observation}

    def status(self, root):
        root = _root(_absolute_root(root))
        if os.path.lexists(root / ".nukytera"):
            return _view(root, "ALREADY_MANAGED")
        if not os.path.lexists(root / ".corinthos"):
            return _view(root, "NOT_IMPORTED")
        try:
            intent = _read_intent(root)
            if intent is None:
                state = project_status(root)
                valid = state.initialized and state.storage in {"ABSENT", "CURRENT", "LEGACY"}
                return _view(root, "ALREADY_MANAGED" if valid else "INVALID")
            project_id = intent["project_id"]
            if not os.path.lexists(private_directory(root) / "state.db"):
                return _view(root, "INCOMPLETE", project_id)
            with ContractStore.inspect_snapshot(root) as (connection, version):
                if version != 9:
                    return _view(root, "INVALID")
                entry = storage.pointer(connection, project_id)
                if entry is None:
                    return _view(root, "INCOMPLETE", project_id)
                if project_id not in storage.imported_project_ids(connection):
                    return _view(root, "INVALID")
            snapshot = storage.read_snapshot(root, project_id, entry)
            if _entry(snapshot) != entry:
                return _view(root, "INVALID")
            return _view(root, "IMPORTED", project_id, entry)
        except (ContractError, OSError, RuntimeError):
            return _view(root, "INVALID")

    def commit(self, root, discovery_digest, confirmations=()):
        from .discovery import scan

        confirmations = _check_inputs(discovery_digest, confirmations)
        root = _root(_absolute_root(root))
        status = self.status(root)
        if status["status"] in {"ALREADY_MANAGED", "INVALID"}:
            return status
        if status["status"] == "IMPORTED":
            project_id = status["project_id"]
            with ContractStore.read_snapshot(root) as (connection, _):
                entry = storage.pointer(connection, project_id, initial=True)
            initial = storage.read_snapshot(root, project_id, entry)
            if (
                entry["discovery_digest"] != discovery_digest
                or initial["confirmations"] != confirmations
            ):
                return _view(root, "ALREADY_MANAGED", project_id)
            return _remember(root, project_id, entry)

        root, observation = scan(root)
        if error := _reject(observation, discovery_digest, confirmations):
            return _view(root, "REJECTED", error=error)
        intent = (
            _read_intent(root)
            if os.path.lexists(root / ".corinthos")
            else _new_intent(root, discovery_digest, confirmations)
        )
        if intent is None:
            return self.status(root)
        project_id = intent["project_id"]
        if (
            intent["discovery_digest"] != discovery_digest
            or intent["confirmations"] != confirmations
        ):
            return _view(root, "INCOMPLETE", project_id, error="IMPORT_INTENT_CHANGED")
        initialize_private(root)
        store = ContractStore(root)
        lifecycle = LifecycleRepository(store)
        with lifecycle.transaction() as connection:
            entry = storage.pointer(connection, project_id, initial=True)
            if entry is None:
                if connection.execute("SELECT 1 FROM lc_projects LIMIT 1").fetchone():
                    raise IntegrityError("Unexpected lifecycle state in the incomplete import.")
                _, current = scan(root)
                if error := _reject(current, discovery_digest, confirmations):
                    return _view(root, "INCOMPLETE", project_id, error=error)
                entry = _save_snapshot(root, project_id, current, confirmations)
                lifecycle.project(
                    project_id, create=True, connection=connection, actor_id="local-operator"
                )
                lifecycle.event(
                    project_id,
                    storage.IMPORT_EVENTS[0],
                    entry,
                    connection=connection,
                    actor_id="local-operator",
                )
            else:
                storage.read_snapshot(root, project_id, entry)
        return _remember(root, project_id, entry)

    def refresh(self, root, expected_snapshot_digest, discovery_digest, confirmations=()):
        from .discovery import scan

        confirmations = _check_inputs(discovery_digest, confirmations)
        storage.require_digest(expected_snapshot_digest)
        root = _root(_absolute_root(root))
        status = self.status(root)
        if status["status"] != "IMPORTED":
            return status
        project_id = status["project_id"]
        lifecycle = LifecycleRepository(ContractStore(root))
        with lifecycle.transaction() as connection:
            current = storage.pointer(connection, project_id)
            if current["snapshot_digest"] != expected_snapshot_digest:
                return _view(root, "REJECTED", project_id, error="SNAPSHOT_CHANGED")
            _, observation = scan(root)
            if error := _reject(observation, discovery_digest, confirmations):
                return _view(root, "REJECTED", project_id, error=error)
            previous = storage.read_snapshot(root, project_id, current)
            if (
                current["discovery_digest"] == discovery_digest
                and previous["confirmations"] == confirmations
            ):
                return _view(root, "IMPORTED", project_id, current)
            count = connection.execute(
                "SELECT COUNT(*) FROM lc_events WHERE project_id = ? AND kind IN (?, ?)",
                (project_id, *storage.IMPORT_EVENTS),
            ).fetchone()[0]
            if count >= storage.MAX_SNAPSHOTS:
                return _view(root, "REJECTED", project_id, error="SNAPSHOT_LIMIT")
            entry = _save_snapshot(root, project_id, observation, confirmations)
            lifecycle.event(
                project_id,
                storage.IMPORT_EVENTS[1],
                entry,
                connection=connection,
                actor_id="local-operator",
            )
        return _view(root, "IMPORTED", project_id, entry)

    def files(self, root, snapshot_digest=None):
        root = _root(_absolute_root(root))
        status = self.status(root)
        if snapshot_digest is not None:
            storage.require_digest(snapshot_digest)
        if status["status"] != "IMPORTED":
            return status, []
        with ContractStore.inspect_snapshot(root) as (connection, _):
            entry = storage.pointer(
                connection, status["project_id"], snapshot_digest=snapshot_digest
            )
        if entry is None:
            return _view(root, "REJECTED", status["project_id"], error="SNAPSHOT_NOT_FOUND"), []
        snapshot = storage.read_snapshot(root, status["project_id"], entry)
        return _view(root, "IMPORTED", status["project_id"], entry), snapshot["observation"][
            "files"
        ]
