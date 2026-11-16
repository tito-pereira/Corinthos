# SPDX-License-Identifier: MPL-2.0
"""Exercise passive registry and Project State projections through real owners."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_validation_repository import _load, _publish, _result, _start
from test_validation_repository import setup as setup

from corinthos import user_state
from corinthos.bootstrap import initialize
from corinthos.contracts import (
    ContractError,
    ContractStore,
    ContractType,
    IntegrityError,
    PublishContext,
    approval_request,
)
from corinthos.lifecycle.repository import LifecycleRepository
from corinthos.project_registry import ProjectRegistryService, _records
from corinthos.project_state import ProjectStateService
from corinthos.work import WorkService

_ACCESS = "2026-01-01T00:00:00Z"


@pytest.fixture
def user(tmp_path, monkeypatch):
    directory = tmp_path / "user"
    monkeypatch.setattr(user_state, "state_root", lambda: directory)
    return directory


def _v1(user, *roots):
    user.mkdir(exist_ok=True)
    document = {
        "version": 1,
        "projects": [{"path": str(root), "last_opened_at": _ACCESS} for root in roots],
    }
    registry = user / "recent-projects.json"
    registry.write_text(json.dumps(document), encoding="utf-8")
    return registry


def _bytes(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_v1_recents_upgrade_on_explicit_access_and_keep_missing_projects(user, tmp_path):
    project = tmp_path / "Offline Notes"
    missing = tmp_path / "Deleted Project"
    other = tmp_path / "Other Project"
    initialize(project)
    initialize(other)
    path = _v1(user, missing, project)
    old = path.read_bytes()
    registry = ProjectRegistryService()
    unavailable, ready = registry.list()
    assert (unavailable.root, unavailable.availability, unavailable.initialized) == (
        str(missing),
        "MISSING",
        False,
    )
    assert (ready.display_name, ready.last_accessed_at, ready.initialized, ready.storage) == (
        "Offline Notes",
        _ACCESS,
        True,
        "ABSENT",
    )
    assert registry.paths() == [str(missing), str(project)]
    assert registry.list(limit=1, offset=1) == [ready]
    assert path.read_bytes() == old and not path.with_suffix(".lock").exists()
    user_state.initialize()
    assert path.read_bytes() == old

    recorded = registry.remember(other)
    assert recorded.availability == "AVAILABLE"
    assert json.loads(path.read_text("utf-8")) == {
        "version": 3,
        "projects": [
            {"root": str(other), "last_accessed_at": recorded.last_accessed_at},
            {"root": str(missing), "last_accessed_at": _ACCESS},
            {"root": str(project), "last_accessed_at": _ACCESS},
        ],
    }
    migrated = path.read_bytes()
    user_state.initialize()
    assert path.read_bytes() == migrated
    with pytest.raises(ContractError):
        registry.remember(missing)
    assert not missing.exists() and path.read_bytes() == migrated


_UNSUPPORTED_VERSIONS = [-1, 0, 4, 99, True, 2.0, "2", None]


@pytest.mark.parametrize("version", _UNSUPPORTED_VERSIONS)
def test_registry_parser_rejects_unknown_versions_before_interpreting_entries(tmp_path, version):
    root = str(tmp_path / "project")
    for entry in (
        {"path": root, "last_opened_at": _ACCESS},
        {"root": root, "last_accessed_at": _ACCESS},
    ):
        with pytest.raises(IntegrityError, match="Unsupported Project Registry version"):
            _records({"version": version, "projects": [entry]})


@pytest.mark.parametrize("version", _UNSUPPORTED_VERSIONS)
def test_registry_rejects_unknown_file_versions_without_rewriting(user, tmp_path, version):
    root = tmp_path / "project"
    initialize(root)
    user.mkdir(exist_ok=True)
    path = user / "recent-projects.json"
    document = {
        "version": version,
        "projects": [{"root": str(root), "last_accessed_at": _ACCESS}],
    }
    original = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    path.write_bytes(original)
    registry = ProjectRegistryService()

    for operation in (
        registry.list,
        registry.paths,
        lambda: registry.last_accessed_at(str(root)),
        lambda: registry.remember(root),
    ):
        with pytest.raises(IntegrityError):
            operation()
        assert path.read_bytes() == original


def test_registry_serializes_explicit_updates_and_keeps_prior_entries(user, tmp_path):
    gone = tmp_path / "gone"
    _v1(user, gone)
    roots = [tmp_path / f"project-{i}" for i in range(6)]
    for root in roots:
        initialize(root)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(ProjectRegistryService().remember, roots))
    entries = ProjectRegistryService().list()
    assert {entry.root for entry in entries} == set(map(str, [gone, *roots]))
    assert entries[-1].root == str(gone)


def test_replaced_registry_root_is_not_followed(user, tmp_path):
    target = tmp_path / "safe-original"
    initialize(target)
    recorded = tmp_path / "replaced"
    try:
        recorded.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this filesystem.")
    path = _v1(user, recorded)
    original = path.read_bytes()
    target_bytes = _bytes(target)
    [entry] = ProjectRegistryService().list()
    assert (entry.availability, entry.storage, entry.initialized) == (
        "UNAVAILABLE",
        "UNVERIFIED",
        False,
    )
    with pytest.raises(ValueError):
        ProjectRegistryService().remember(recorded)
    with pytest.raises(ValueError):
        ProjectStateService().summary(recorded)
    assert _bytes(target) == target_bytes and path.read_bytes() == original


def test_project_summary_is_read_only_and_projects_exact_owner_data(setup, user, monkeypatch):
    repo, *_ = setup
    root = repo.store.database_path.parent.parent
    plan = _publish(setup)
    run = _start(repo, plan)
    project_id = plan.project_id
    lifecycle = LifecycleRepository(repo.store).project(project_id, create=True)
    work = WorkService(root)
    for scope in ("TASK", "CRITICAL"):
        item = work.create(
            scope_level=scope,
            objective=f"Record {scope.lower()} work.",
            acceptance_criteria=["Record is saved."],
        )
    work.transition(item.work_item_id, "ACTIVE")
    registry = _v1(user, root)
    user_original = _bytes(user)
    before = _bytes(root)

    def unexpected(*_args, **_options):
        pytest.fail("Passive Project State must not discover or run providers.")

    monkeypatch.setattr("corinthos.providers.refresh_providers", unexpected)
    monkeypatch.setattr("corinthos.adapters.service.AdapterManager.discover", unexpected)
    monkeypatch.setattr("corinthos.adapters.service.AdapterManager.probe", unexpected)
    summary = ProjectStateService().summary(root)
    assert (summary.initialized, summary.storage, summary.schema_version) == (True, "CURRENT", 9)
    assert summary.project_id == project_id
    assert summary.active_project.contract_type == ContractType.PROJECT
    assert summary.active_workflow.contract_type == ContractType.WORKFLOW
    for ref in [*summary.modules, *summary.technology_decisions]:
        assert ref.project_id == project_id and repo.store.get(ref) is not None
    [module] = summary.modules
    [decision] = summary.technology_decisions
    assert module.contract_type == ContractType.MODULE
    assert decision.contract_type == ContractType.TECHNOLOGY_DECISION
    assert summary.validation.plan == plan and summary.validation.plan_is_active
    assert (summary.validation.run_id, summary.validation.state, summary.validation.result) == (
        run["run_id"],
        "RUNNING",
        None,
    )
    assert summary.lifecycle.tracker_id == lifecycle["tracker_id"]
    assert summary.lifecycle.enabled is False
    assert summary.work_items.total == 2
    assert summary.work_items.by_status["OPEN"] == summary.work_items.by_status["ACTIVE"] == 1
    assert summary.work_items.by_scope["TASK"] == summary.work_items.by_scope["CRITICAL"] == 1
    assert summary.last_accessed_at == _ACCESS and summary.unavailable_sources == []
    assert _bytes(root) == before and _bytes(user) == user_original
    assert registry.exists()

    frozen = repo.freeze(run["run_id"], run["owner"], _result)
    expected = repo.publish(run["run_id"])
    assert frozen["state"] == "FROZEN"
    verified = ProjectStateService().summary(root)
    assert verified.validation.state == "PUBLISHED"
    assert verified.validation.result == expected


def test_summary_preserves_bootstrap_and_missing_state_despite_bad_registry(user, tmp_path):
    root = tmp_path / "workspace"
    initialize(root)
    registry = _v1(user, root)
    registry.write_text('{"version":99}', encoding="utf-8")
    before = _bytes(root)
    actual = ProjectStateService().summary(root)
    assert actual.initialized and actual.project_id is None and actual.active_project is None
    assert actual.work_items is None and actual.storage == "ABSENT"
    assert {"REGISTRY", "WORK_ITEMS"} <= set(actual.unavailable_sources)
    assert _bytes(root) == before and not os.path.lexists(root / ".corinthos/state.db")


def test_multiple_project_identities_have_no_arbitrary_active_reference(user, tmp_path):
    store = ContractStore(tmp_path)
    document = _load("project")
    proof = approval_request(document)
    origin = document["metadata"]["provenance"]
    store.publish(
        document,
        context=PublishContext(origin["engine"], origin["actor_id"], lambda value: value == proof),
    )
    document["metadata"].update(
        contract_id="other-contract", revision_id="other-contract-r1", project_id="other-project"
    )
    document["metadata"]["approval"]["approval_id"] = "other-project-approval"
    other_proof = approval_request(document)
    store.publish(
        document,
        context=PublishContext(
            origin["engine"], origin["actor_id"], lambda value: value == other_proof
        ),
    )
    before = _bytes(tmp_path)
    summary = ProjectStateService().summary(tmp_path)
    assert summary.storage == "CURRENT" and summary.work_items.total == 0
    assert summary.project_id is None and summary.active_project is None
    assert {"PROJECT_IDENTITY", "PROJECT_CONTRACT"} <= set(summary.unavailable_sources)
    assert _bytes(tmp_path) == before and not user.exists()
