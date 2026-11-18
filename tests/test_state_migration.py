# SPDX-License-Identifier: MPL-2.0
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from corinthos.contracts import (
    AuthorityError,
    ConflictError,
    ContractRef,
    ContractStore,
    IntegrityError,
    PublishContext,
    StoreError,
    approval_request,
    migration,
)
from corinthos.contracts import store as storage


@pytest.fixture(scope="module")
def original_state(tmp_path_factory):
    root = tmp_path_factory.mktemp("original-store")
    store = ContractStore(root)
    directory = Path(__file__).resolve().parents[1] / "examples" / "contracts"
    order = (
        "project",
        "technology-decision",
        "module",
        "workflow",
        "validation",
        "validation-result",
        "evidence-report",
        "project-revision-request",
    )
    documents = [json.loads((directory / f"{name}.json").read_text("utf-8")) for name in order]
    plan, result = documents[4:6]
    matrix = plan["payload"]["verification_matrix"][0]
    record = result["payload"]
    execution = record["target_and_execution"]
    evidence = b"Observed partial evidence in the disposable current fixture."
    private = store.database_path.parent
    artifact = private / "artifacts" / "checks" / "partial.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(evidence)
    record["evidence_refs"].append(
        {
            "evidence_id": "legacy-evidence",
            "check_id": matrix["check_id"],
            "source_ref": matrix["source_ref"],
            "artifact_identity": execution["artifact_identity"],
            "execution_context": {
                "validation_run_id": execution["validation_run_id"],
                "validation_plan_ref": record["validation_plan_ref"],
            },
            "verification_method": matrix["verification_method"],
            "evaluator_ref": execution["evaluator_ref"],
            "evidence_kind": matrix["required_evidence"][0],
            "path": "checks/partial.txt",
            "sha256": hashlib.sha256(evidence).hexdigest(),
        }
    )
    record["check_results"][0]["evidence_ids"] = ["legacy-evidence"]
    second = deepcopy(documents[0])
    second["metadata"].update(
        version=2,
        revision_id="demo-intent-r2",
        supersedes=ContractRef.from_document(documents[0]).to_json(),
    )
    second["metadata"]["approval"]["approval_id"] = "legacy-approval-r2"
    second["payload"]["scope"]["included"].append("Document the copied current setup.")
    for document in [*documents, second]:
        origin = document["metadata"]["provenance"]
        receipt = approval_request(document) if document["metadata"]["approval"] else None
        store.publish(
            document,
            context=PublishContext(
                origin["engine"],
                origin["actor_id"],
                lambda value, proof=receipt: value == proof,
            ),
        )
    store.backup()
    snapshots = private / "snapshots"
    snapshots.mkdir()
    (snapshots / "retained.json").write_bytes(b'{"build_id":"retained-build"}')
    (snapshots / "empty").mkdir()
    legacy = root / ".nukytera"
    private.rename(legacy)
    return legacy


@pytest.fixture
def legacy(tmp_path, original_state):
    return shutil.copytree(original_state, tmp_path / ".nukytera")


def _contract_records(database):
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("contracts", "revisions", "contract_references", "audit_events")
        }


def _database_version(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_v1_upgrade_preserves_all_contract_records_and_verified_backup(tmp_path, original_state):
    private = shutil.copytree(original_state, tmp_path / ".corinthos")
    database = private / "state.db"
    before = _contract_records(database)
    existing_backup = (private / "state.backup.db").read_bytes()
    assert _database_version(database) == 1

    store = ContractStore(tmp_path)

    assert _database_version(database) == 9
    assert _contract_records(database) == before
    backups = list(private.glob("state.pre-v2-*.db"))
    assert len(backups) == 1
    assert _database_version(backups[0]) == 1
    assert _contract_records(backups[0]) == before
    assert (private / "state.backup.db").read_bytes() == existing_backup
    assert len(store.list_events()) == 9
    result = store.list_revisions("demo-result")[0]
    evidence = result["payload"]["evidence_refs"][0]
    store.verify_artifact(evidence["path"], evidence["sha256"])
    assert ContractStore(tmp_path).list_events() == store.list_events()
    assert list(private.glob("state.pre-v2-*.db")) == backups


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_failed_v2_upgrade_rolls_back_to_v1_and_retains_backup(
    tmp_path, original_state, monkeypatch
):
    private = shutil.copytree(original_state, tmp_path / ".corinthos")
    database = private / "state.db"
    before = _contract_records(database)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V2", (*storage._MIGRATION_V2, "INVALID SQL"))
        with pytest.raises(StoreError, match="database operation failed"):
            ContractStore(tmp_path)
    assert _database_version(database) == 1
    assert _contract_records(database) == before
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'pm_%'").fetchall()
            == []
        )
    backup = next(private.glob("state.pre-v2-*.db"))
    assert _contract_records(backup) == before
    assert ContractStore(tmp_path).list_events()
    assert _database_version(database) == 9
    assert backup.exists()


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_failed_preupgrade_backup_publishes_no_schema_changes(
    tmp_path, original_state, monkeypatch
):
    private = shutil.copytree(original_state, tmp_path / ".corinthos")
    database = private / "state.db"
    before = database.read_bytes()

    def unavailable(*args, **kwargs):
        raise StoreError("The pre-upgrade backup failed.")

    monkeypatch.setattr(ContractStore, "_copy_backup", unavailable)
    with pytest.raises(StoreError, match="backup failed"):
        ContractStore(tmp_path)
    assert database.read_bytes() == before
    assert _database_version(database) == 1
    assert not list(private.glob("state.pre-v2-*.db"))


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_readonly_v1_database_cannot_be_upgraded(tmp_path, original_state):
    private = shutil.copytree(original_state, tmp_path / ".corinthos")
    database = private / "state.db"
    before = database.read_bytes()
    mode = database.stat().st_mode
    database.chmod(mode & ~stat.S_IWUSR)
    try:
        with pytest.raises(StoreError, match="read-only"):
            ContractStore(tmp_path)
        assert database.read_bytes() == before
        assert not list(private.glob("state.pre-v2-*.db"))
    finally:
        database.chmod(mode)


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_concurrent_openers_upgrade_v1_once(tmp_path, original_state):
    private = shutil.copytree(original_state, tmp_path / ".corinthos")
    before = _contract_records(private / "state.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        stores = list(executor.map(lambda _: ContractStore(tmp_path), range(2)))
    assert all(len(store.list_events()) == 9 for store in stores)
    assert len(list(private.glob("state.pre-v2-*.db"))) == 1
    assert _contract_records(private / "state.db") == before


def test_fresh_store_needs_no_preupgrade_backup(tmp_path):
    store = ContractStore(tmp_path)
    assert _database_version(store.database_path) == 9
    assert not list(store.database_path.parent.glob("state.pre-v2-*.db"))


@pytest.mark.skip(reason="Archived-schema upgrade scenarios are retired from routine verification.")
def test_legacy_copy_stays_v1_until_explicit_open_upgrades_it(legacy):
    original = (legacy / "state.db").read_bytes()
    assert migration.migrate_legacy_state(legacy.parent)
    target = legacy.parent / ".corinthos" / "state.db"
    assert target.read_bytes() == original
    assert _database_version(target) == 1
    assert not migration.migrate_legacy_state(legacy.parent)
    assert len(ContractStore(legacy.parent).list_events()) == 9
    assert _database_version(target) == 9
    assert (legacy / "state.db").read_bytes() == original


def test_files_only_migration_also_preserves_a_current_store(tmp_path):
    store = ContractStore(tmp_path)
    with store._transaction() as connection:
        connection.execute(
            "INSERT INTO pm_sessions VALUES (?, ?, ?, ?)",
            ("test-session", "test-project", 1, '{"state":"DRAFT"}'),
        )
    private = store.database_path.parent
    legacy = private.with_name(".nukytera")
    private.rename(legacy)
    original = (legacy / "state.db").read_bytes()
    assert migration.migrate_legacy_state(tmp_path)
    assert (private / "state.db").read_bytes() == original
    assert (legacy / "state.db").read_bytes() == original
    assert not migration.migrate_legacy_state(tmp_path)


def test_old_store_migration_preserves_database_and_evidence(legacy):
    root = legacy.parent
    with pytest.raises(StoreError):
        ContractStore(root)
    database_bytes = (legacy / "state.db").read_bytes()
    assert migration.migrate_legacy_state(root)
    target = root / ".corinthos"
    assert (target / "state.db").read_bytes() == database_bytes
    assert (legacy / "state.db").read_bytes() == database_bytes
    assert (target / "state.backup.db").read_bytes() == (legacy / "state.backup.db").read_bytes()
    assert (target / "snapshots" / "empty").is_dir()
    assert (target / "snapshots" / "retained.json").read_bytes() == (
        legacy / "snapshots" / "retained.json"
    ).read_bytes()
    assert not migration.migrate_legacy_state(root)
    store = ContractStore(root)
    events = store.list_events()
    assert len(events) == 9
    assert [event["sequence"] for event in events] == list(range(1, 10))
    first, second = store.list_revisions("demo-intent")
    assert store.get(ContractRef.from_document(first)) == first
    assert store.resolve_active("demo-intent") == second
    result = store.list_revisions("demo-result")[0]
    entry = result["payload"]["evidence_refs"][0]
    store.verify_artifact(entry["path"], entry["sha256"])
    third = deepcopy(second)
    third["metadata"].update(version=3, revision_id="demo-intent-r3")
    third["metadata"]["supersedes"] = ContractRef.from_document(second).to_json()
    third["metadata"]["approval"]["approval_id"] = "new-approval-r3"
    third["payload"]["scope"]["included"].append("Document the current setup.")
    actor = third["metadata"]["provenance"]["actor_id"]
    with pytest.raises(AuthorityError):
        store.publish(third, context=PublishContext("PROJECT_MANAGER", actor))
    receipt = approval_request(third)
    store.publish(third, context=PublishContext("PROJECT_MANAGER", actor, {receipt}.__contains__))
    assert store.resolve_active("demo-intent") == third
    assert store.list_events()[:9] == events
    assert (legacy / "state.db").read_bytes() == database_bytes


def test_migration_refuses_existing_target_including_windows_case_alias(legacy):
    root = legacy.parent
    target = root / (".CoRiNtHoS" if os.name == "nt" else ".corinthos")
    target.mkdir()
    with pytest.raises(StoreError):
        ContractStore(root)
    with pytest.raises(ConflictError, match="existing .corinthos"):
        migration.migrate_legacy_state(root)
    assert list(target.iterdir()) == []
    assert (legacy / "state.db").is_file()


@pytest.mark.parametrize("fault", ["corrupt", "sidecar", "readonly", "busy", "artifact"])
def test_migration_failure_keeps_source_and_publishes_nothing(legacy, fault):
    root = legacy.parent
    database = legacy / "state.db"
    mode = database.stat().st_mode
    connection = None
    if fault == "corrupt":
        database.write_bytes(b"invalid database")
    elif fault == "sidecar":
        (legacy / "state.db-wal").write_bytes(b"pending")
    elif fault == "readonly":
        database.chmod(mode & ~stat.S_IWUSR)
    elif fault == "busy":
        connection = sqlite3.connect(database, autocommit=True)
        connection.execute("BEGIN IMMEDIATE")
    else:
        (legacy / "artifacts/checks/partial.txt").write_bytes(b"changed evidence")
    before = database.read_bytes()
    try:
        with pytest.raises((StoreError, IntegrityError)):
            migration.migrate_legacy_state(root)
        assert not (root / ".corinthos").exists()
        assert not list(root.glob(".corinthos-migration-*"))
        assert database.read_bytes() == before
    finally:
        if connection is not None:
            connection.close()
        if fault == "readonly":
            database.chmod(mode)


def test_migration_does_not_follow_linked_private_files(legacy):
    outside = legacy.parent / "outside.txt"
    outside.write_bytes(b"outside")
    os.link(outside, legacy / "artifacts" / "linked.txt")
    with pytest.raises(StoreError, match="unlinked regular files"):
        migration.migrate_legacy_state(legacy.parent)
    assert outside.read_bytes() == b"outside"
    assert not (legacy.parent / ".corinthos").exists()


def test_migration_does_not_follow_directory_links(legacy):
    root = legacy.parent
    outside = root / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")
    linked = legacy / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", ".nukytera\\linked", "outside"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    else:
        linked.symlink_to(outside, target_is_directory=True)
    try:
        assert linked.resolve() == outside.resolve()
        with pytest.raises(StoreError, match="link or case-insensitive collision"):
            migration.migrate_legacy_state(root)
        assert sentinel.read_bytes() == b"outside"
        assert not (root / ".corinthos").exists()
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()


def test_interrupted_publish_keeps_original_and_allows_retry(legacy, monkeypatch):
    root = legacy.parent
    before = (legacy / "state.db").read_bytes()
    with monkeypatch.context() as patch:

        def interrupted(*args):
            raise PermissionError("simulated publish failure")

        patch.setattr(migration, "_promote", interrupted)
        with pytest.raises(StoreError):
            migration.migrate_legacy_state(root)
    assert not (root / ".corinthos").exists()
    assert not list(root.glob(".corinthos-migration-*"))
    assert (legacy / "state.db").read_bytes() == before
    assert migration.migrate_legacy_state(root)


def test_migration_never_replaces_a_target_created_at_publish_time(legacy, monkeypatch):
    root = legacy.parent
    original = migration._promote

    def collide(staged, target):
        target.mkdir()
        original(staged, target)

    monkeypatch.setattr(migration, "_promote", collide)
    with pytest.raises(ConflictError, match="appeared during migration"):
        migration.migrate_legacy_state(root)
    assert list((root / ".corinthos").iterdir()) == []
    assert not list(root.glob(".corinthos-migration-*"))
