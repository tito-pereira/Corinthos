# SPDX-License-Identifier: MPL-2.0
"""Immutable import snapshots owned by the existing project lifecycle."""

import json
import os
import re
import stat
import tempfile
from hashlib import sha256

from corinthos.contracts import IntegrityError, StoreError
from corinthos.contracts.artifacts import private_directory
from corinthos.contracts.model import ID_PATTERN, canonical_json
from corinthos.user_state import _checked_file, _ordinary

FORMAT_VERSION = 1
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOTS = 32
IMPORT_EVENTS = ("EXTERNAL_PROJECT_IMPORTED", "EXTERNAL_PROJECT_REFRESHED")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def require_digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("Supply the exact SHA-256 import digest.")
    return value


def imported_project_ids(connection):
    """Use only completed, lifecycle-owned imports to seed operational identity."""
    return [
        row[0]
        for row in connection.execute(
            """SELECT DISTINCT e.project_id FROM lc_events e JOIN lc_projects p USING (project_id)
           WHERE e.kind = ? ORDER BY e.project_id LIMIT 2""",
            (IMPORT_EVENTS[0],),
        )
    ]


def pointer(connection, project_id, *, snapshot_digest=None, initial=False):
    where = "project_id = ? AND kind IN (?, ?)"
    params = [project_id, *IMPORT_EVENTS]
    if initial:
        where += " AND kind = ?"
        params.append(IMPORT_EVENTS[0])
    if snapshot_digest is not None:
        where += " AND json_extract(data, '$.snapshot_digest') = ?"
        params.append(require_digest(snapshot_digest))
    row = connection.execute(
        f"SELECT data FROM lc_events WHERE {where} ORDER BY sequence DESC LIMIT 1", params
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        require_digest(data["snapshot_digest"])
        require_digest(data["discovery_digest"])
        if set(data) != {"snapshot_digest", "discovery_digest", "summary"} or not isinstance(
            data["summary"], dict
        ):
            raise ValueError
    except (TypeError, KeyError, ValueError, RecursionError):
        raise IntegrityError("The import reference is invalid.") from None
    return data


def current_snapshot_digest(connection, project_id):
    entry = pointer(connection, project_id)
    return entry["snapshot_digest"] if entry else None


def current_snapshot(connection, root, project_id, *, snapshot_digest=None, initial=False):
    entry = pointer(connection, project_id, snapshot_digest=snapshot_digest, initial=initial)
    if entry is None:
        return None
    return read_snapshot(root, project_id, entry)


def _decode(raw, snapshot_digest, project_id, discovery_digest):
    from .discovery import discovery_digest as semantic_digest

    try:
        if len(raw) > MAX_SNAPSHOT_BYTES or sha256(raw).hexdigest() != snapshot_digest:
            raise ValueError
        data = json.loads(raw)
        if (
            set(data) != {"format_version", "project_id", "observation", "confirmations"}
            or type(data["format_version"]) is not int
            or data["format_version"] != FORMAT_VERSION
            or data["project_id"] != project_id
            or not ID_PATTERN.fullmatch(project_id)
            or not isinstance(data["observation"], dict)
            or data["observation"].get("discovery_digest") != discovery_digest
            or semantic_digest(data["observation"]) != discovery_digest
            or not isinstance(data["observation"].get("files"), list)
            or not isinstance(data["confirmations"], list)
            or data["confirmations"] != sorted(set(data["confirmations"]))
            or any(
                not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                for code in data["confirmations"]
            )
            or canonical_json(data).encode("utf-8") != raw
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise IntegrityError("The immutable import snapshot failed verification.") from None
    return data | {"snapshot_digest": snapshot_digest}


def read_snapshot(root, project_id, entry):
    digest = require_digest(entry["snapshot_digest"])
    path = private_directory(root, "snapshots") / f"external-import-{digest}.json"
    try:
        info = _ordinary(path, stat.S_ISREG)
        if info is None or info.st_size > MAX_SNAPSHOT_BYTES:
            raise StoreError("The immutable import snapshot is missing or oversized.")
        with _checked_file(path, info) as source:
            raw = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) != info.st_size:
            raise StoreError("The immutable import snapshot changed while reading.")
    except OSError:
        raise StoreError("The immutable import snapshot could not be read.") from None
    return _decode(raw, digest, project_id, entry["discovery_digest"])


def save_snapshot(root, project_id, observation, confirmations):
    data = {
        "format_version": FORMAT_VERSION,
        "project_id": project_id,
        "observation": observation,
        "confirmations": confirmations,
    }
    raw = canonical_json(data).encode("utf-8")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise IntegrityError("The import snapshot exceeds its storage limit.")
    digest = sha256(raw).hexdigest()
    entry = {"snapshot_digest": digest, "discovery_digest": observation["discovery_digest"]}
    target = private_directory(root, "snapshots", create=True)
    path = target / f"external-import-{digest}.json"
    directory = target.stat()
    with tempfile.NamedTemporaryFile(dir=target, prefix=".external-import-", delete=False) as file:
        temporary = file.name
        try:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            if not os.path.samestat(directory, private_directory(root, "snapshots").stat()):
                raise IntegrityError("Private snapshot storage moved before publication.")
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                    os.unlink(temporary)
            except FileExistsError:
                pass
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
    return read_snapshot(root, project_id, entry)
