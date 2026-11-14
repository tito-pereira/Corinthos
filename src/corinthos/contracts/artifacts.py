# SPDX-License-Identifier: MPL-2.0
"""Contain and verify local evidence references without following links."""

import os
import re
import stat
from hashlib import file_digest
from hashlib import sha256 as _sha256
from pathlib import Path

from corinthos._paths import windows_component_is_reserved

from .model import IntegrityError, StoreError

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_safe_directory(path: Path) -> None:
    info = path.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise StoreError("A private project directory is unsafe.")


def private_directory(project_root: Path, name: str | None = None, *, create: bool = False) -> Path:
    """Return a private directory under an existing, caller-authorized project."""
    if name not in {None, "artifacts", "snapshots"}:
        raise StoreError("Unknown private project directory.")
    try:
        root = Path(project_root).resolve(strict=True)
        if not root.is_dir():
            raise StoreError("The project root must be an existing directory.")
        private = root / ".corinthos"
        if create:
            try:
                (root / ".nukytera").lstat()
            except FileNotFoundError:
                pass
            else:
                try:
                    _require_safe_directory(private)
                    (private / "state.db").lstat()
                except FileNotFoundError:
                    raise StoreError(
                        "Legacy private state requires explicit migration before opening a Corinthos store."
                    ) from None
        directories = [private] if name is None else [private, private / name]
        for path in directories:
            if create:
                path.mkdir(mode=0o700, exist_ok=True)
            _require_safe_directory(path)
        return directories[-1]
    except (OSError, RuntimeError):
        raise StoreError("The private project directory is unavailable.") from None


def verify_artifact(
    project_root: Path, relative_path: str, sha256: str, *, max_bytes: int | None = None
) -> None:
    """Verify an existing regular artifact by a portable relative path and SHA-256."""
    if not isinstance(relative_path, str) or not isinstance(sha256, str):
        raise IntegrityError("An artifact reference requires a path and digest.")
    parts = relative_path.split("/")
    if not _SHA256.fullmatch(sha256) or any(
        not _COMPONENT.fullmatch(part) or part.endswith(".") or windows_component_is_reserved(part)
        for part in parts
    ):
        raise IntegrityError("The artifact path or digest is invalid.")
    try:
        base = private_directory(project_root, "artifacts")
        path = base
        for index, part in enumerate(parts):
            path = path / part
            info = path.lstat()
            if is_link(info):
                raise IntegrityError("Artifact references cannot follow links.")
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise IntegrityError("An artifact path component is not a directory.")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IntegrityError("Artifact evidence must be an unlinked regular file.")
        if not path.resolve(strict=True).is_relative_to(base):
            raise IntegrityError("Artifact path escapes private storage.")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not os.path.samestat(info, before):
                raise IntegrityError("The artifact changed while it was opened.")
            if max_bytes is None:
                observed = file_digest(stream, "sha256").hexdigest()
            else:
                if before.st_size > max_bytes:
                    raise IntegrityError("The artifact exceeds its verification size limit.")
                data = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
                if (
                    len(data) != before.st_size
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                ):
                    raise IntegrityError("The artifact changed during bounded verification.")
                observed = _sha256(data).hexdigest()
    except (OSError, StoreError, RuntimeError):
        raise IntegrityError("The referenced artifact could not be read safely.") from None
    if observed != sha256:
        raise IntegrityError("The artifact digest does not match the evidence reference.")
