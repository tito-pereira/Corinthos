# SPDX-License-Identifier: MPL-2.0
"""Exercise passive routing, explicit mutations and preserved migration failures."""

import json
import os
import shutil
import sqlite3
from hashlib import sha256

import pytest
from jsonschema import ValidationError
from test_application_workspace import _call, _transcript
from test_application_workspace import wire as wire
from test_context_migration import current_v8 as current_v8
from test_external_import import _git
from test_external_import import repository as repository
from test_state_migration import original_state as original_state

from corinthos.application import onboarding, workspace
from corinthos.bootstrap import _MAX_PROFILE, _read_profile, initialize_private
from corinthos.contracts import ConflictError, ContractStore, IntegrityError
from corinthos.contracts.migration import (
    _ONBOARDING_INTENT,
    _inventory,
    migrate_legacy_state,
    onboarding_migration,
    record_onboarding_migration,
)
from corinthos.contracts.model import canonical_json
from corinthos.external_import.service import _new_intent
from corinthos.project_registry import ProjectRegistryService
from corinthos.project_status import project_status


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: user)
    return user


def _files(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _open(root, *, migrate=False):
    inspected = workspace.project_inspect(str(root))
    return workspace.project_open_checked(
        str(root), inspected["inspection_digest"], migrate=migrate
    )


@pytest.mark.parametrize("database", [False, True])
def test_current_and_bootstrap_open_without_writable_constructor(tmp_path, monkeypatch, database):
    root = tmp_path / "managed"
    root.mkdir()
    initialize_private(root)
    if database:
        ContractStore(root)
    before = _files(root)
    monkeypatch.setattr(
        onboarding, "ContractStore", lambda *_: pytest.fail("Current Open migrated.")
    )
    inspected = workspace.project_inspect(str(root))
    assert inspected["status"] == "CURRENT"
    assert len(inspected["inspection_digest"]) == 64
    assert ProjectRegistryService().paths() == []
    assert _files(root) == before
    result = _open(root)
    assert result["initialized"]
    assert _files(root) == before
    assert ProjectRegistryService().paths() == [str(root)]


@pytest.mark.parametrize("description_length", [20_000, 120_000])
def test_canonical_large_bootstrap_profiles_inspect_and_open(tmp_path, description_length):
    root = tmp_path / "large-profile"
    root.mkdir()
    initialize_private(root)
    path = root / ".corinthos/bootstrap.json"
    profile = json.loads(path.read_bytes())
    profile["standard"]["commandments"]["M1"]["description"] = "x" * description_length
    profile["standard_sha256"] = sha256(
        canonical_json(profile["standard"]).encode("utf-8")
    ).hexdigest()
    path.write_bytes(canonical_json(profile).encode("utf-8"))
    assert 16_384 < path.stat().st_size <= _MAX_PROFILE
    assert _read_profile(path) == profile
    assert project_status(root).initialized
    before = _files(root)
    inspected = workspace.project_inspect(str(root))
    assert inspected["status"] == "CURRENT"
    assert _files(root) == before
    assert _open(root)["initialized"]
    assert _files(root) == before
    assert ProjectRegistryService().paths() == [str(root)]


@pytest.mark.parametrize("invalid", ["oversized", "malformed"])
def test_bootstrap_bounds_and_validation_remain_fail_closed(tmp_path, invalid):
    root = tmp_path / "invalid-profile"
    root.mkdir()
    initialize_private(root)
    path = root / ".corinthos/bootstrap.json"
    if invalid == "oversized":
        path.write_bytes(b" " * (_MAX_PROFILE + 1))
    else:
        path.write_bytes(b"{}")
    before = _files(root)
    result = workspace.project_inspect(str(root))
    assert result["status"] == "INVALID"
    assert result["inspection_digest"] is None
    assert _files(root) == before
    assert ProjectRegistryService().paths() == []


def test_selected_regular_file_returns_directory_guidance_without_mutation(tmp_path, isolated_user):
    root = tmp_path / "selected-file"
    root.write_bytes(b"Owner bytes\x00\xff")
    before = _files(tmp_path)
    inspected = workspace.project_inspect(str(root))
    assert inspected["status"] == "UNAVAILABLE"
    assert inspected["reason"] == "Select an ordinary directory."
    assert inspected["inspection_digest"] is None
    assert inspected["can_import"] is False
    assert _files(tmp_path) == before
    assert not isolated_user.exists()


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("empty", "UNMANAGED"),
        ("nonempty", "UNMANAGED"),
        ("missing", "MISSING"),
        ("empty-private", "INVALID"),
        ("corrupt", "INVALID"),
        ("legacy-name", "INVALID"),
        ("bad-create-intent", "INVALID"),
    ],
)
def test_passive_classification_has_no_files_or_recency(tmp_path, isolated_user, kind, expected):
    root = tmp_path / "selected"
    if kind != "missing":
        root.mkdir()
    if kind == "nonempty":
        (root / "owner.txt").write_bytes(b"Owner bytes\x00\xff")
    if kind in {"empty-private", "corrupt"}:
        (root / ".corinthos").mkdir()
    if kind == "corrupt":
        (root / ".corinthos/state.db").write_bytes(b"corrupt")
    if kind == "legacy-name":
        (root / ".nukytera").mkdir()
    if kind == "bad-create-intent":
        initialize_private(root)
        (root / ".corinthos/project-create.json").write_text("{}")
    before = _files(tmp_path)
    result = workspace.project_inspect(str(root))
    assert result["status"] == expected
    assert not result["can_import"]
    assert result["inspection_digest"] is None or expected == "UNMANAGED"
    assert _files(tmp_path) == before
    assert not isolated_user.exists()


@pytest.mark.parametrize("bootstrap,database", [(False, False), (True, False), (True, True)])
def test_import_intent_owns_recovery_even_with_valid_bootstrap(repository, bootstrap, database):
    _new_intent(repository, "a" * 64, [])
    if bootstrap:
        initialize_private(repository)
    if database:
        ContractStore(repository)
    before = _files(repository)
    inspected = workspace.project_inspect(str(repository))
    assert inspected["status"] == "INCOMPLETE_IMPORT"
    assert inspected["inspection_digest"] is None
    assert _files(repository) == before
    assert ProjectRegistryService().paths() == []


def test_import_eligibility_uses_existing_metadata_policy_without_content_scan(
    repository, monkeypatch
):
    monkeypatch.setattr(
        "corinthos.external_import.discovery._file", lambda *_: pytest.fail("Read content")
    )
    monkeypatch.setattr(
        "corinthos.external_import.discovery._walk_untracked", lambda *_: pytest.fail("Walk")
    )
    before = _files(repository)
    result = workspace.project_inspect(str(repository))
    assert result["status"] == "UNMANAGED" and result["can_import"]
    assert _files(repository) == before
    _git(repository, "update-index", "--skip-worktree", "README.md")
    assert not workspace.project_inspect(str(repository))["can_import"]


@pytest.mark.parametrize("kind", ["linked", "partial", "sparse", "bare"])
def test_unsupported_git_shapes_never_offer_import(repository, kind):
    if kind == "linked":
        linked = repository.parent / "linked"
        _git(repository, "worktree", "add", "-b", "linked", str(linked))
        repository = linked
    elif kind == "partial":
        _git(repository, "config", "remote.origin.promisor", "true")
    elif kind == "sparse":
        _git(repository, "config", "core.sparseCheckout", "true")
    else:
        _git(repository, "config", "core.bare", "true")
    assert workspace.project_inspect(str(repository))["can_import"] is False


@pytest.mark.parametrize("change", ["replace-root", "private", "database"])
def test_changed_current_selection_cannot_register_or_migrate(tmp_path, change):
    root = tmp_path / "current"
    root.mkdir()
    initialize_private(root)
    original = workspace.project_inspect(str(root))
    if change == "replace-root":
        root.rename(tmp_path / "preserved")
        root.mkdir()
        initialize_private(root)
    elif change == "private":
        (root / ".corinthos").rename(root / ".old-corinthos")
        initialize_private(root)
    else:
        ContractStore(root)
    before = _files(root)
    with pytest.raises(ConflictError):
        workspace.project_open_checked(str(root), original["inspection_digest"])
    assert _files(root) == before
    assert ProjectRegistryService().paths() == []


def test_supported_older_schema_requires_explicit_consent(current_v8):
    root = current_v8
    before = _files(root)
    inspected = workspace.project_inspect(str(root))
    assert inspected["status"] == "MIGRATION_REQUIRED" and inspected["schema_version"] == 8
    with pytest.raises(ConflictError):
        workspace.project_open_checked(str(root), inspected["inspection_digest"])
    assert _files(root) == before
    result = workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    assert result["storage"] == "CURRENT" and result["schema_version"] == 9
    assert len(list((root / ".corinthos").glob("state.pre-v9-*.db"))) == 1


@pytest.fixture
def legacy_root(tmp_path, original_state):
    root = tmp_path / "legacy"
    root.mkdir()
    shutil.copytree(original_state, root / ".nukytera")
    return root


@pytest.mark.parametrize("migrate", [False, True])
@pytest.mark.parametrize("substitution", ["root", "private"])
def test_reinspection_identity_substitution_cannot_adopt_replacement(
    tmp_path, original_state, monkeypatch, migrate, substitution
):
    root = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    root.mkdir()
    replacement.mkdir()
    private_name = ".nukytera" if migrate else ".corinthos"
    if migrate:
        shutil.copytree(original_state, root / private_name)
        shutil.copytree(original_state, replacement / private_name)
    else:
        initialize_private(root)
        initialize_private(replacement)
    original_owner = b"Reviewed original owner bytes"
    (root / "owner.txt").write_bytes(original_owner)
    (replacement / "owner.txt").write_bytes(b"Unreviewed replacement owner bytes")
    (replacement / private_name / "replacement-marker").write_bytes(b"Unreviewed private bytes")
    original_bytes = _files(root)
    original_private = _files(root / private_name)
    replacement_bytes = _files(replacement)
    replacement_private = _files(replacement / private_name)
    reviewed = workspace.project_inspect(str(root))
    actual_inspect = onboarding.inspect_project
    preserved = tmp_path / "preserved"
    swapped = False

    def substitute_after_reinspection(value):
        nonlocal swapped
        observation = actual_inspect(value)
        if not swapped:
            swapped = True
            if substitution == "root":
                root.rename(preserved)
                replacement.rename(root)
            else:
                (root / private_name).rename(preserved)
                (replacement / private_name).rename(root / private_name)
        return observation

    monkeypatch.setattr(onboarding, "inspect_project", substitute_after_reinspection)
    with pytest.raises(IntegrityError, match="changed"):
        workspace.project_open_checked(str(root), reviewed["inspection_digest"], migrate=migrate)
    assert swapped
    assert ProjectRegistryService().paths() == []
    if substitution == "root":
        assert _files(preserved) == original_bytes
        assert _files(root) == replacement_bytes
    else:
        assert _files(preserved) == original_private
        assert _files(root / private_name) == replacement_private
        assert (root / "owner.txt").read_bytes() == original_owner
    if migrate:
        assert not (root / ".corinthos").exists()


def test_verified_legacy_copy_requires_consent_preserves_source_and_reopens(legacy_root):
    root = legacy_root
    before = _inventory(root / ".nukytera")
    inspected = workspace.project_inspect(str(root))
    assert inspected["status"] == "MIGRATION_REQUIRED"
    assert not (root / ".corinthos").exists()
    with pytest.raises(ConflictError):
        workspace.project_open_checked(str(root), inspected["inspection_digest"])
    result = workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    assert result["storage"] == "CURRENT" and result["schema_version"] == 9
    assert _inventory(root / ".nukytera") == before
    assert onboarding_migration(root)["phase"] == "COMPLETE"
    assert workspace.project_inspect(str(root))["status"] == "CURRENT"
    assert _open(root)["root"] == str(root)


@pytest.mark.parametrize("phase", ["copy", "checkpoint", "open", "bootstrap", "complete"])
def test_interrupted_legacy_phases_preserve_both_stores(legacy_root, monkeypatch, phase):
    root = legacy_root
    original = _inventory(root / ".nukytera")
    inspected = workspace.project_inspect(str(root))
    actual_copy = onboarding.migrate_legacy_state
    actual_record = onboarding.record_onboarding_migration
    actual_store = onboarding.ContractStore
    actual_bootstrap = onboarding.initialize_private

    def fail():
        raise OSError("Simulated interruption")

    def copy(*args):
        actual_copy(*args)
        if phase == "copy":
            fail()

    def record(*args):
        actual_record(*args)
        if (phase == "checkpoint" and args[-1] == "COPIED") or (
            phase == "complete" and args[-1] == "COMPLETE"
        ):
            fail()

    def store(*args):
        value = actual_store(*args)
        if phase == "open":
            fail()
        return value

    def bootstrap(*args):
        actual_bootstrap(*args)
        if phase == "bootstrap":
            fail()

    with monkeypatch.context() as patch:
        patch.setattr(onboarding, "migrate_legacy_state", copy)
        patch.setattr(onboarding, "record_onboarding_migration", record)
        patch.setattr(onboarding, "ContractStore", store)
        patch.setattr(onboarding, "initialize_private", bootstrap)
        with pytest.raises(OSError):
            workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    assert _inventory(root / ".nukytera") == original
    assert (root / ".corinthos/state.db").exists()
    assert ProjectRegistryService().paths() == []
    observed = workspace.project_inspect(str(root))
    if phase in {"copy", "checkpoint", "open"}:
        assert observed["status"] == "MIGRATION_REQUIRED"
        assert _open(root, migrate=True)["schema_version"] == 9
    elif phase == "complete":
        assert observed["status"] == "CURRENT"
        assert _open(root)["schema_version"] == 9
    else:
        assert observed["status"] == "INVALID"
        assert observed["inspection_digest"] is None
    assert _inventory(root / ".nukytera") == original


@pytest.mark.parametrize("change", ["malformed", "digest", "source", "target", "extra", "linked"])
def test_checkpoint_cannot_adopt_malformed_stale_or_replaced_state(legacy_root, change):
    root = legacy_root
    migrate_legacy_state(root)
    record_onboarding_migration(root, onboarding._digest(_inventory(root / ".nukytera")), "COPIED")
    path = root / ".corinthos" / _ONBOARDING_INTENT
    if change == "malformed":
        path.write_text("{}")
    elif change == "digest":
        value = json.loads(path.read_bytes())
        value["source_digest"] = "0" * 64
        path.write_text(canonical_json(value), encoding="utf-8")
    elif change == "source":
        (root / ".nukytera").rename(root / ".original")
        shutil.copytree(root / ".original", root / ".nukytera")
    elif change == "target":
        (root / ".corinthos").rename(root / ".copied")
        shutil.copytree(root / ".copied", root / ".corinthos")
    elif change == "extra":
        (root / ".corinthos/unowned.txt").write_bytes(b"Preserve")
    else:
        linked = root / "checkpoint.json"
        path.rename(linked)
        os.link(linked, path)
    before = _files(root)
    assert workspace.project_inspect(str(root))["status"] == "INVALID"
    assert _files(root) == before
    assert ProjectRegistryService().paths() == []


def test_completed_migration_can_retry_failed_registration(legacy_root, monkeypatch):
    root = legacy_root
    inspected = workspace.project_inspect(str(root))
    with monkeypatch.context() as patch:
        patch.setattr(
            ProjectRegistryService, "remember", lambda *_: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(OSError):
            workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    before = _files(root)
    assert workspace.project_inspect(str(root))["status"] == "CURRENT"
    assert _open(root)["schema_version"] == 9
    assert _files(root) == before


@pytest.mark.parametrize("change", ["artifact", "unsupported-schema", "wrong-application"])
def test_legacy_preflight_verifies_markers_and_artifact_evidence(legacy_root, change):
    root = legacy_root
    legacy = root / ".nukytera"
    if change == "artifact":
        (legacy / "artifacts/checks/partial.txt").write_bytes(b"Changed evidence")
    else:
        with sqlite3.connect(legacy / "state.db") as connection:
            connection.execute(
                "PRAGMA user_version = 100"
                if change == "unsupported-schema"
                else "PRAGMA application_id = 100"
            )
    before = _files(root)
    assert workspace.project_inspect(str(root))["status"] == "INVALID"
    assert _files(root) == before
    assert not (root / ".corinthos").exists()


def test_legacy_inventory_change_invalidates_review_before_copying(legacy_root):
    root = legacy_root
    inspected = workspace.project_inspect(str(root))
    (root / ".nukytera/snapshots/retained.json").write_bytes(b'{"changed":true}')
    before = _files(root)
    with pytest.raises(ConflictError):
        workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    assert _files(root) == before
    assert not (root / ".corinthos").exists()


def test_source_replacement_during_copy_is_preserved_and_never_checkpointed(
    legacy_root, monkeypatch
):
    root = legacy_root
    actual = onboarding.migrate_legacy_state

    def replace(*args):
        actual(*args)
        (root / ".nukytera").rename(root / ".original-legacy")
        shutil.copytree(root / ".original-legacy", root / ".nukytera")

    inspected = workspace.project_inspect(str(root))
    monkeypatch.setattr(onboarding, "migrate_legacy_state", replace)
    with pytest.raises(IntegrityError):
        workspace.project_open_checked(str(root), inspected["inspection_digest"], migrate=True)
    assert _inventory(root / ".nukytera") == _inventory(root / ".original-legacy")
    assert not (root / ".corinthos" / _ONBOARDING_INTENT).exists()
    assert ProjectRegistryService().paths() == []


def test_v2_contracts_and_mutating_notification_guard(tmp_path, wire):
    root = tmp_path / "rpc"
    root.mkdir()
    initialize_private(root)
    digest = workspace.project_inspect(str(root))["inspection_digest"]
    requests = [
        _call("system.handshake", 1, protocol_version=2),
        _call("project.inspect", 2, root=str(root)),
        _call("project.open_checked", None, root=str(root), inspection_digest=digest),
        _call("project.open_checked", 3, root=str(root), inspection_digest=digest),
        _call("project.open_checked", 4, root=str(root), inspection_digest="bad"),
    ]
    results = _transcript(requests)
    assert {"project.inspect", "project.open_checked"} <= set(results[1]["result"]["methods"])
    for request in (requests[1], requests[3]):
        wire("request", request)
    wire("project-inspect-result", results[2]["result"])
    wire("project-summary-result", results[3]["result"])
    assert results[4]["error"]["code"] == -32602
    with pytest.raises(ValidationError):
        wire("request", requests[2])
    with pytest.raises(ValidationError):
        wire("request", requests[4])
    assert ProjectRegistryService().paths() == [str(root)]


@pytest.mark.parametrize("value", [None, 1, "bad", "A" * 64, "f" * 63])
def test_invalid_checked_open_digests_have_no_project_mutation(tmp_path, value):
    root = tmp_path / "selected"
    root.mkdir()
    initialize_private(root)
    before = _files(root)
    with pytest.raises(ValueError):
        workspace.project_open_checked(str(root), value)
    assert _files(root) == before


def test_cli_shared_open_retains_its_existing_schema_upgrade(current_v8):
    assert project_status(current_v8).schema_version == 8
    assert workspace.project_open(str(current_v8))["schema_version"] == 9
