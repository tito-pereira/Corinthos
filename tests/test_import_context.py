# SPDX-License-Identifier: MPL-2.0
"""Check imported Context uses bounded current evidence and immutable provenance."""

import json
import subprocess
from hashlib import sha256

import pytest

from corinthos.context import ContextService
from corinthos.contracts import ConflictError
from corinthos.external_import import context_sources
from corinthos.external_import.service import ExternalProjectImportService
from corinthos.work import WorkService


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def _write(root, name, text):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def _confirmations(observation):
    return sorted(row["code"] for row in observation["ambiguities"] if row["required"])


@pytest.fixture
def imported_repository(tmp_path, monkeypatch):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user-state")
    root = tmp_path / "external"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Synthetic import fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, "README.md", "# Ledger processing fixture\n")
    _write(
        root, "pyproject.toml", '[project]\nname = "ledger-fixture"\ndependencies = ["requests"]\n'
    )
    _write(root, "src/payment.py", "def payment():\n    return 'original_source_marker'\n")
    _write(root, "tests/test_payment.py", "# related_test_marker\n")
    _write(root, "src/calendar.py", "import another.py\n# unrelated_source_marker\n")
    _write(root, ".env", "PASSWORD=fixture_path_secret\n")
    _git(root, "add", "--all")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic external repository")
    importer = ExternalProjectImportService()
    assert not (root / ".corinthos").exists()
    inventory = importer.inspect(root)
    imported = importer.commit(
        root, inventory["discovery_digest"], confirmations=_confirmations(inventory)
    )
    assert imported["status"] == "IMPORTED"
    work = WorkService(root)
    task = work.create(
        scope_level="CHANGE",
        objective="Repair processing in the payment module.",
        acceptance_criteria=[
            "Payment transactions have correct behavior.",
            "Use src/payment.py and tests/test_payment.py.",
        ],
    )
    return root, work, task, importer, imported


def _source_by_path(projection):
    return {
        row["identity"]: row
        for row in projection.canonical_payload["sources"]
        if row["kind"].startswith("IMPORT_") and row["kind"] != "IMPORT_REPOSITORY"
    }


def test_imported_context_retains_current_exact_sources_with_precedence(imported_repository):
    root, work, item, importer, imported = imported_repository
    source_file = root / "src/payment.py"
    source_file.write_text(
        "# filler\n" * 60
        + "# payment transactions: current_source_marker\npassword = 'fixture_inline_secret'\n",
        encoding="utf-8",
        newline="\n",
    )
    _write(root, "src/payment_extra.py", "# payment_untracked_marker\n")
    context = ContextService(root)
    projection = context.create(item.work_item_id, "codex")
    assert context.create(item.work_item_id, "codex") == projection
    rows = projection.canonical_payload["sources"]
    assert [row["reason"] for row in rows if row["kind"].startswith("IMPORT_")] == [
        "IMPORT_REPOSITORY",
        "IMPORT_MANIFEST",
        "IMPORT_DOCUMENT",
        "IMPORT_LEXICAL",
        "IMPORT_RELATED_TEST",
    ]
    assert rows[0]["kind"] == "WORK_ITEM"
    assert all(row["authority"] == "OPERATIONAL" for row in rows)
    assert projection.project_id == item.project_id == imported["project_id"]
    by_path = _source_by_path(projection)
    assert set(by_path) == {
        "README.md",
        "pyproject.toml",
        "src/payment.py",
        "tests/test_payment.py",
    }
    selected = by_path["src/payment.py"]
    content_sha = sha256(source_file.read_bytes()).hexdigest()
    assert selected["revision"] == selected["capture"]["sha256"] == content_sha
    assert selected["capture"]["snapshot_digest"] == imported["snapshot_digest"]
    assert selected["capture"]["excerpt"]["start_line"] > 50
    assert "current_source_marker" in selected["capture"]["excerpt"]["text"]
    assert "SENSITIVE_ASSIGNMENT" in selected["redaction"]
    assert by_path["pyproject.toml"]["capture"]["manifest_at_import"]["name"] == "ledger-fixture"
    assert "related_test_marker" in by_path["tests/test_payment.py"]["capture"]["excerpt"]["text"]
    text = json.dumps(projection.canonical_payload)
    assert all(
        marker not in text
        for marker in (
            str(root),
            "original_source_marker",
            "fixture_path_secret",
            "fixture_inline_secret",
            "payment_untracked_marker",
            "unrelated_source_marker",
        )
    )
    assert importer.status(root)["snapshot_digest"] == imported["snapshot_digest"]
    assert context.verify(projection.projection_id) == projection
    assert work.get(item.work_item_id) == item


def test_removed_tracking_cannot_import_same_path_replacement(imported_repository):
    root, _, item, _, _ = imported_repository
    context = ContextService(root)
    before = context.create(item.work_item_id, "codex")
    assert "src/payment.py" in _source_by_path(before)
    _git(root, "rm", "--cached", "src/payment.py")
    _write(root, "src/payment.py", "# payment replaced_untracked_marker\n")
    after = context.create(item.work_item_id, "codex")
    assert "src/payment.py" not in _source_by_path(after)
    assert "tests/test_payment.py" not in _source_by_path(after)
    assert "replaced_untracked_marker" not in json.dumps(after.canonical_payload)
    assert context.verify(before.projection_id) == before


def test_tracking_removed_during_capture_discards_the_file_set(imported_repository, monkeypatch):
    from corinthos.external_import import discovery

    root, _, item, _, _ = imported_repository
    read = discovery.read_indexed_text

    def read_after_untracking(root, relative, **options):
        if relative == "src/payment.py":
            _git(root, "rm", "--cached", relative)
            _write(root, relative, "# payment replaced_untracked_marker\n")
        return read(root, relative, **options)

    monkeypatch.setattr(discovery, "read_indexed_text", read_after_untracking)
    projection = ContextService(root).create(item.work_item_id, "codex")
    assert _source_by_path(projection) == {}
    summary = next(
        row for row in projection.canonical_payload["sources"] if row["kind"] == "IMPORT_REPOSITORY"
    )
    assert summary["capture"]["content_exclusion"] == "TRACKED_FILES_CHANGED"


def test_context_rejects_work_change_during_repository_capture(imported_repository, monkeypatch):
    root, work, item, _, _ = imported_repository
    context = ContextService(root)
    select_files = context_sources._select_files

    def capture_then_edit(*args):
        result = select_files(*args)
        work.update(item.work_item_id, objective="Use edited payment selectors.")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(context_sources, "_select_files", capture_then_edit)
        with pytest.raises(ConflictError, match="Imported Context sources changed"):
            context.create(item.work_item_id, "codex")
    assert context.list(item.work_item_id) == []
    assert "src/payment.py" in _source_by_path(context.create(item.work_item_id, "codex"))


def test_new_tracked_context_file_requires_explicit_import_refresh(imported_repository):
    root, _, item, importer, imported = imported_repository
    _write(root, "src/payment_bonus.py", "# payment bonus validated_source_marker\n")
    _git(root, "add", "src/payment_bonus.py")
    context = ContextService(root)
    previous = context.create(item.work_item_id, "codex")
    assert "src/payment_bonus.py" not in _source_by_path(previous)
    observed = importer.inspect(root)
    refreshed = importer.refresh(
        root,
        imported["snapshot_digest"],
        observed["discovery_digest"],
        confirmations=_confirmations(observed),
    )
    assert refreshed["status"] == "IMPORTED"
    assert refreshed["snapshot_digest"] != imported["snapshot_digest"]
    current = context.create(item.work_item_id, "codex")
    selected = _source_by_path(current)["src/payment_bonus.py"]
    assert selected["capture"]["snapshot_digest"] == refreshed["snapshot_digest"]
    assert "validated_source_marker" in selected["capture"]["excerpt"]["text"]
    assert context.verify(previous.projection_id) == previous


def test_multiline_redaction_never_exposes_partial_private_key():
    content = "one\n-----BEGIN RSA PRIVATE KEY-----\nfixture_key_secret\n-----END RSA PRIVATE KEY-----\npayment\n"
    source = context_sources._file_source(
        {"path": "src/payment.py", "kind": "SOURCE"},
        content,
        sha256(content.encode()).hexdigest(),
        "a" * 64,
        "IMPORT_LEXICAL",
        {"payment"},
        rank=0,
        score=1,
    )
    assert source.capture["content_exclusion"] == "UNMAPPABLE_REDACTION"
    assert {"LINE_MAPPING_UNAVAILABLE", "PRIVATE_KEY"} <= set(source.redaction)
    assert "excerpt" not in source.capture
    assert "fixture_key_secret" not in json.dumps(source.capture)
