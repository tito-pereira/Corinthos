# SPDX-License-Identifier: MPL-2.0
"""Share bounded project import operations across terminal and desktop."""

import json
from itertools import islice

from corinthos.application.workspace import _PAGE_BUDGET, _page, _page_args
from corinthos.external_import.service import ExternalProjectImportService

_MANIFEST_FACTS = ("dependencies", "scripts", "members")


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def import_inspect(root, *, limit=10, offset=0):
    _page_args(limit, offset)
    result = ExternalProjectImportService().inspect(root)
    files = result.pop("files")
    manifests = result["manifests"]
    result["manifests"] = [dict(record) for record in manifests]
    result["manifest_facts_truncated"] = False
    records = []
    preview = result | {
        "files": records,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit,
    }
    remaining = _PAGE_BUDGET - _size(preview)
    if remaining < 0:
        for record in reversed(result["manifests"]):
            for field in reversed(_MANIFEST_FACTS):
                occurrences = record[field]
                if occurrences:
                    remaining += _size(occurrences) - 2
                    record[field] = []
                    preview["manifest_facts_truncated"] = True
                if remaining >= 0:
                    break
            if remaining >= 0:
                break
    # Leave enough space for at least one bounded file row when a page exists.
    for record in islice(files, offset, offset + limit):
        size = _size(record) + 1
        if records and remaining < size:
            break
        records.append(record)
        remaining -= size
    preview["next_offset"] = offset + len(records) if offset + len(records) < len(files) else None
    return preview


def import_commit(root, discovery_digest, confirmations=()):
    return ExternalProjectImportService().commit(root, discovery_digest, confirmations)


def import_status(root):
    return ExternalProjectImportService().status(root)


def import_refresh(root, expected_snapshot_digest, discovery_digest, confirmations=()):
    return ExternalProjectImportService().refresh(
        root, expected_snapshot_digest, discovery_digest, confirmations
    )


def import_files(root, *, snapshot_digest=None, limit=10, offset=0):
    _page_args(limit, offset)
    result, files = ExternalProjectImportService().files(root, snapshot_digest)
    page = _page("files", files[offset : offset + limit], limit, offset)
    if offset + len(page["files"]) >= len(files):
        page["next_offset"] = None
    return result | page
