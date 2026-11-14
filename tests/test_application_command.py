# SPDX-License-Identifier: MPL-2.0
"""Verify the bounded Command read model against real project-owned state."""

import json

import pytest
import test_application_workspace as workspace_tests
import test_external_import as import_tests
import test_validation_runtime as validation_tests
from jsonschema import ValidationError
from test_application_workspace import _call, _transcript
from test_validation_runtime import _review

from corinthos.application import adaptive, command
from corinthos.bootstrap import initialize_private
from corinthos.context import ContextService
from corinthos.contracts import ContractStore, PublishContext, approval_request
from corinthos.external_import.service import ExternalProjectImportService
from corinthos.project_registry import ProjectRegistryService
from corinthos.project_state import ProjectStateService
from corinthos.sessions.repository import SessionRepository
from corinthos.work import WorkService

wire = workspace_tests.wire
repository = import_tests.repository
audit = validation_tests.audit


def _work(root, objective, *, scope="TASK"):
    return WorkService(root).create(
        scope_level=scope,
        objective=objective,
        acceptance_criteria=["Verify the behavior in the fixture."],
    )


def _session(root, item, provider):
    owner = SessionRepository(root)
    session = owner.create(
        item.work_item_id,
        provider=provider,
        owner_id="fixture-owner",
        timeout_seconds=120,
        envelope_sha256="a" * 64,
    )
    args = session.session_id, "fixture-owner", session.run_generation
    owner.running(*args)
    return owner, args


def _files(root):
    return {
        file.relative_to(root).as_posix(): file.read_bytes()
        for file in root.rglob("*")
        if file.is_file()
    }


def _codes(card):
    return {(row["reason_code"], row["owner_id"]) for row in card["attention"]}


@pytest.mark.parametrize("repository", ["sha256"], indirect=True)
def test_command_is_read_only_over_multiple_native_and_imported_projects(
    tmp_path, monkeypatch, repository, wire
):
    native = tmp_path / "native"
    native.mkdir()
    initialize_private(native)
    native_work = _work(native, "Inspect the native inputs.")
    native_session, native_args = _session(native, native_work, "codex")
    native_session.observe(
        *native_args,
        "USAGE",
        {},
        usage={"input_tokens": 19, "reported_cost": "0.25", "reported_cost_currency": "EUR"},
    )
    registry = ProjectRegistryService()
    registry.remember(native)

    importer = ExternalProjectImportService()
    inventory = importer.inspect(repository)
    imported = importer.commit(
        repository,
        inventory["discovery_digest"],
        [row["code"] for row in inventory["ambiguities"] if row["required"]],
    )
    assert imported["status"] == "IMPORTED"
    imported_work = _work(repository, "Inspect README", scope="CHANGE")
    projection = ContextService(repository).create(imported_work.work_item_id, "claude")
    _, imported_args = _session(repository, imported_work, "claude")
    expected = ProjectStateService().summary(repository)
    expected_work = adaptive.flow_status(repository, imported_work.work_item_id)
    files = _files(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Command attempted a mutation or provider probe.")

    monkeypatch.setattr(ContractStore, "__init__", forbidden)
    monkeypatch.setattr(ProjectRegistryService, "remember", forbidden)
    monkeypatch.setattr("corinthos.adapters.process_control.start_process", forbidden)
    monkeypatch.setattr("corinthos.external_import.discovery.scan", forbidden)
    observed = command.command_overview()
    assert observed == command.command_overview()
    wire("command-overview-result", observed)
    assert [card["root"] for card in observed["projects"]] == [str(repository), str(native)]
    external, internal = observed["projects"]
    assert external["project_id"] == imported["project_id"]
    assert external["import_status"] == "IMPORTED"
    assert external["external_import"]["summary"]["object_format"] == "sha256"
    assert external["work_counts"]["by_status"] == expected.work_items.by_status
    assert external["focus_work_items"][0]["stage"] == expected_work["stage"]
    assert (
        external["focus_work_items"][0]["latest_projection"] == expected_work["latest_projection"]
    )
    assert external["latest_context"]["projection_id"] == projection.projection_id
    assert external["sessions"]["active"]["session_id"] == imported_args[0]
    assert external["sessions"]["active"]["input_tokens"] is None
    assert internal["sessions"]["active"]["session_id"] == native_args[0]
    assert internal["sessions"]["active"]["input_tokens"] == 19
    assert internal["sessions"]["active"]["reported_cost_currency"] == "EUR"
    assert internal["external_import"] is None
    assert external["validation"]["state"] == "NOT_VERIFIED"
    assert command.command_project(native) == internal
    assert _files(tmp_path) == files


def test_focus_is_ordered_by_active_owner_then_priority_without_fake_canonical_item(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    root = tmp_path / "sample"
    root.mkdir()
    initialize_private(root)
    first = _work(root, "First active item.")
    second = _work(root, "Second active item.")
    blocked = _work(root, "Blocked item.")
    active_owner = _work(root, "Active session on an open item.")
    work = WorkService(root)
    for item, status in ((first, "ACTIVE"), (second, "ACTIVE"), (blocked, "BLOCKED")):
        work.transition(item.work_item_id, status, expected_updated_at=item.updated_at)
    sessions, args = _session(root, active_owner, "codex")
    sessions.block_reconciliation(*args)
    card = command.command_project(root)
    expected = [active_owner.work_item_id, first.work_item_id, second.work_item_id]
    assert [row["work_item_id"] for row in card["focus_work_items"]] == expected
    assert card["work_counts"]["total"] == 4
    assert ("WORK_ITEM_BLOCKED", blocked.work_item_id) in _codes(card)
    assert ("CLEANUP_UNCONFIRMED", args[0]) in _codes(card)
    assert {row["authority"] for row in card["focus_work_items"]} == {"OPERATIONAL"}
    assert {row["authority"] for row in card["attention"]} == {"DERIVED"}


def test_focus_retains_work_namespace_when_canonical_project_is_published(
    tmp_path, monkeypatch, wire
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    root = tmp_path / "project"
    root.mkdir()
    initialize_private(root)
    item = _work(root, "Keep the original namespace.")
    owner, args = _session(root, item, "codex")
    owner.block_reconciliation(*args)
    document = json.loads(
        (validation_tests._ROOT / "examples/contracts/project.json").read_text(encoding="utf-8")
    )
    provenance = document["metadata"]["provenance"]
    approval = approval_request(document)
    project = ContractStore(root).publish(
        document,
        context=PublishContext(
            provenance["engine"], provenance["actor_id"], lambda exact: exact == approval
        ),
    )
    card = command.command_project(root)
    assert card["project_id"] == project.project_id
    assert card["project_id"] != item.project_id
    assert card["sessions"]["active"]["project_id"] == item.project_id
    assert card["focus_work_items"][0]["work_item_id"] == item.work_item_id
    assert card["focus_work_items"][0]["project_id"] == item.project_id
    assert card["attention"][0]["project_id"] == item.project_id
    wire("command-project-result", card)


def test_command_skips_unbounded_adaptive_proof_with_explicit_unknown(tmp_path, monkeypatch, wire):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    monkeypatch.setattr(command, "_MAX_VALIDATION_HISTORY", 0)
    root = tmp_path / "project"
    root.mkdir()
    initialize_private(root)
    document = json.loads(
        (validation_tests._ROOT / "examples/contracts/project.json").read_text(encoding="utf-8")
    )
    provenance = document["metadata"]["provenance"]
    approval = approval_request(document)
    ContractStore(root).publish(
        document,
        context=PublishContext(
            provenance["engine"], provenance["actor_id"], lambda exact: exact == approval
        ),
    )
    item = _work(root, "Retain operational facts.", scope="PROJECT")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Command traversed adaptive proof over its history ceiling.")

    monkeypatch.setattr("corinthos.work.adaptive.workflow_validation", forbidden)
    card = command.command_project(root)
    focus = card["focus_work_items"][0]
    assert focus["work_item_id"] == item.work_item_id
    assert focus["stage"] is None
    assert focus["next_actions"] == []
    assert focus["validation_state"] == "NOT_VERIFIED"
    wire("command-project-result", card)


def test_pages_preserve_registry_order_and_isolate_corrupt_or_missing_roots(
    tmp_path, monkeypatch, wire
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    registry = ProjectRegistryService()
    roots = [tmp_path / f"project-{i}" for i in range(12)]
    for root in roots:
        root.mkdir()
        initialize_private(root)
        registry.remember(root)
    (roots[-1] / ".corinthos/state.db").write_bytes(b"invalid SQLite")
    for child in (roots[-2] / ".corinthos").iterdir():
        child.unlink()
    (roots[-2] / ".corinthos").rmdir()
    roots[-2].rmdir()
    files = _files(tmp_path)
    first = command.command_overview()
    assert first["next_offset"] == 10
    second = command.command_overview(offset=10)
    assert second["next_offset"] is None
    projects = first["projects"] + second["projects"]
    assert [card["root"] for card in projects] == [str(root) for root in reversed(roots)]
    assert projects[0]["availability"] == "UNAVAILABLE"
    assert ("PROJECT_UNAVAILABLE", None) in _codes(projects[0])
    assert projects[1]["availability"] == "MISSING"
    assert all(card["availability"] == "AVAILABLE" for card in projects[2:])
    wire("command-overview-result", first)
    wire("command-overview-result", second)
    assert _files(tmp_path) == files


def test_project_validation_reports_exact_independent_result_without_claiming_completion(
    audit, wire
):
    runner, plan, candidate = audit
    root = runner.store.database_path.parent.parent
    result = runner.run(plan, candidate, evaluator="fixture-evaluator", reviewer=_review)
    card = command.command_project(root)
    assert card["validation"]["active_plan_refs"] == [plan.to_json()]
    assert card["validation"]["latest_run"]["plan"] == plan.to_json()
    assert card["validation"]["latest_run"]["result"] == result.to_json()
    assert card["validation"]["outcome"] == "SATISFIED"
    assert card["validation"]["state"] == "NOT_VERIFIED"
    wire("command-project-result", card)
    (candidate / "README.md").write_text("different", encoding="utf-8")
    rejected = runner.run(plan, candidate, evaluator="fixture-evaluator", reviewer=_review)
    card = command.command_project(root)
    assert card["validation"]["latest_run"]["result"] == rejected.to_json()
    assert card["validation"]["state"] == "BLOCKED"
    assert ("VALIDATION_RESULT_BLOCKED", card["validation"]["latest_run"]["run_id"]) in _codes(card)
    assert "evidence_refs" not in json.dumps(card)


def test_command_v2_request_and_result_are_strict_and_v1_does_not_expose_methods(tmp_path, wire):
    missing = str(tmp_path / "missing")
    request = _call("command.project", "project", root=missing)
    overview = _call("command.overview", "overview", limit=1, offset=0)
    notification = _call("command.overview", None)
    for row in (request, overview, notification):
        wire("request", row)
    result = _transcript(
        [_call("system.handshake", 1, protocol_version=2), request, overview, notification]
    )
    assert {"command.overview", "command.project"} <= set(result[1]["result"]["methods"])
    wire("command-project-result", result[2]["result"])
    wire("command-overview-result", result[3]["result"])
    assert len(result) == 4
    malformed = _call("command.overview", "invalid", limit=11, unexpected=True)
    with pytest.raises(ValidationError):
        wire("request", malformed)
    with pytest.raises(ValidationError):
        wire("command-project-result", result[2]["result"] | {"raw_context": {}})
    v1 = _transcript([_call("system.handshake", 1, protocol_version=1), request])
    assert "command.project" not in v1[1]["result"]["methods"]
    assert v1[2]["error"]["code"] == -32601
