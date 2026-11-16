# SPDX-License-Identifier: MPL-2.0
"""Explicit, retry-safe private initialization without project or approval authority."""

import json
import os
import stat
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from corinthos import user_state
from corinthos._paths import windows_component_is_reserved
from corinthos.bootstrap import _root, initialize_private
from corinthos.contracts import ConflictError, ContractError, IntegrityError
from corinthos.contracts.artifacts import private_directory
from corinthos.contracts.migration import _promote
from corinthos.contracts.model import canonical_json
from corinthos.external_import.service import _INTENT as _IMPORT_INTENT
from corinthos.external_import.service import ExternalProjectImportService
from corinthos.project_registry import ProjectRegistryService, validate_display_name
from corinthos.project_status import _absolute_root, project_status
from corinthos.user_state import _checked_file, _lock, _ordinary

_INTENT = "project-create.json"


class ProjectCreationError(ContractError):
    """A bounded recovery category carried through the existing RPC error boundary."""

    def __init__(self, creation_code, message):
        super().__init__(message)
        self.creation_code = creation_code


def _selected_root(value):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise ValueError("Supply an explicit absolute local project directory.")
    path = _absolute_root(value)
    if ".." in path.parts or (
        os.name == "nt" and any(windows_component_is_reserved(part) for part in path.parts[1:])
    ):
        raise ValueError("Supply an ordinary absolute local project directory.")
    return Path(os.path.abspath(path))


def _identities(path):
    """Pin every existing ancestor, including a selected root that already exists."""
    _root(path, missing_ok=True)
    result = []
    for parent in [*reversed(path.parents), path]:
        info = _ordinary(parent, stat.S_ISDIR)
        if info is None:
            break
        result.append((parent, info))
    return result


def _recheck(identities):
    for path, selected in identities:
        current = _ordinary(path, stat.S_ISDIR)
        if current is None or not os.path.samestat(selected, current):
            raise IntegrityError("The selected project path changed. Select it again.")


def _intent(private):
    path = private / _INTENT
    info = _ordinary(path, stat.S_ISREG)
    if info is None:
        return None
    try:
        if info.st_size > 4096:
            raise ValueError
        with _checked_file(path, info) as source:
            data = source.read(4097)
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or value.keys() != {"version", "display_name"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or validate_display_name(value["display_name"]) != value["display_name"]
            or canonical_json(value).encode("utf-8") != data
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IntegrityError("The creation intent is invalid. Preserve private state.") from None


def _write_intent(private, name):
    path = private / _INTENT
    selected = private.stat()
    with NamedTemporaryFile(dir=private, prefix=".project-create-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(canonical_json({"version": 1, "display_name": name}).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            if not os.path.samestat(selected, _ordinary(private, stat.S_ISDIR)):
                raise IntegrityError("Private storage changed before recording creation intent.")
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _verify_intent(private, name):
    if _intent(private) != {"version": 1, "display_name": name}:
        raise IntegrityError(
            "The expected creation intent is missing or changed. Preserve private state."
        )


def _check_import(root, private):
    if (
        os.path.lexists(private / _IMPORT_INTENT)
        and ExternalProjectImportService().status(root)["status"] != "IMPORTED"
    ):
        raise IntegrityError(
            "Incomplete or invalid import state requires its existing import recovery path."
        )


def _stage(root, name, identities):
    # Root-local private staging also supports a writable project with a read-only parent.
    temporary = TemporaryDirectory(dir=root, prefix=".corinthos-create-", delete=False)
    with temporary as directory:
        staged_root = Path(directory)
        selected = staged_root.stat()
        stage_identities = [*identities, (staged_root, selected)]
        private_info = None
        published = False
        try:
            _recheck(stage_identities)
            private = private_directory(staged_root, create=True)
            private_info = private.stat()
            source_identities = [*stage_identities, (private, private_info)]
            _recheck(source_identities)
            _write_intent(private, name)
            _recheck(source_identities)
            initialize_private(staged_root)
            _recheck(source_identities)
            _verify_intent(private, name)
            staged = project_status(staged_root)
            if not staged.initialized or staged.storage != "ABSENT":
                raise IntegrityError("The staged private initialization did not verify.")
            _recheck(source_identities)
            _verify_intent(private, name)
            _promote(private, root / ".corinthos")
            published = True
            _recheck([*identities, (root / ".corinthos", private_info)])
            _verify_intent(root / ".corinthos", name)
            return private_info
        finally:
            # Changed roots or staging sources retain their bytes for inspection.
            try:
                if private_info is not None:
                    _recheck(stage_identities)
                    if not published:
                        _recheck([(private, private_info)])
                    # Nonempty stages may contain unexpected data; never remove them recursively.
                    staged_root.rmdir()
            except (ContractError, OSError):
                pass


def _create(root, name):
    identities = _identities(root)
    registry = ProjectRegistryService()
    saved_name = registry.metadata(str(root)).get("display_name")
    if saved_name is not None and saved_name != name:
        raise ConflictError("Creation conflicts with the saved project name.")
    status = project_status(root)
    if status.exists and not status.is_directory:
        raise IntegrityError("The selected project root is not an ordinary directory.")
    if os.path.lexists(root / ".nukytera") or status.storage in {"LEGACY", "INVALID", "UNVERIFIED"}:
        raise IntegrityError("Existing private state requires inspection or explicit migration.")
    existing = os.path.lexists(root / ".corinthos")
    if existing:
        private = private_directory(root)
        private_info = private.stat()
        _check_import(root, private)
        _recheck([*identities, (private, private_info)])
        intent = _intent(private)
        if intent is not None and intent["display_name"] != name:
            raise ConflictError("Retry creation with the original project name.")
        if not status.initialized:
            raise IntegrityError("Unverified private state cannot be adopted by creation.")
    _recheck(identities)
    root = _root(root, create=True)
    _recheck(identities)
    identities = _identities(root)
    if existing:
        if not os.path.samestat(private_info, private_directory(root).stat()):
            raise IntegrityError("Private storage changed before creation.")
        initialize_private(root)
        _recheck(identities)
        if not os.path.samestat(private_info, private_directory(root).stat()):
            raise IntegrityError("Private storage changed during creation.")
        if intent is None:
            _write_intent(private, name)
    else:
        private_info = _stage(root, name, identities)
    private = root / ".corinthos"
    project_identities = [*identities, (private, private_info)]
    _recheck(project_identities)
    _check_import(root, private)
    _verify_intent(private, name)
    verified = project_status(root)
    if not verified.initialized or verified.storage not in {"ABSENT", "CURRENT"}:
        raise IntegrityError("Initialized state could not be verified. Preserve it for inspection.")
    _recheck(project_identities)
    _verify_intent(private, name)
    try:
        registry.remember(root, name=name)
    except ConflictError:
        raise
    except (ContractError, OSError):
        raise ProjectCreationError(
            "REGISTRY_INCOMPLETE",
            "Private initialization succeeded. Retry Create with the same name and directory to register it.",
        ) from None
    _recheck(project_identities)
    _verify_intent(private, name)
    from corinthos.application.workspace import project_summary

    return project_summary(str(root))


def create_project(root, display_name):
    """Serialize the complete creation/recovery operation for one canonical local root."""
    root, name = _selected_root(root), validate_display_name(display_name)
    try:
        if user_state.state_root().resolve().is_relative_to(root):
            raise IntegrityError("The per-user registry must be outside the selected project.")
        lock_root = user_state._path("recent-projects.json", create=True).parent / "cache"
        _ordinary(lock_root, stat.S_ISDIR)
        lock_root.mkdir(mode=0o700, exist_ok=True)
        _ordinary(lock_root, stat.S_ISDIR)
        key = sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
        with _lock(lock_root / f"project-create-{key}.lock"):
            return _create(root, name)
    except ProjectCreationError:
        raise
    except ConflictError as error:
        raise ProjectCreationError("CONFLICT", str(error)) from error
    except OSError as error:
        raise ProjectCreationError(
            "ACCESS_DENIED", "The selected project directory is unavailable."
        ) from error
    except ContractError as error:
        raise ProjectCreationError("STATE_UNAVAILABLE", str(error)) from error
