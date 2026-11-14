# SPDX-License-Identifier: MPL-2.0
"""Verify digest-bound private initialization and immutable import provenance."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from test_session_supervisor import SyntheticManager

from corinthos.bootstrap import initialize
from corinthos.cli import main
from corinthos.contracts import ContractStore, StoreError
from corinthos.external_import import service, storage
from corinthos.project_registry import ProjectRegistryService
from corinthos.project_state import ProjectStateService
from corinthos.sessions.supervisor import SessionSupervisor
from corinthos.work import WorkService
from corinthos.work.adaptive import AdaptiveWorkflowService


def _git(root, *arguments):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *arguments],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout


def _write(root, path, value):
    (root / path).write_text(value, encoding="utf-8", newline="\n")


def _files(root):
    return {
        file.relative_to(root).as_posix(): file.read_bytes()
        for file in root.rglob("*")
        if file.is_file() and ".corinthos" not in file.relative_to(root).parts
    }


def _confirm(observation):
    return [row["code"] for row in observation["ambiguities"] if row["required"]]


def _events(root):
    with ContractStore.read_snapshot(root) as (connection, version):
        assert version == 9
        assert connection.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0
        rows = connection.execute(
            "SELECT project_id, kind FROM lc_events WHERE kind IN (?, ?) ORDER BY sequence",
            storage.IMPORT_EVENTS,
        ).fetchall()
        return [tuple(row) for row in rows]


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    root = tmp_path / "external"
    root.mkdir()
    _git(root, "init")
    _write(root, "README.md", "# Inventory")
    _write(root, "AGENTS.md", "Authoritative repository instructions")
    _write(root, "pyproject.toml", '[project]\nname = "inventory"\n')
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Create synthetic repository",
    )
    return root


def test_inspect_reject_and_commit_preserve_tracked_files_git_and_contract_authority(repository):
    importer = service.ExternalProjectImportService()
    baseline = _files(repository)
    original = importer.inspect(repository)
    assert importer.inspect(repository) == original
    assert importer.status(repository)["status"] == "NOT_IMPORTED"
    assert _files(repository) == baseline
    assert not (repository / ".corinthos").exists()
    assert ProjectRegistryService().paths() == []

    _write(repository, "README.md", "# A dirty checkout")
    modified = _files(repository)
    assert (
        importer.commit(repository, original["discovery_digest"], _confirm(original))["error"]
        == "REPOSITORY_CHANGED"
    )
    preview = importer.inspect(repository)
    assert "DIRTY_TRACKED" in _confirm(preview)
    assert (
        importer.commit(repository, preview["discovery_digest"])["error"] == "CONFIRMATION_REQUIRED"
    )
    assert _files(repository) == modified
    assert not (repository / ".corinthos").exists()
    assert ProjectRegistryService().paths() == []

    result = importer.commit(repository, preview["discovery_digest"], _confirm(preview))
    assert result["status"] == "IMPORTED"
    assert result == importer.status(repository)
    assert result == importer.commit(repository, preview["discovery_digest"], _confirm(preview))
    identity = result["project_id"]
    summary = ProjectStateService().summary(repository)
    assert summary.project_id == identity and summary.active_project is None
    assert summary.external_import["snapshot_digest"] == result["snapshot_digest"]
    for scope in ("TASK", "CHANGE"):
        item = WorkService(repository).create(
            scope_level=scope, objective="Inspect inventory", acceptance_criteria=["Inspect README"]
        )
        assert item.project_id == identity
    assert _events(repository) == [(identity, "EXTERNAL_PROJECT_IMPORTED")]
    assert ProjectRegistryService().paths() == [str(repository)]
    assert _files(repository) == modified


def test_atomic_claim_preserves_preexisting_invalid_or_managed_state(repository, monkeypatch):
    importer = service.ExternalProjectImportService()
    preview = importer.inspect(repository)
    actual_promote = service._promote

    def collide(source, target):
        target.mkdir()
        actual_promote(source, target)

    monkeypatch.setattr(service, "_promote", collide)
    importer.commit(repository, preview["discovery_digest"], _confirm(preview))
    invalid = repository / ".corinthos"
    assert list(invalid.iterdir()) == []
    assert importer.status(repository)["status"] == "INVALID"
    assert (
        importer.commit(repository, preview["discovery_digest"], _confirm(preview))["status"]
        == "INVALID"
    )
    assert list(invalid.iterdir()) == []
    assert ProjectRegistryService().paths() == []
    invalid.rmdir()
    initialize(repository)
    known = _files(repository)
    assert (
        importer.commit(repository, preview["discovery_digest"], _confirm(preview))["status"]
        == "ALREADY_MANAGED"
    )
    assert _files(repository) == known


def test_exact_retry_reconciles_private_failure_without_duplicate_identity(repository, monkeypatch):
    importer = service.ExternalProjectImportService()
    preview = importer.inspect(repository)
    args = (repository, preview["discovery_digest"], _confirm(preview))

    def unavailable(*args):
        raise OSError("synthetic persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "save_snapshot", unavailable)
        with pytest.raises(StoreError):
            importer.commit(*args)
    incomplete = importer.status(repository)
    assert incomplete["status"] == "INCOMPLETE"
    assert _events(repository) == []
    assert ProjectRegistryService().paths() == []
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: importer.commit(*args), range(2)))
    assert all(result["status"] == "IMPORTED" for result in results)
    assert {result["snapshot_digest"] for result in results} == {
        importer.status(repository)["snapshot_digest"]
    }
    assert _events(repository) == [(incomplete["project_id"], "EXTERNAL_PROJECT_IMPORTED")]


def test_registry_failure_is_recoverable_and_never_creates_files_inside_source(
    repository, monkeypatch
):
    importer = service.ExternalProjectImportService()
    preview = importer.inspect(repository)
    args = (repository, preview["discovery_digest"], _confirm(preview))
    with monkeypatch.context() as patch:
        patch.setattr("corinthos.user_state.state_root", lambda: repository / "user")
        outcome = importer.commit(*args)
        assert outcome["status"] == "IMPORTED"
        assert outcome["error"] == "REGISTRY_UNAVAILABLE"
        assert not (repository / "user").exists()
    assert ProjectRegistryService().paths() == []
    assert importer.status(repository)["snapshot_digest"] == outcome["snapshot_digest"]
    result = importer.commit(*args)
    assert result["snapshot_digest"] == outcome["snapshot_digest"]
    assert result["error"] is None
    assert ProjectRegistryService().paths() == [str(repository)]


def test_change_before_writer_reservation_cannot_publish_stale_snapshot(repository, monkeypatch):
    importer = service.ExternalProjectImportService()
    preview = importer.inspect(repository)
    actual = service.LifecycleRepository.transaction
    with monkeypatch.context() as patch:

        @contextmanager
        def changed(lifecycle):
            _write(repository, "README.md", "# Edited before the SQLite write reservation")
            with actual(lifecycle) as connection:
                yield connection

        patch.setattr(service.LifecycleRepository, "transaction", changed)
        result = importer.commit(repository, preview["discovery_digest"], _confirm(preview))

    assert result["status"] == "INCOMPLETE" and result["error"] == "REPOSITORY_CHANGED"
    assert _events(repository) == []
    assert ProjectRegistryService().paths() == []


def test_imported_task_uses_reduced_flow_and_leaves_verification_independent(repository):
    importer = service.ExternalProjectImportService()
    _write(repository, "payment.py", "PAYMENT = 'imported_task_source_marker'\n")
    _git(repository, "add", "payment.py")
    preview = importer.inspect(repository)
    result = importer.commit(repository, preview["discovery_digest"], _confirm(preview))
    work = WorkService(repository)
    item = work.create(
        scope_level="TASK",
        objective="Inspect payment",
        acceptance_criteria=["Read payment.py"],
    )
    assert item.project_id == result["project_id"]
    manager = SyntheticManager()
    supervisor = SessionSupervisor(manager_factory=lambda *args, **kwargs: manager)
    flow = AdaptiveWorkflowService(repository, supervisor)
    try:
        started = flow.start(item.work_item_id, "codex", read_only=True)
        session = supervisor.wait(repository, started["latest_session"]["session_id"])
        assert session.state == "SUCCEEDED" and not session.cleanup_unconfirmed
        assert session.work_item_id == item.work_item_id
        assert "imported_task_source_marker" in manager.prompts[0]
        report = flow.report(item.work_item_id)
        assert report["overall_status"] == "NOT_VERIFIED"
        assert work.get(item.work_item_id) == item
        assert _events(repository) == [(item.project_id, "EXTERNAL_PROJECT_IMPORTED")]
    finally:
        assert supervisor.close()


def test_cli_import_refresh_preserves_previous_inventory_and_checks_integrity(repository, capsys):
    importer = service.ExternalProjectImportService()
    root = str(repository)

    def run(*args, expected_code=0):
        assert main(["import", *args]) == expected_code
        output = capsys.readouterr()
        assert output.err == ""
        return json.loads(output.out)

    preview = run("inspect", root)
    first = run(
        "commit",
        root,
        "--digest",
        preview["discovery_digest"],
        *(part for code in _confirm(preview) for part in ("--confirm", code)),
    )
    original = first["snapshot_digest"]
    old, files = importer.files(repository, original)
    snapshot = repository / ".corinthos" / "snapshots" / f"external-import-{original}.json"
    original_bytes = snapshot.read_bytes()
    _write(repository, "main.py", "print('synthetic marker')\n")
    _git(repository, "add", "main.py")
    latest = run("inspect", root)
    args = [
        "refresh",
        "--root",
        root,
        "--expected-snapshot-digest",
        original,
        "--digest",
        latest["discovery_digest"],
        *(part for code in _confirm(latest) for part in ("--confirm", code)),
    ]
    refreshed = run(*args)
    assert refreshed["snapshot_digest"] != original
    assert refreshed["project_id"] == first["project_id"]
    assert run(*args, expected_code=1)["error"] == "SNAPSHOT_CHANGED"
    assert importer.files(repository, original) == (old, files)
    assert snapshot.read_bytes() == original_bytes
    listing = run("files", "--root", root)
    assert "main.py" in {row["path"] for row in listing["files"]}
    assert run("status", "--root", root) == refreshed

    current = snapshot.with_name(f"external-import-{refreshed['snapshot_digest']}.json")
    current.write_bytes(current.read_bytes() + b" ")
    assert run("status", "--root", root, expected_code=1)["status"] == "INVALID"
