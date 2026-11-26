# SPDX-License-Identifier: MPL-2.0
"""Bounded local documents and interprocess locks on ordinary paths."""

import json
import os
import stat
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile

from corinthos.errors import ConflictError, IntegrityError


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def plain_path(path, *, file=False):
    path = Path(path).absolute()
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        expected = stat.S_ISREG if file and current == path else stat.S_ISDIR
        if (
            is_link(info)
            or not expected(info.st_mode)
            or (file and current == path and info.st_nlink != 1)
        ):
            raise IntegrityError("Use ordinary local paths without links or hardlinks.")


def read_json(path, default=None, *, max_bytes=262144):
    path = Path(path)
    plain_path(path, file=True)
    try:
        with path.open("rb") as source:
            data = source.read(max_bytes + 1)
    except FileNotFoundError:
        return deepcopy(default)

    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("Duplicate keys")
        return value

    try:
        if len(data) > max_bytes:
            raise ValueError("Document size limit")
        return json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, RecursionError) as error:
        raise IntegrityError("Invalid or oversized local JSON document.") from error


def atomic_json(path, value, *, max_bytes=262144):
    path = Path(path)
    plain_path(path, file=True)
    try:
        data = (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
    except (TypeError, ValueError, RecursionError) as error:
        raise IntegrityError("Invalid local JSON value.") from error
    if len(data) > max_bytes:
        raise IntegrityError("Local JSON document exceeds its size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    plain_path(path.parent)
    with NamedTemporaryFile(dir=path.parent, prefix=".corinthos-", delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
            target.close()
            plain_path(path, file=True)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path, *, blocking=True):
    path = Path(path)
    plain_path(path, file=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        plain_path(path, file=True)
        if not os.path.samestat(path.stat(), os.fstat(stream.fileno())):
            raise IntegrityError("Lock identity changed while opening.")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as error:
            raise ConflictError("Another process owns this operation.") from error
        try:
            yield stream
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
