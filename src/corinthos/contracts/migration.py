# SPDX-License-Identifier: MPL-2.0
"""Copy a quiescent legacy private store without rewriting its records."""

import ctypes
import errno
import json
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing
from hashlib import file_digest
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic

from .artifacts import is_link, verify_artifact
from .model import (
    ConflictError,
    ContractRef,
    IntegrityError,
    StoreError,
    canonical_json,
    iter_references,
)
from .schema import validate_schema
from .store import (
    _APPLICATION_ID,
    _REVISIONS,
    _SUPPORTED_VERSIONS,
    _markers,
    _stored_document,
    _verify_database,
)

_ONBOARDING_INTENT = "onboarding-migration.json"


def _directory_identity(path):
    _directory(path)
    info = path.stat()
    return [str(info.st_dev), str(info.st_ino)]


def _legacy_identity(root):
    legacy = root / ".nukytera"
    _directory(legacy)
    database = legacy / "state.db"
    try:
        _check_database(database)
    except FileNotFoundError:
        raise IntegrityError("Legacy storage has no verified state database.") from None
    info = database.stat()
    return [
        *_directory_identity(legacy),
        str(info.st_dev),
        str(info.st_ino),
        str(info.st_size),
        str(info.st_mtime_ns),
    ]


def onboarding_migration(root):
    """Read a bounded, identity-bound GUI migration checkpoint."""
    from corinthos.user_state import _checked_file, _ordinary

    path = root / ".corinthos" / _ONBOARDING_INTENT
    info = _ordinary(path, stat.S_ISREG)
    if info is None:
        return None
    try:
        if info.st_size > 4096:
            raise ValueError
        with _checked_file(path, info) as source:
            raw = source.read(4097)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.keys()
            != {"version", "phase", "source_digest", "source_identity", "target_identity"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["phase"] not in {"COPIED", "COMPLETE"}
            or not isinstance(value["source_digest"], str)
            or len(value["source_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in value["source_digest"])
            or value["source_identity"] != _legacy_identity(root)
            or value["target_identity"] != _directory_identity(root / ".corinthos")
            or canonical_json(value).encode("utf-8") != raw
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise IntegrityError(
            "The migration checkpoint is invalid. Preserve private state."
        ) from None


def record_onboarding_migration(root, source_digest, phase):
    """Atomically checkpoint the explicit copy/upgrade operation inside its private directory."""
    private = root / ".corinthos"
    selected = _directory_identity(private)
    value = {
        "version": 1,
        "phase": phase,
        "source_digest": source_digest,
        "source_identity": _legacy_identity(root),
        "target_identity": selected,
    }
    with NamedTemporaryFile(dir=private, prefix=".onboarding-migration-", delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(canonical_json(value).encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
            target.close()
            if _directory_identity(private) != selected:
                raise ConflictError("Private state changed during migration.")
            temporary.replace(private / _ONBOARDING_INTENT)
        finally:
            temporary.unlink(missing_ok=True)


def inspect_legacy_state(root):
    """Verify supported, quiescent legacy storage without copying or writing sidecars."""
    root = Path(root)
    selected = _legacy_identity(root)
    before = _inventory(root / ".nukytera")
    deadline = monotonic() + 2
    database = root / ".nukytera" / "state.db"
    try:
        with closing(
            sqlite3.connect(
                f"{database.as_uri()}?mode=ro&immutable=1", uri=True, timeout=0, autocommit=True
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.set_progress_handler(lambda: monotonic() >= deadline, 1000)
            application, version = _markers(connection)
            if application != _APPLICATION_ID or version not in _SUPPORTED_VERSIONS:
                raise StoreError("The legacy database application or schema is unsupported.")

            def verify(relative, digest):
                if before.get(f"artifacts/{relative}") != digest:
                    raise IntegrityError("Legacy artifact evidence differs from its reference.")

            _verify_snapshot_connection(connection, verify)
        if _legacy_identity(root) != selected or _inventory(root / ".nukytera") != before:
            raise ConflictError("Legacy private state changed during inspection.")
        return version, before
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_INTERRUPT:
            raise ConflictError(
                "Legacy inspection timed out. Retry after closing its writers."
            ) from None
        raise StoreError("The legacy state database cannot be inspected safely.") from None


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _directory(path: Path) -> None:
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise StoreError("Private migration directories must be unlinked directories.")


def _inventory(directory: Path) -> dict[str, str | None]:
    _directory(directory)
    entries = {}
    folded = set()
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            info = path.lstat()
            relative = path.relative_to(directory).as_posix()
            if relative.casefold() in folded or is_link(info):
                raise StoreError("Private migration contains a link or case-insensitive collision.")
            folded.add(relative.casefold())
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
                entries[relative] = None
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                with path.open("rb") as source:
                    if not os.path.samestat(info, os.fstat(source.fileno())):
                        raise ConflictError("A private file changed during migration.")
                    entries[relative] = file_digest(source, "sha256").hexdigest()
            else:
                raise StoreError("Private migration requires unlinked regular files.")
    return entries


def _check_database(database: Path) -> None:
    info = database.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StoreError("The legacy state database must be an unlinked regular file.")
    if not info.st_mode & stat.S_IWUSR:
        raise StoreError("The legacy state database is read-only.")
    if any(
        _exists(database.with_name(database.name + suffix))
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ConflictError(
            "Close the legacy store cleanly before migration. SQLite sidecars remain."
        )
    with database.open("rb") as source:
        header = source.read(20)
    if not header.startswith(b"SQLite format 3\x00"):
        raise StoreError("The legacy state database is not a valid SQLite file.")
    if header[18:20] != b"\x01\x01":
        raise StoreError("Migration requires a closed rollback-journal SQLite database.")


def _verify_snapshot(root: Path) -> None:
    # A directory naming migration preserves database bytes. Opening the
    # current ContractStore here would perform a separate SQLite upgrade.
    database = root / ".corinthos" / "state.db"
    uri = f"{database.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0, autocommit=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        _verify_snapshot_connection(
            connection, lambda path, digest: verify_artifact(root, path, digest)
        )


def _verify_snapshot_connection(connection, verify):
    """Apply the canonical copy verification to a supplied read-only connection."""
    _verify_database(connection, _markers(connection)[1])
    documents = {}
    for row in connection.execute(_REVISIONS):
        document = _stored_document(row)
        validate_schema(document)
        documents[ContractRef.from_document(document)] = document
    expected_edges = set()
    for source, document in documents.items():
        for target in iter_references(document):
            if target.project_id != source.project_id or target not in documents:
                raise IntegrityError("A migrated document has an unresolved exact reference.")
            expected_edges.add((source.revision_id, target.revision_id))
        payload = document["payload"]
        if source.contract_type == "VALIDATION_RESULT":
            evidence = payload["evidence_refs"]
        elif source.contract_type == "EVIDENCE_REPORT":
            evidence = payload["artifact_and_evidence_index"]
        else:
            evidence = ()
        for item in evidence:
            verify(item["path"], item["sha256"])
    if expected_edges != {
        tuple(row) for row in connection.execute("SELECT * FROM contract_references")
    }:
        raise IntegrityError("The migrated reference index disagrees with its documents.")
    if {ref.revision_id for ref in documents} != {
        row[0] for row in connection.execute("SELECT revision_id FROM audit_events")
    }:
        raise IntegrityError("The migrated audit events disagree with published revisions.")


def _promote(staged: Path, target: Path) -> None:
    if os.name == "nt":
        try:
            staged.rename(target)
        except FileExistsError:
            raise ConflictError(
                "The target .corinthos directory appeared during migration."
            ) from None
        return
    # A POSIX rename can replace an existing empty directory.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise StoreError("The platform does not support an atomic no-replace directory move.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    at_fdcwd, rename_noreplace = -100, 1
    if rename(at_fdcwd, os.fsencode(staged), at_fdcwd, os.fsencode(target), rename_noreplace) != 0:
        if ctypes.get_errno() == errno.EEXIST:
            raise ConflictError("The target .corinthos directory appeared during migration.")
        raise StoreError("The filesystem refused the atomic no-replace directory move.")


def migrate_legacy_state(project_root: str | Path) -> bool:
    """Verify and copy .nukytera to .corinthos while preserving the original.

    The caller authorizes the project root and stops all users of both private
    directories, including artifact writers. An identical verified copy is a no-op.
    """
    try:
        root = Path(project_root).resolve(strict=True)
        _directory(root)
        legacy, target = root / ".nukytera", root / ".corinthos"
        if not _exists(legacy):
            return False
        _directory(legacy)
        database = legacy / "state.db"
        _check_database(database)
        uri = f"{database.as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=0, autocommit=True)) as connection:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN EXCLUSIVE")
            _check_database(database)
            application, version = _markers(connection)
            if application != _APPLICATION_ID or version not in _SUPPORTED_VERSIONS:
                raise StoreError(
                    "The legacy state database application or schema version is unsupported."
                )
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StoreError("The legacy state database failed its integrity check.")
            original = _inventory(legacy)
            if _exists(target):
                if _inventory(target) != original:
                    raise ConflictError(
                        "The existing .corinthos directory differs from the legacy store."
                    )
                _verify_snapshot(root)
                return False
            if os.name == "nt" and sys.version_info < (3, 12, 4):
                raise StoreError(
                    "Private Windows migration staging requires Python 3.12.4 or later."
                )
            with TemporaryDirectory(prefix=".corinthos-migration-", dir=root) as scratch:
                stage_root = Path(scratch)
                staged = stage_root / ".corinthos"
                shutil.copytree(legacy, staged, symlinks=True)
                if os.name != "nt":
                    staged.chmod(0o700)
                if _inventory(staged) != original:
                    raise IntegrityError("The private state copy does not match the original.")
                _verify_snapshot(stage_root)
                if _inventory(staged) != original or _inventory(legacy) != original:
                    raise ConflictError("Private state changed during migration.")
                if _exists(target):
                    raise ConflictError(
                        "The target .corinthos directory appeared during migration."
                    )
                _promote(staged, target)
                return True
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", 0) & 255 in (
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        ):
            raise ConflictError("The legacy store is busy. Close it before migration.") from None
        raise StoreError("The legacy SQLite database cannot be verified safely.") from None
    except OSError:
        raise StoreError(
            "Private migration could not access or publish its files safely."
        ) from None
