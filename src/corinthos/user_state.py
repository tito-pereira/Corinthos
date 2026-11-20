# SPDX-License-Identifier: MPL-2.0
"""Platform paths and versioned, non-authoritative per-user state."""

import json
import os
import stat
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

from corinthos.contracts import IntegrityError
from corinthos.contracts.artifacts import is_link

_FILES = {"config.json", "providers.json", "recent-projects.json", "updates.json"}
_MAX_BYTES = 128 * 1024


def state_root() -> Path:
    """Return the current platform's per-user state directory."""
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if not value or not Path(value).is_absolute():
            raise IntegrityError("LOCALAPPDATA must identify the current user's local directory.")
        return Path(value) / "Corinthos"
    value = os.environ.get("XDG_STATE_HOME")
    base = Path(value) if value and Path(value).is_absolute() else Path.home() / ".local/state"
    return base / "corinthos"


def _ordinary(path: Path, kind) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not kind(info.st_mode) or is_link(info) or (kind is stat.S_ISREG and info.st_nlink != 1):
        raise IntegrityError("Corinthos user state must use ordinary local files and directories.")
    return info


def _path(filename: str, *, create: bool = False) -> Path:
    if filename not in _FILES:
        raise IntegrityError("Unknown Corinthos user state document.")
    root = state_root()
    if (
        not root.is_absolute()
        or ".." in root.parts
        or (os.name == "nt" and root.drive.startswith("\\\\"))
    ):
        raise IntegrityError("Corinthos user state must use an unredirected local directory.")
    for parent in root.parents:
        _ordinary(parent, stat.S_ISDIR)
    if not _ordinary(root, stat.S_ISDIR) and create:
        root.mkdir(parents=True, exist_ok=True)
        _ordinary(root, stat.S_ISDIR)
    return root / filename


def _validate(filename: str, document):
    supported = {1, 2, 3} if filename == "recent-projects.json" else {1}
    if (
        not isinstance(document, dict)
        or type(document.get("version")) is not int
        or document["version"] not in supported
    ):
        raise IntegrityError("Unsupported Corinthos user state document version.")
    return document


def _checked_file(path: Path, info: os.stat_result | None, *, lock: bool = False):
    flags = os.O_RDWR if lock else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if info is None:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        actual = os.fstat(fd)
        linked = _ordinary(path, stat.S_ISREG)
        if (
            linked is None
            or not os.path.samestat(linked, actual)
            or (info is not None and not os.path.samestat(info, actual))
        ):
            raise IntegrityError("Corinthos user state changed while it was being opened.")
        return os.fdopen(fd, "r+b" if lock else "rb")
    except BaseException:
        os.close(fd)
        raise


def _read(path: Path, default: dict) -> dict:
    info = _ordinary(path, stat.S_ISREG)
    if info is None:
        return _validate(path.name, default)
    try:
        with _checked_file(path, info) as source:
            data = source.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError("User state document exceeds size limit.")
        return _validate(path.name, json.loads(data))
    except (ValueError, RecursionError) as error:
        raise IntegrityError("Invalid Corinthos user state document.") from error


def read_state(filename: str, default: dict) -> dict:
    """Read a complete state snapshot or return its default without writing."""
    return _read(_path(filename), default)


@contextmanager
def _lock(path: Path):
    info = _ordinary(path, stat.S_ISREG)
    try:
        file = _checked_file(path, info, lock=True)
    except FileExistsError:
        info = _ordinary(path, stat.S_ISREG)
        if info is None:
            raise IntegrityError("The Corinthos user state lock changed while opening.")
        file = _checked_file(path, info, lock=True)
    with file:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        yield


def update_state(filename: str, default: dict, transform) -> dict:
    """Serialize read-modify-write and replace the document only when valid."""
    path = _path(filename, create=True)
    with _lock(path.with_suffix(".lock")):
        current = _read(path, default)
        updated = _validate(filename, transform(deepcopy(current)))
        if updated == current and path.is_file():
            return updated
        try:
            data = (
                json.dumps(updated, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
            ).encode()
        except (ValueError, TypeError, RecursionError) as error:
            raise IntegrityError("Invalid Corinthos user state document.") from error
        if len(data) > _MAX_BYTES:
            raise IntegrityError("Corinthos user state document exceeds its size limit.")
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
                file.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return updated


def initialize() -> None:
    """Prepare convenience state for a per-user installation."""
    for filename, default in (
        ("config.json", {"version": 1}),
        ("recent-projects.json", {"version": 1, "projects": []}),
    ):
        update_state(filename, default, lambda current: current)
    for name in ("logs", "cache"):
        path = state_root() / name
        path.mkdir(exist_ok=True)
        _ordinary(path, stat.S_ISDIR)


if __name__ == "__main__":
    try:
        initialize()
    except (IntegrityError, OSError) as error:
        print(f"Could not initialize Corinthos user state: {error}", file=sys.stderr)
        sys.exit(1)
