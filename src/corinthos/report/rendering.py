# SPDX-License-Identifier: MPL-2.0
"""Safe report prose, frozen Markdown views and recoverable local exports."""

import html
import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path

from corinthos._paths import windows_component_is_reserved
from corinthos.contracts import ContractRef, IntegrityError, validate_schema
from corinthos.contracts.artifacts import is_link
from corinthos.contracts.model import canonical_json
from corinthos.project_manager.presentation import safe_text

_LIMIT = 4 * 1024 * 1024
_TOKEN = re.compile(
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?i:Bearer\s+[A-Za-z0-9._~+/-]{12,})"
)
_ASSIGNMENT = re.compile(
    r"""(?ix)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|token)
    \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
_PRIVATE_URL = re.compile(r"https?://(?:app\.|www\.)?notion\.(?:so|com)/[^\s<>'\"]+", re.I)
_PRIVATE_PATH = re.compile(
    r"""(?ix)(?<![A-Za-z0-9])(?:[A-Z]:[\\/][^\r\n<>"'|,;]*|
    /(?:Users|home|tmp|private|var|root)/[^\s<>"'|,;]*)"""
)
_MARKUP = re.compile(r"([\\\x60*_{}\[\]()#+.!|>~-])")
_SHA = re.compile(r"[0-9a-f]{64}")
_SECTIONS = (
    ("report_scope", "Scope and cutoff"),
    ("source_refs", "Exact source references"),
    ("version_and_delivery_lineage", "Product identity and independent states"),
    ("execution_and_validation_digest", "Execution and Validation"),
    ("engineering_quality_and_documentation", "Five Engineering Commandments"),
    ("decisions_and_change_history", "Decisions and change history"),
    ("artifact_and_evidence_index", "Artifact and evidence index"),
    ("feedback_and_acceptance", "Feedback and human acceptance"),
    ("risks_metrics_and_next_actions", "Risks, metrics and next actions"),
)


def _redact(value):
    value = _TOKEN.sub("[credential redacted]", value)
    value = _ASSIGNMENT.sub("[sensitive assignment redacted]", value)
    value = _PRIVATE_URL.sub("[private planning reference redacted]", value)
    return _PRIVATE_PATH.sub("[private path redacted]", value)


def safe_prose(value: object) -> str:
    """Project bounded source prose without known credentials or private paths."""
    text = _redact(str(value))
    if len(text) > 4096:
        text = text[:4096] + " [source prose truncated]"
    return safe_text(text)


def _markdown(value):
    return _MARKUP.sub(r"\\\1", html.escape(safe_prose(value), quote=True))


def _fields(value, indent=0):
    prefix = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [prefix + "- None recorded."]
        lines = []
        for key, item in value.items():
            label = _markdown(key.replace("_", " ").capitalize())
            if isinstance(item, (dict, list)):
                lines.append(prefix + "- **" + label + "**")
                lines.extend(_fields(item, indent + 1))
            else:
                lines.append(prefix + "- **" + label + "** — " + _markdown(item))
        return lines
    if isinstance(value, list):
        if not value:
            return [prefix + "- None recorded."]
        lines = []
        for index, item in enumerate(value, 1):
            if isinstance(item, (dict, list)):
                lines.append(prefix + "- **Record " + str(index) + "**")
                lines.extend(_fields(item, indent + 1))
            else:
                lines.append(prefix + "- " + _markdown(item))
        return lines
    return [prefix + "- " + _markdown(value)]


def _report(document):
    validate_schema(document)
    if document["metadata"]["contract_type"] != "EVIDENCE_REPORT":
        raise IntegrityError("Use an exact Evidence and Report Contract.")
    data = canonical_json(document).encode("utf-8")
    if len(data) > _LIMIT:
        raise IntegrityError("The report exceeds the 4 MiB presentation limit.")
    return data


def render_json(document: dict) -> str:
    """Return exact canonical ERC JSON, refusing recognized sensitive content."""
    encoded = _report(document)
    if len(encoded) + 1 > _LIMIT:
        raise IntegrityError("The JSON report exceeds the 4 MiB presentation limit.")
    data = encoded.decode("utf-8")
    if _redact(data) != data:
        raise IntegrityError("Canonical report prose contains sensitive content.")
    return data + "\n"


def render_markdown(document: dict, manifest: dict | None = None) -> str:
    """Render only frozen report facts, with explicit presentation sanitization."""
    _report(document)
    metadata, payload = document["metadata"], document["payload"]
    scope, lineage = payload["report_scope"], payload["version_and_delivery_lineage"]
    lines = [
        "# " + _markdown(scope["kind"].title()) + " report",
        "",
        "**Technical** "
        + _markdown(lineage["technical_validation"])
        + " · **Delivery** "
        + _markdown(lineage["delivery"])
        + " · **Availability** "
        + str(lineage["availability"]).lower()
        + " · **Human acceptance** "
        + _markdown(lineage["acceptance"]),
        "",
        _markdown(payload["execution_and_validation_digest"]["summary"]),
        "",
        "Report "
        + _markdown(metadata["contract_id"])
        + " at revision "
        + _markdown(metadata["revision_id"])
        + ".",
        "",
        "Project "
        + _markdown(metadata["project_id"])
        + ", candidate "
        + _markdown(scope["target_id"])
        + ", as of "
        + _markdown(scope["as_of"])
        + ".",
        "",
        "This human view sanitizes source prose and displays recorded snapshot facts. "
        "The canonical JSON retains the exact ERC. Evidence hashes describe the source "
        "artifacts, while the export manifest identifies this presentation.",
        "",
    ]
    for key, label in _SECTIONS:
        lines.extend(("## " + label, ""))
        lines.extend(_fields(payload[key]))
        lines.append("")
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > _LIMIT:
        raise IntegrityError("The rendered report exceeds the 4 MiB presentation limit.")
    return result


def _safe_directory(path):
    path = Path(path)
    if ".." in path.parts:
        raise IntegrityError("Export paths cannot contain parent traversal.")
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if (
            part in {".git", ".nukytera", ".agentforge"}
            or windows_component_is_reserved(part)
            or part.endswith((".", " "))
            or ":" in part
        ):
            raise IntegrityError("The export path contains an unsafe component.")
        current = current / part
        info = current.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise IntegrityError("Export directories cannot follow links or redirected paths.")
    return absolute


def _same_directory(path, expected):
    observed = path.lstat()
    if is_link(observed) or not os.path.samestat(expected, observed):
        raise IntegrityError("The export directory changed during the operation.")


def _release_empty_reservation(directory, identity):
    try:
        if _safe_directory(directory) != directory or directory.resolve(strict=True) != directory:
            return
        _same_directory(directory, identity)
        directory.rmdir()
    except (OSError, IntegrityError):
        pass


def _read_file(path):
    info = path.lstat()
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise IntegrityError("Export files must be ordinary unlinked files.")
    if info.st_size > _LIMIT:
        raise IntegrityError("An export file exceeds the 4 MiB limit.")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise IntegrityError("An export file changed while opening it.")
        data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT or os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise IntegrityError("An export file changed while reading it.")
    return data


def _write_new(path, data):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if path.exists() and os.path.samestat(owned, path.lstat()):
            path.unlink()
        raise


def _index(directory):
    complete = directory / "export-manifest.json"
    pending = directory / ".export-pending.json"
    path = complete if os.path.lexists(complete) else pending
    try:
        value = json.loads(_read_file(path).decode("utf-8"))
    except (ValueError, UnicodeError, OSError):
        raise IntegrityError("The export manifest cannot be read as intact JSON.") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "report_ref", "report_sha256", "generation_sha256", "outputs"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["report_sha256"], str)
        or not _SHA.fullmatch(value["report_sha256"])
        or value["generation_sha256"] is not None
        and (
            not isinstance(value["generation_sha256"], str)
            or not _SHA.fullmatch(value["generation_sha256"])
        )
    ):
        raise IntegrityError("The export manifest is invalid.")
    reference = ContractRef.from_json(value["report_ref"])
    if reference.contract_type != "EVIDENCE_REPORT":
        raise IntegrityError("The export does not identify a Report Contract.")
    entries = value["outputs"]
    if not isinstance(entries, list) or not entries or len(entries) > 2:
        raise IntegrityError("The export manifest has invalid output entries.")
    names = set()
    for item in entries:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "size", "representation"}
            or not isinstance(item["path"], str)
            or item["path"] not in {"report.json", "report.md"}
            or item["path"] in names
            or not isinstance(item["sha256"], str)
            or not _SHA.fullmatch(item["sha256"])
            or type(item["size"]) is not int
            or not 0 < item["size"] <= _LIMIT
            or item["representation"]
            != ("canonical ERC JSON" if item["path"] == "report.json" else "sanitized Markdown")
        ):
            raise IntegrityError("The export manifest has an invalid output identity.")
        names.add(item["path"])
    return value, path == complete


def _outputs(document, manifest, format):
    canonical = render_json(document).encode("utf-8")[:-1]
    if format not in {"both", "json", "markdown"}:
        raise IntegrityError("Choose json, markdown or both report representations.")
    outputs = {}
    if format in {"both", "json"}:
        outputs["report.json"] = canonical + b"\n"
    if format in {"both", "markdown"}:
        outputs["report.md"] = render_markdown(document, manifest).encode("utf-8")
    return canonical, outputs


def verify_export(destination, *, document=None, manifest=None) -> dict:
    """Observe bundle completeness and bytes without changing recorded report facts."""
    directory = _safe_directory(destination)
    index, complete = _index(directory)
    observations = []
    for item in index["outputs"]:
        path = directory / item["path"]
        if not os.path.lexists(path):
            state = "MISSING"
        else:
            data = _read_file(path)
            state = (
                "INTACT"
                if len(data) == item["size"] and sha256(data).hexdigest() == item["sha256"]
                else "CHANGED"
            )
        observations.append({"path": item["path"], "state": state})
    source_verified = False
    if document is not None:
        names = {item["path"] for item in index["outputs"]}
        format = "both" if len(names) == 2 else ("json" if "report.json" in names else "markdown")
        canonical, expected_outputs = _outputs(document, manifest, format)
        if (
            index["report_ref"] != ContractRef.from_document(document).to_json()
            or index["report_sha256"] != sha256(canonical).hexdigest()
            or index["generation_sha256"]
            != (
                None
                if manifest is None
                else sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
            )
        ):
            raise IntegrityError("The export manifest differs from its exact source report.")
        for item in index["outputs"]:
            expected = expected_outputs[item["path"]]
            if item["size"] != len(expected) or item["sha256"] != sha256(expected).hexdigest():
                raise IntegrityError("The exported presentation differs from its frozen source.")
        source_verified = True
    intact = complete and all(item["state"] == "INTACT" for item in observations)
    return {
        "state": "COMPLETE" if intact else "INCOMPLETE",
        "report_ref": index["report_ref"],
        "report_sha256": index["report_sha256"],
        "generation_sha256": index["generation_sha256"],
        "outputs": observations,
        "destination": str(directory),
        "source_verified": source_verified,
    }


def export_report(document, destination, *, manifest=None, format="both") -> dict:
    """Export or recover one exact bundle, preserving every pre-existing output."""
    canonical, outputs = _outputs(document, manifest, format)
    expected = {
        "version": 1,
        "report_ref": ContractRef.from_document(document).to_json(),
        "report_sha256": sha256(canonical).hexdigest(),
        "generation_sha256": (
            None
            if manifest is None
            else sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
        ),
        "outputs": [
            {
                "path": name,
                "sha256": sha256(data).hexdigest(),
                "size": len(data),
                "representation": (
                    "canonical ERC JSON" if name.endswith(".json") else "sanitized Markdown"
                ),
            }
            for name, data in outputs.items()
        ],
    }
    destination = Path(destination)
    if ".." in destination.parts:
        raise IntegrityError("Export paths cannot contain parent traversal.")
    parent = _safe_directory(destination.parent)
    directory = parent / destination.name
    if (
        not destination.name
        or destination.name in {".", "..", ".git", ".nukytera", ".agentforge"}
        or windows_component_is_reserved(destination.name)
        or destination.name.endswith((".", " "))
        or ":" in destination.name
    ):
        raise IntegrityError("Choose a safe new export directory name.")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        directory = _safe_directory(directory)
        identity = directory.lstat()
        existing, complete = _index(directory)
        if existing != expected:
            raise IntegrityError(
                "The existing export belongs to different frozen content."
            ) from None
        if complete:
            observed = verify_export(directory, document=document, manifest=manifest)
            if observed["state"] != "COMPLETE":
                raise IntegrityError("A completed export no longer matches its recorded bytes.")
            return observed
    else:
        identity = directory.lstat()
        try:
            _write_new(directory / ".export-pending.json", canonical_json(expected).encode("utf-8"))
        except BaseException:
            _release_empty_reservation(directory, identity)
            raise
    for name, data in outputs.items():
        _same_directory(directory, identity)
        path = directory / name
        if os.path.lexists(path):
            if _read_file(path) != data:
                raise IntegrityError("An existing export file differs from the frozen output.")
        else:
            _write_new(path, data)
    _same_directory(directory, identity)
    for name, data in outputs.items():
        if _read_file(directory / name) != data:
            raise IntegrityError("Export bytes changed before completion.")
    _write_new(directory / "export-manifest.json", canonical_json(expected).encode("utf-8"))
    return verify_export(directory, document=document, manifest=manifest)
