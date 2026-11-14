# SPDX-License-Identifier: MPL-2.0
"""Select bounded repository Context from verified import observations."""

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from corinthos.context.index import _source
from corinthos.context.model import Source
from corinthos.context.redaction import redact, sanitize_text, secret_path
from corinthos.contracts import ConflictError, IntegrityError
from corinthos.work.repository import WorkRepository

_POLICY_VERSION = 1
_TEXT_SCAN_LIMIT = 32
_MAX_FILE_BYTES = 64 * 1024
_MAX_EXCERPT_BYTES = 4 * 1024
_MAX_EXCERPT_LINES = 24
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset(
    "add all an and are as be by file files fix for from implement in into is it of on or "
    "per preserve set should test tests that the this to update use using when with work "
    "app apps code doc docs lib source src verify".split()
)
_PATH_REFERENCES = re.compile(r"(?<![\w.-])(?:[\w.-]+[/\\])*[\w-]+(?:\.[\w-]+)+")
_SOURCE_KINDS = {
    "MANIFEST": "IMPORT_MANIFEST",
    "DOCUMENTATION": "IMPORT_DOCUMENT",
    "SOURCE": "IMPORT_SOURCE_FILE",
    "TEST": "IMPORT_TEST_FILE",
}


@dataclass(frozen=True, slots=True)
class ImportCapture:
    snapshot_digest: str | None
    work_updated_at: str | None
    sources: tuple[Source, ...]


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return {
        token
        for token in _TOKENS.findall(normalized)
        if len(token) > 1 and token not in _STOP_WORDS
    }


def _path_key(path):
    return path.casefold(), path


def _path_tokens(value):
    path = PurePosixPath(value.replace("\\", "/"))
    return _tokens("/".join((*path.parts[:-1], path.stem)))


def _work_terms(value):
    value = sanitize_text(value)
    return _tokens(
        _PATH_REFERENCES.sub(lambda match: " ".join(sorted(_path_tokens(match[0]))), value)
    )


def _important_document(path):
    location = PurePosixPath(path)
    stem = location.stem.casefold()
    if stem in {"readme", "architecture", "design"}:
        return len(location.parts), _path_key(path)
    return None


def _excerpt(text, terms):
    lines = text.splitlines()
    if not lines:
        return {"start_line": 0, "end_line": 0, "text": "", "truncated": False}
    scores = [len(_tokens(line) & terms) for line in lines]
    best = max(range(len(lines)), key=scores.__getitem__)
    start = max(0, best - 2) if scores[best] else 0
    if best > start and sum(1 + len(line.encode("utf-8")) for line in lines[start:best]) > 1024:
        start = best
    selected, truncated = [], False
    total = 0
    for line in lines[start : start + _MAX_EXCERPT_LINES]:
        length = len(line.encode("utf-8"))
        if total + length + bool(selected) > _MAX_EXCERPT_BYTES:
            if not selected:
                value = line.encode("utf-8")[:_MAX_EXCERPT_BYTES].decode("utf-8", "ignore")
                selected.append(value)
            truncated = True
            break
        selected.append(line)
        total += length + (len(selected) > 1)
    return {
        "start_line": start + 1,
        "end_line": start + len(selected),
        "text": "\n".join(selected),
        "truncated": truncated or start > 0 or start + len(selected) < len(lines),
    }


def _redacted_text(text):
    # Redact the full bounded text before cropping so secrets spanning a
    # potential excerpt boundary cannot survive as fragments.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    clean, categories = redact(text)
    # Redaction can replace multiline values (including PEM blocks). Preserve
    # source line numbers by excluding excerpts when exact offsets cannot hold.
    if len(clean.splitlines()) != len(text.splitlines()):
        return clean, (*categories, "LINE_MAPPING_UNAVAILABLE")
    return clean, categories


def _file_source(
    entry, text, content_digest, snapshot_digest, reason, terms, *, rank, score=0, manifest=None
):
    path, kind = entry["path"], entry["kind"]
    clean, redactions = _redacted_text(text)
    capture = {
        "path": path,
        "category": kind,
        "sha256": content_digest,
        "snapshot_digest": snapshot_digest,
        "selector_policy_version": _POLICY_VERSION,
        "selector_rank": rank,
    }
    if manifest is not None:
        capture["manifest_at_import"] = {
            "content_sha256": entry["worktree_sha256"],
            **{
                field: manifest[field]
                for field in ("ecosystem", "name", "dependencies", "scripts", "members", "parsed")
            },
        }
    if reason == "IMPORT_LEXICAL":
        capture["selector_signal"] = {"authority": "DERIVED", "score": score}
    elif reason == "IMPORT_RELATED_TEST":
        capture["selector_signal"] = {"authority": "DERIVED", "method": "SHARED_NAME_TOKEN"}
    if "LINE_MAPPING_UNAVAILABLE" in redactions:
        capture["content_exclusion"] = "UNMAPPABLE_REDACTION"
    else:
        capture["excerpt"] = _excerpt(clean, terms)
    source = _source(_SOURCE_KINDS[kind], "OPERATIONAL", path, content_digest, reason, capture)
    if source is None:
        return None
    return replace(source, redaction=tuple(sorted({*source.redaction, *redactions})))


def _select_files(root, observation, snapshot_digest, terms):
    from .discovery import read_indexed_text, tracked_paths

    try:
        tracked = tracked_paths(root)
    except IntegrityError:
        # If the index cannot prove tracking, retain only the repository summary.
        return [], "TRACKED_FILES_UNAVAILABLE"
    entries = [
        entry
        for entry in observation["files"]
        if entry["kind"] in _SOURCE_KINDS
        and entry["index_mode"] in {"100644", "100755"}
        and entry["path"] in tracked
        and not secret_path(entry["path"])
        and sanitize_text(entry["path"]) == entry["path"]
    ]
    entries.sort(key=lambda entry: _path_key(entry["path"]))
    observed = {}

    def read(entry):
        path = entry["path"]
        if path not in observed:
            observed[path] = read_indexed_text(root, path, max_bytes=_MAX_FILE_BYTES)
        return observed[path]

    exact_manifests = {row["path"]: row for row in observation["manifests"]}

    def source(entry, reason, rank, *, score=0):
        value = read(entry)
        return (
            _file_source(
                entry,
                *value,
                snapshot_digest,
                reason,
                terms,
                rank=rank,
                score=score,
                manifest=exact_manifests.get(entry["path"]),
            )
            if value is not None
            else None
        )

    manifests = [entry for entry in entries if entry["kind"] == "MANIFEST"]
    manifests.sort(key=lambda entry: (entry["path"].count("/"), _path_key(entry["path"])))
    documents = [
        entry
        for entry in entries
        if entry["kind"] == "DOCUMENTATION" and _important_document(entry["path"]) is not None
    ]
    documents.sort(key=lambda entry: _important_document(entry["path"]))
    chosen = [source(entry, "IMPORT_MANIFEST", rank) for rank, entry in enumerate(manifests[:3])]
    chosen.extend(
        source(entry, "IMPORT_DOCUMENT", rank) for rank, entry in enumerate(documents[:3])
    )

    lexical = [entry for entry in entries if entry["kind"] == "SOURCE"]
    lexical.sort(
        key=lambda entry: (-len(_path_tokens(entry["path"]) & terms), _path_key(entry["path"]))
    )
    matches = []
    for entry in lexical[:_TEXT_SCAN_LIMIT] if terms else ():
        value = read(entry)
        if value is None:
            continue
        text, _ = value
        path_score = len(_path_tokens(entry["path"]) & terms)
        content_score = len(_tokens(_redacted_text(text)[0]) & terms)
        score = 2 * path_score + content_score
        if score:
            matches.append((score, entry))
    matches.sort(key=lambda row: (-row[0], _path_key(row[1]["path"])))
    selected = matches[:4]
    chosen.extend(
        source(entry, "IMPORT_LEXICAL", rank, score=score)
        for rank, (score, entry) in enumerate(selected)
    )

    source_terms = {
        token for _, entry in selected for token in _tokens(PurePosixPath(entry["path"]).stem)
    }
    related_tests = [
        entry
        for entry in entries
        if entry["kind"] == "TEST" and _tokens(PurePosixPath(entry["path"]).stem) & source_terms
    ]
    chosen.extend(
        source(entry, "IMPORT_RELATED_TEST", rank) for rank, entry in enumerate(related_tests[:3])
    )
    selected_sources = [record for record in chosen if record is not None]
    if selected_sources:
        try:
            still_tracked = tracked_paths(root)
        except IntegrityError:
            return [], "TRACKED_FILES_UNAVAILABLE"
        if any(record.identity not in still_tracked for record in selected_sources):
            return [], "TRACKED_FILES_CHANGED"
    return selected_sources, None


def capture_for_work(repository, work_item_id, target_provider, required_handoff_id):
    """Read import and work identity before bounded Git and file reads."""
    from .storage import pointer, read_snapshot

    root = Path(repository.root).resolve()
    with repository.read() as connection:
        if connection is None:
            return ImportCapture(None, None, ())
        work = WorkRepository(connection)
        item = work.item(work.row(work_item_id))
        if (
            required_handoff_id is not None
            and repository.switch_projection(
                connection, item.project_id, work_item_id, target_provider, required_handoff_id
            )
            is not None
        ):
            return ImportCapture(None, None, ())
        entry = pointer(connection, item.project_id)
    if entry is None:
        return ImportCapture(None, None, ())
    snapshot = read_snapshot(root, item.project_id, entry)
    snapshot_digest = snapshot["snapshot_digest"]
    observation = snapshot["observation"]
    terms = _work_terms("\n".join((item.objective, *item.acceptance_criteria)))
    files, exclusion = _select_files(root, observation, snapshot_digest, terms)
    summary = {
        "snapshot_digest": snapshot_digest,
        "head": observation["head"],
        "dirty_at_import": observation["dirty"],
        "tracked_count_at_import": observation["tracked_count"],
        "selector_policy_version": _POLICY_VERSION,
        "document_count_at_import": len(observation["docs"]),
        "test_count_at_import": len(observation["tests"]),
        "manifest_count_at_import": len(observation["manifests"]),
        "languages_at_import": observation["languages"],
    }
    if exclusion is not None:
        summary["content_exclusion"] = exclusion
    source = _source(
        "IMPORT_REPOSITORY",
        "OPERATIONAL",
        item.project_id,
        snapshot_digest,
        "IMPORT_REPOSITORY",
        summary,
    )
    return ImportCapture(snapshot_digest, item.updated_at, (source, *files))


def verify_capture_head(connection, project_id, work_item_id, captured):
    from .storage import current_snapshot_digest

    snapshot = current_snapshot_digest(connection, project_id)
    if snapshot != captured.snapshot_digest or (
        snapshot is not None
        and WorkRepository(connection).row(work_item_id)["updated_at"] != captured.work_updated_at
    ):
        raise ConflictError("Imported Context sources changed before publication. Retry selection.")
    return captured.sources
