# SPDX-License-Identifier: MPL-2.0

import json
import os
import shutil
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Timer
from types import SimpleNamespace

import pytest

from corinthos.contracts import store as storage
from corinthos.contracts.authority import PublishContext, approval_request
from corinthos.contracts.model import ConflictError, ContractRef, IntegrityError, StoreError
from corinthos.contracts.store import ContractStore

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "contracts"


@pytest.fixture
def project():
    return json.loads((_EXAMPLES / "project.json").read_text(encoding="utf-8"))


def _context(document):
    """Simulate an integration that already holds proof for this exact test document."""
    provenance = document["metadata"]["provenance"]
    approval = document["metadata"]["approval"]
    proof = approval_request(document) if approval is not None else None
    return PublishContext(
        provenance["engine"], provenance["actor_id"], lambda value: value == proof
    )


def _publish(store, document):
    return store.publish(document, context=_context(document))


def _revision(previous, revision_id):
    document = deepcopy(previous)
    metadata = document["metadata"]
    metadata.update(
        revision_id=revision_id,
        supersedes=ContractRef.from_document(previous).to_json(),
        version=metadata["version"] + 1,
    )
    metadata["approval"]["approval_id"] = f"test-approval-{revision_id}"
    document["payload"]["vision"] = f"Approved test vision for {revision_id}."
    return document


@pytest.mark.parametrize("has_target_directory", [False, True])
def test_open_store_requires_explicit_migration_for_legacy_state(tmp_path, has_target_directory):
    legacy = tmp_path / ".nukytera"
    legacy.mkdir()
    old_database = legacy / "state.db"
    original = b"Existing private state must be left untouched."
    old_database.write_bytes(original)
    target = tmp_path / ".corinthos"
    if has_target_directory:
        target.mkdir()

    with pytest.raises(StoreError, match="Legacy private state requires explicit migration"):
        ContractStore(tmp_path)

    assert old_database.read_bytes() == original
    assert target.exists() is has_target_directory
    assert not (target / "state.db").exists()


def test_migration_publication_and_history_survive_reopen(tmp_path, project):
    store = ContractStore(tmp_path)
    assert store.database_path == tmp_path / ".corinthos" / "state.db"
    first = _publish(store, project)
    second_document = _revision(project, "project-r2")
    second = _publish(store, second_document)

    reopened = ContractStore(tmp_path)
    assert reopened.get(first) == project
    assert reopened.get(second) == second_document
    assert reopened.list_revisions(first.contract_id) == [project, second_document]
    assert reopened.resolve_active(first.contract_id) == second_document
    detached = reopened.get(first)
    detached["payload"]["vision"] = "Caller-side change"
    assert reopened.get(first) == project

    events = reopened.list_events()
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["revision_id"] for event in events] == [first.revision_id, second.revision_id]
    assert [event["previous_revision_id"] for event in events] == [None, first.revision_id]
    assert all(event["activated"] is True for event in events)
    with closing(sqlite3.connect(reopened.database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (9,)
        assert connection.execute("SELECT * FROM contract_references").fetchall() == [
            (second.revision_id, first.revision_id)
        ]


def test_exact_lookup_requires_all_identity_fields(tmp_path, project):
    store = ContractStore(tmp_path)
    reference = _publish(store, project)
    assert store.get(replace(reference, revision_id="unknown-revision")) is None
    assert store.get(replace(reference, contract_id="unknown-contract")) is None
    assert store.get(replace(reference, project_id="another-project")) is None
    assert store.get(replace(reference, contract_type="MODULE")) is None


def test_retry_does_not_replace_active_revision_or_append_events(tmp_path, project):
    store = ContractStore(tmp_path)
    first = _publish(store, project)
    second_document = _revision(project, "project-r2")
    _publish(store, second_document)
    assert _publish(store, project) == first
    assert store.resolve_active(first.contract_id) == second_document
    assert len(store.list_events()) == 2


def test_reused_identity_and_unchanged_content_cannot_create_a_revision(tmp_path, project):
    store = ContractStore(tmp_path)
    first = _publish(store, project)
    duplicate = _revision(project, "project-r2")
    duplicate["payload"] = deepcopy(project["payload"])
    with pytest.raises(ConflictError, match="unchanged payload"):
        _publish(store, duplicate)

    duplicate["metadata"]["revision_id"] = first.revision_id
    duplicate["payload"]["vision"] = "Different content on the same revision ID."
    with pytest.raises(ConflictError, match="different content"):
        _publish(store, duplicate)

    other = deepcopy(project)
    other["metadata"].update(contract_id="alternate-project-contract", revision_id="alternate-r1")
    with pytest.raises(ConflictError, match="already exists"):
        _publish(store, other)
    assert store.list_revisions(first.contract_id) == [project]
    assert len(store.list_events()) == 1


def test_stale_predecessor_and_inconsistent_active_pointer_conflict(tmp_path, project):
    store = ContractStore(tmp_path)
    first = _publish(store, project)
    second_document = _revision(project, "project-r2")
    _publish(store, second_document)
    with pytest.raises(ConflictError, match="current contract head"):
        _publish(store, _revision(project, "stale-revision"))

    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute(
            "UPDATE contracts SET active_revision_id = ? WHERE contract_id = ?",
            (first.revision_id, first.contract_id),
        )
    with pytest.raises(ConflictError, match="active revision has changed"):
        _publish(store, _revision(second_document, "project-r3"))
    assert store.list_revisions(first.contract_id) == [project, second_document]
    assert len(store.list_events()) == 2


def test_concurrent_writers_cannot_publish_two_successors_to_one_base(tmp_path, project):
    store = ContractStore(tmp_path)
    base = _publish(store, project)
    candidates = [_revision(project, "race-r2-left"), _revision(project, "race-r2-right")]
    ready = Barrier(2, timeout=10)

    def publish_together(candidate):
        ready.wait()
        try:
            return _publish(store, candidate)
        except ConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish_together, candidates))
    published = [result for result in results if isinstance(result, ContractRef)]
    rejected = [result for result in results if isinstance(result, ConflictError)]
    assert len(published) == len(rejected) == 1
    assert "current contract head" in str(rejected[0])
    assert store.get(published[0]) == store.resolve_active(base.contract_id)
    assert len(store.list_revisions(base.contract_id)) == len(store.list_events()) == 2


def test_database_busy_is_reported_as_a_conflict(tmp_path, project, monkeypatch):
    store = ContractStore(tmp_path)
    monkeypatch.setattr(storage, "_LOCK_TIMEOUT", 0.05)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(ConflictError, match="database is busy"):
            _publish(store, project)
        writer.execute("ROLLBACK")
    assert store.list_events() == []
    assert _publish(store, project) == ContractRef.from_document(project)


@pytest.mark.parametrize("has_predecessor", [False, True])
def test_late_audit_failure_rolls_back_the_entire_publication(tmp_path, project, has_predecessor):
    store = ContractStore(tmp_path)
    candidate = project
    if has_predecessor:
        _publish(store, project)
        candidate = _revision(project, "project-r2")
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'internal test trigger details'); END"
        )
    with pytest.raises(StoreError, match="database operation failed") as caught:
        _publish(store, candidate)
    assert "internal test trigger details" not in str(caught.value)
    reference = ContractRef.from_document(candidate)
    assert store.get(reference) is None
    assert store.resolve_active(reference.contract_id) == (project if has_predecessor else None)
    assert store.list_revisions(reference.contract_id) == ([project] if has_predecessor else [])
    assert len(store.list_events()) == int(has_predecessor)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute("DROP TRIGGER reject_event")
    assert _publish(store, candidate) == reference


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE revisions SET document = '{}'",
        "DELETE FROM revisions",
        "INSERT OR REPLACE INTO revisions SELECT * FROM revisions",
        "UPDATE audit_events SET activated = 0",
        "DELETE FROM audit_events",
        "INSERT OR REPLACE INTO audit_events SELECT * FROM audit_events",
    ],
)
def test_sqlite_guards_published_revisions_and_audit_events(tmp_path, project, statement):
    store = ContractStore(tmp_path)
    reference = _publish(store, project)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable|append-only"):
            connection.execute(statement)
    assert store.get(reference) == project
    assert len(store.list_events()) == 1


def test_corrupted_revision_body_is_rejected_on_read(tmp_path, project):
    store = ContractStore(tmp_path)
    reference = _publish(store, project)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute("DROP TRIGGER revisions_no_update")
        connection.execute(
            "UPDATE revisions SET document = '{}' WHERE revision_id = ?", (reference.revision_id,)
        )
    with pytest.raises(IntegrityError, match="inconsistent digest"):
        store.get(reference)


@pytest.mark.parametrize("case", ["foreign", "future", "corrupt"])
def test_unsupported_database_is_never_reinitialized(tmp_path, case):
    private = tmp_path / ".corinthos"
    private.mkdir()
    database = private / "state.db"
    if case == "corrupt":
        database.write_bytes(b"not a SQLite database" * 20)
    else:
        with closing(sqlite3.connect(database, autocommit=True)) as connection:
            if case == "foreign":
                connection.execute("CREATE TABLE unrelated (value TEXT)")
                connection.execute("INSERT INTO unrelated VALUES (?)", ("preserve",))
            else:
                connection.execute("PRAGMA application_id = 1314212697")
                connection.execute("PRAGMA user_version = 42")
    original = database.read_bytes()
    with pytest.raises(StoreError, match="unsupported|corrupt"):
        ContractStore(tmp_path)
    assert database.read_bytes() == original


def test_migration_failure_leaves_an_empty_upgradeable_database(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_MIGRATION_V1", (*storage._MIGRATION_V1, "INVALID SQL"))
        with pytest.raises(StoreError, match="database operation failed"):
            ContractStore(tmp_path)
    database = tmp_path / ".corinthos" / "state.db"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []
    assert ContractStore(tmp_path).list_events() == []


def test_existing_store_detects_changed_database_version(tmp_path):
    store = ContractStore(tmp_path)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute("PRAGMA user_version = 42")
    with pytest.raises(StoreError, match="schema version is unsupported"):
        store.list_events()


def test_reopen_checks_foreign_key_integrity(tmp_path, project):
    store = ContractStore(tmp_path)
    _publish(store, project)
    with closing(sqlite3.connect(store.database_path, autocommit=True)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE contracts SET active_revision_id = 'missing-revision'")
    with pytest.raises(StoreError, match="foreign-key integrity check"):
        ContractStore(tmp_path)


def test_read_only_write_failure_is_reported_without_partial_state(tmp_path, project, monkeypatch):
    store = ContractStore(tmp_path)
    connect = sqlite3.connect

    def read_only(database, **kwargs):
        return connect(database.removesuffix("mode=rw") + "mode=ro", **kwargs)

    monkeypatch.setattr(sqlite3, "connect", read_only)
    with pytest.raises(StoreError, match="read-only"):
        _publish(store, project)
    assert store.list_events() == []
    assert store.get(ContractRef.from_document(project)) is None


@pytest.mark.parametrize(
    "filename", ["state.db", "state.db-journal", "state.db-wal", "state.db-shm"]
)
def test_linked_state_files_cannot_modify_another_project(tmp_path, filename):
    project = tmp_path / "project"
    private = project / ".corinthos"
    private.mkdir(parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside content")
    try:
        os.link(outside, private / filename)
    except PermissionError:
        pytest.skip("The local filesystem policy blocks creation of a hard link.")
    with pytest.raises(StoreError, match="unlinked local files"):
        ContractStore(project)
    assert outside.read_bytes() == b"outside content"
    assert sorted(path.name for path in private.iterdir()) == [filename]


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("allow_existing", [False, True])
def test_database_guard_rechecks_unlinked_sidecar_once(
    tmp_path, monkeypatch, suffix, replacement, allow_existing
):
    database = tmp_path / "state.db"
    sidecar = database.with_name(database.name + suffix)
    sidecar.touch()
    original = Path.lstat
    calls = 0

    def unlinked_sidecar(path, *args, **kwargs):
        nonlocal calls
        if path == sidecar:
            calls += 1
            if calls == 1:
                info = original(path)
                path.unlink()
                if replacement:
                    path.touch()
                # Replay Linux lstat completing after SQLite unlinked its journal.
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=0)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unlinked_sidecar)
    if replacement and not allow_existing:
        with pytest.raises(ConflictError, match="backup already exists"):
            storage._check_database_files(database, allow_existing=allow_existing)
    else:
        storage._check_database_files(database, allow_existing=allow_existing)
    assert calls == 2


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize(
    ("mode", "links", "attributes"),
    [
        (stat.S_IFREG, 0, 0),
        (stat.S_IFREG, 2, 0),
        (stat.S_IFDIR, 0, 0),
        (stat.S_IFLNK, 0, 0),
        (stat.S_IFREG, 0, stat.FILE_ATTRIBUTE_REPARSE_POINT),
    ],
)
def test_database_guard_rejects_persistent_unsafe_files(
    tmp_path, monkeypatch, suffix, mode, links, attributes
):
    database = tmp_path / "state.db"
    unsafe = database.with_name(database.name + suffix)
    original = Path.lstat
    calls = 0

    def unsafe_stat(path, *args, **kwargs):
        nonlocal calls
        if path == unsafe:
            calls += 1
            return SimpleNamespace(st_mode=mode, st_nlink=links, st_file_attributes=attributes)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unsafe_stat)
    with pytest.raises(StoreError, match="unlinked local files"):
        storage._check_database_files(database)
    can_recheck = bool(suffix) and mode == stat.S_IFREG and links == attributes == 0
    assert calls == (2 if can_recheck else 1)


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_database_guard_preserves_sidecar_recheck_io_errors(tmp_path, monkeypatch, suffix):
    database = tmp_path / "state.db"
    sidecar = database.with_name(database.name + suffix)
    original = Path.lstat
    calls = 0
    failure = PermissionError("Sidecar recheck failed")

    def unavailable_sidecar(path, *args, **kwargs):
        nonlocal calls
        if path == sidecar:
            calls += 1
            if calls == 1:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=0)
            raise failure
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unavailable_sidecar)
    with pytest.raises(PermissionError) as caught:
        storage._check_database_files(database)
    assert caught.value is failure
    assert calls == 2


def test_backup_can_reopen_without_overwriting_existing_snapshot(tmp_path, project):
    store = ContractStore(tmp_path)
    reference = _publish(store, project)
    backup = store.backup()
    assert backup == store.database_path.with_name("state.backup.db")
    if os.name == "posix":
        assert backup.stat().st_mode & 0o077 == 0
    original = backup.read_bytes()
    _publish(store, _revision(project, "project-r2"))
    with pytest.raises(ConflictError, match="backup already exists"):
        store.backup()
    assert backup.read_bytes() == original

    restore_root = tmp_path / "backup-inspection"
    restored_private = restore_root / ".corinthos"
    restored_private.mkdir(parents=True)
    shutil.copyfile(backup, restored_private / "state.db")
    restored = ContractStore(restore_root)
    assert restored.get(reference) == restored.resolve_active(reference.contract_id) == project
    assert restored.list_revisions(reference.contract_id) == [project]
    assert len(restored.list_events()) == 1


def test_backup_never_replaces_a_racing_destination(tmp_path, project, monkeypatch):
    store = ContractStore(tmp_path)
    _publish(store, project)
    operation = "rename" if os.name == "nt" else "link"
    install = getattr(os, operation)
    backup = store.database_path.with_name("state.backup.db")

    def competing_backup(source, destination):
        destination.write_bytes(b"preserve a competing backup")
        install(source, destination)

    monkeypatch.setattr(os, operation, competing_backup)
    with pytest.raises(ConflictError, match="backup already exists"):
        store.backup()
    assert backup.read_bytes() == b"preserve a competing backup"
    assert list(backup.parent.glob("state.backup-*.db")) == []


def test_backup_has_bounded_busy_wait(tmp_path, project, monkeypatch):
    store = ContractStore(tmp_path)
    _publish(store, project)
    monkeypatch.setattr(storage, "_LOCK_TIMEOUT", 0.05)
    mkstemp = storage.tempfile.mkstemp
    with closing(
        sqlite3.connect(store.database_path, autocommit=True, check_same_thread=False)
    ) as writer:
        release = Timer(2.0, writer.execute, args=("ROLLBACK",))

        def lock_before_copy(*args, **kwargs):
            writer.execute("BEGIN EXCLUSIVE")
            release.start()
            return mkstemp(*args, **kwargs)

        monkeypatch.setattr(storage.tempfile, "mkstemp", lock_before_copy)
        try:
            with pytest.raises(ConflictError, match="remained busy during backup"):
                store.backup()
        finally:
            release.cancel()
            if release.ident is not None:
                release.join()
            if writer.in_transaction:
                writer.execute("ROLLBACK")
    assert not store.database_path.with_name("state.backup.db").exists()
    assert list(store.database_path.parent.glob("state.backup-*.db")) == []
