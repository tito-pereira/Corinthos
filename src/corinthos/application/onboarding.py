# SPDX-License-Identifier: MPL-2.0
"""Passive onboarding decisions and explicit, inspected desktop opening."""

import os
import stat
from hashlib import sha256

from corinthos import user_state
from corinthos.bootstrap import _MAX_PROFILE, initialize_private
from corinthos.contracts import ConflictError, ContractError, ContractStore, IntegrityError
from corinthos.contracts.artifacts import private_directory
from corinthos.contracts.migration import (
    _ONBOARDING_INTENT,
    _inventory,
    inspect_legacy_state,
    migrate_legacy_state,
    onboarding_migration,
    record_onboarding_migration,
)
from corinthos.contracts.model import canonical_json
from corinthos.external_import.discovery import can_inspect
from corinthos.external_import.service import _INTENT as _IMPORT_INTENT
from corinthos.external_import.service import ExternalProjectImportService
from corinthos.project_creation import _identities, _intent, _recheck, _selected_root
from corinthos.project_registry import ProjectRegistryService
from corinthos.project_status import _inspect_path, project_status
from corinthos.user_state import _checked_file, _lock, _ordinary


def _digest(value):
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fingerprint(root, observation, identities, legacy_digest):
    files = []
    for directory_name in (".corinthos", ".nukytera"):
        directory = root / directory_name
        selected = _ordinary(directory, stat.S_ISDIR)
        if selected is None:
            continue
        files.append([directory_name, selected.st_dev, selected.st_ino])
        for filename in (
            "state.db",
            "bootstrap.json",
            "project-create.json",
            _IMPORT_INTENT,
            _ONBOARDING_INTENT,
        ):
            path = directory / filename
            info = _ordinary(path, stat.S_ISREG)
            if info is None:
                continue
            value = [filename, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            if filename != "state.db":
                limit = _MAX_PROFILE if filename == "bootstrap.json" else 16384
                if info.st_size > limit:
                    raise IntegrityError("Private operation metadata is too large.")
                with _checked_file(path, info) as source:
                    data = source.read(limit + 1)
                if len(data) > limit:
                    raise IntegrityError("Private operation metadata is too large.")
                value.append(sha256(data).hexdigest())
            files.append(value)
    return _digest(
        {
            "observation": observation,
            "directories": [[str(path), info.st_dev, info.st_ino] for path, info in identities],
            "files": files,
            "legacy_digest": legacy_digest,
        }
    )


def inspect_project(root):
    """Classify one supplied root without creating state, migrating or changing recency."""
    root = _selected_root(root)
    result = {
        "root": str(root),
        "status": "UNAVAILABLE",
        "reason": "The selected directory is inaccessible or unavailable.",
        "can_import": False,
        "schema_version": None,
        "inspection_digest": None,
    }
    legacy_digest = None
    try:
        _, exists, is_directory = _inspect_path(root)
        if exists and not is_directory:
            result["reason"] = "Select an ordinary directory."
            return result
        identities = _identities(root)
        state = project_status(root)
        result["root"] = state.root
        result["schema_version"] = state.schema_version
        if not state.exists:
            result.update(status="MISSING", reason="The selected directory does not exist.")
        elif not state.is_directory:
            result["reason"] = "Select an ordinary directory."
        elif state.storage == "UNVERIFIED":
            result.update(
                status="UNVERIFIED",
                reason="Private state is busy or changed. Close its writers and inspect again.",
            )
        elif state.storage == "INVALID":
            result.update(
                status="INVALID",
                reason="Private state could not be verified. Preserve it for inspection.",
            )
        elif os.path.lexists(root / ".nukytera"):
            version, inventory = inspect_legacy_state(root)
            if _IMPORT_INTENT in inventory:
                raise IntegrityError("Legacy import state requires its own recovery.")
            source_digest = _digest(inventory)
            legacy_digest = source_digest
            target_exists = os.path.lexists(root / ".corinthos")
            checkpoint = onboarding_migration(root) if target_exists else None
            if checkpoint is not None and checkpoint["source_digest"] != source_digest:
                raise IntegrityError("Legacy state differs from its migration checkpoint.")
            if (
                target_exists
                and checkpoint is None
                and _inventory(root / ".corinthos") != inventory
            ):
                raise IntegrityError("The private stores differ. Preserve both copies.")
            if checkpoint is not None and checkpoint["phase"] == "COMPLETE":
                if not state.initialized or state.storage != "CURRENT":
                    raise IntegrityError("Completed migration has no verified current target.")
                result.update(status="CURRENT", reason="This managed project is ready to open.")
            else:
                if checkpoint is not None:
                    copied = _inventory(root / ".corinthos")
                    copied.pop(_ONBOARDING_INTENT, None)
                    if copied != inventory:
                        raise IntegrityError(
                            "Interrupted migration requires private-state recovery."
                        )
                result.update(
                    status="MIGRATION_REQUIRED",
                    schema_version=version,
                    reason="Legacy private storage requires explicit verified migration before opening.",
                )
        elif os.path.lexists(root / ".corinthos"):
            _intent(private_directory(root))
            imported = ExternalProjectImportService().status(root)["status"]
            if imported == "INCOMPLETE":
                result.update(
                    status="INCOMPLETE_IMPORT",
                    reason="An incomplete import requires its existing import recovery.",
                )
            elif imported == "INVALID" or not state.initialized:
                result.update(
                    status="INVALID",
                    reason="Private state is incomplete or invalid. Preserve it for recovery.",
                )
            elif state.storage == "LEGACY":
                result.update(
                    status="MIGRATION_REQUIRED",
                    reason="This project needs an explicit database schema upgrade.",
                )
            else:
                result.update(status="CURRENT", reason="This managed project is ready to open.")
        else:
            result.update(
                status="UNMANAGED",
                reason="This directory is not yet managed by Corinthos.",
                can_import=can_inspect(root),
            )
        _recheck(identities)
        if result["status"] in {"CURRENT", "MIGRATION_REQUIRED", "UNMANAGED"}:
            result["inspection_digest"] = _fingerprint(root, result, identities, legacy_digest)
        return result
    except ConflictError:
        result.update(
            status="UNVERIFIED", reason="Private state is busy or changed. Inspect it again."
        )
    except (ContractError, RuntimeError):
        result.update(
            status="INVALID",
            reason=(
                "Preserve both .nukytera and .corinthos. Recover verified private state before retrying."
                if os.path.lexists(root / ".nukytera") and os.path.lexists(root / ".corinthos")
                else "Private state could not be verified. Preserve it for inspection."
            ),
        )
    except (OSError, ValueError):
        result.update(
            status="UNAVAILABLE", reason="The selected directory is inaccessible or unavailable."
        )
    result["can_import"] = False
    result["inspection_digest"] = None
    return result


def open_checked(root, inspection_digest, *, migrate=False):
    """Revalidate a reviewed directory and execute only the explicitly selected operation."""
    if (
        not isinstance(inspection_digest, str)
        or len(inspection_digest) != 64
        or any(character not in "0123456789abcdef" for character in inspection_digest)
        or type(migrate) is not bool
    ):
        raise ValueError("Supply a current directory inspection.")
    root = _selected_root(root)
    lock_root = user_state._path("recent-projects.json", create=True).parent / "cache"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    _ordinary(lock_root, stat.S_ISDIR)
    key = sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    with _lock(lock_root / f"project-create-{key}.lock"):
        identities = _identities(root)
        private = root / ".corinthos"
        for directory in (private, root / ".nukytera"):
            selected = _ordinary(directory, stat.S_ISDIR)
            if selected is not None:
                identities.append((directory, selected))
        observation = inspect_project(str(root))
        _recheck(identities)
        if observation["inspection_digest"] != inspection_digest:
            raise ConflictError("The selected directory changed. Inspect it again.")
        expected = "MIGRATION_REQUIRED" if migrate else "CURRENT"
        if observation["status"] != expected:
            raise ConflictError("This directory requires a different explicit onboarding action.")
        if migrate:
            if os.path.lexists(root / ".nukytera"):
                _, inventory = inspect_legacy_state(root)
                source_digest = _digest(inventory)
                checkpoint = onboarding_migration(root) if os.path.lexists(private) else None
                if checkpoint is None:
                    migrate_legacy_state(root)
                    if _inventory(private) != inventory:
                        raise ConflictError("The migrated copy changed before checkpointing.")
                    if not any(path == private for path, _ in identities):
                        identities.append((private, private.stat()))
                    _recheck(identities)
                    record_onboarding_migration(root, source_digest, "COPIED")
                elif checkpoint["source_digest"] != source_digest:
                    raise ConflictError("Legacy state changed. Inspect it again.")
                _recheck(identities)
                ContractStore(root)
                _recheck(identities)
                initialize_private(root)
                _recheck(identities)
                record_onboarding_migration(root, source_digest, "COMPLETE")
            else:
                ContractStore(root)
            if inspect_project(str(root))["status"] != "CURRENT":
                raise IntegrityError(
                    "Migrated state could not be verified. Preserve it for recovery."
                )
        _recheck(identities)
        ProjectRegistryService().remember(root)
        _recheck(identities)
        from .workspace import project_summary

        return project_summary(str(root))
