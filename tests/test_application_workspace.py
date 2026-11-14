# SPDX-License-Identifier: MPL-2.0
"""Verify frozen v1 and the shared Phase 2 CLI and RPC operations."""

import hashlib
import io
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from corinthos.application import sidecar, workspace
from corinthos.bootstrap import initialize
from corinthos.cli import main
from corinthos.contracts import ContractStore

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMAS = _ROOT / "schemas/rpc"


def _call(method, request_id, **params):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        msg["id"] = request_id
    return msg


def _transcript(requests):
    source = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in requests))
    output, diagnostics = io.BytesIO(), io.StringIO()
    sidecar.serve(source, output, diagnostics)
    assert diagnostics.getvalue() == ""
    assert all(len(line) <= sidecar.MAX_FRAME_BYTES for line in output.getvalue().splitlines())
    return [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.fixture
def wire():
    schemas = {
        (file.parent.name, file.name.removesuffix(".schema.json")): json.loads(
            file.read_text("utf-8")
        )
        for file in _SCHEMAS.glob("v*/*.schema.json")
    }
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )

    def validate(name, data):
        Draft202012Validator(schemas["v2", name], registry=registry).validate(data)

    return validate


@pytest.fixture
def initialized_root(tmp_path, monkeypatch):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    root = tmp_path / "project"
    initialize(root)
    return str(root)


def test_phase_one_rpc_schema_freeze():
    record = (_ROOT / "docs/v0.1/rpc-v1.sha256").read_text("ascii").splitlines()
    expected = {name: digest for digest, name in (line.split() for line in record)}
    assert len(expected) == len(record) == 9
    # Git records LF bytes. A Windows checkout may materialize CRLF.
    actual = {
        file.name: hashlib.sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for file in (_SCHEMAS / "v1").iterdir()
        if file.is_file()
    }
    assert actual == expected


def test_connection_version_cannot_change_or_grant_v2_by_notification(initialized_root):
    root = initialized_root
    output = _transcript(
        [
            _call("system.handshake", None, protocol_version=2),
            _call("projects.list", 1),
            _call("system.handshake", 2, protocol_version=1),
            _call("project.summary", 3, root=root),
            _call("system.handshake", 4, protocol_version=2),
            _call("system.version", 5),
        ]
    )
    assert output[0]["params"] == {"protocol_version": 1}
    assert output[1]["error"]["code"] == -32001
    assert output[2]["result"]["methods"] == [
        "system.handshake",
        "system.version",
        "project.status",
        "providers.status",
        "system.shutdown",
    ]
    assert output[3]["error"]["code"] == -32601
    assert output[4]["error"]["code"] == -32001
    assert output[5]["result"]["version"]


def test_v2_create_and_read_use_real_backend_and_schemas(initialized_root, wire):
    root = initialized_root
    assert workspace.projects_list()["projects"] == []
    requests = [
        _call("system.handshake", 1, protocol_version=2),
        _call("project.open", 2, root=root),
        _call("projects.list", 3, limit=10, offset=0),
        _call(
            "work.create",
            4,
            root=root,
            scope_level="CHANGE",
            objective="Durable goal",
            acceptance_criteria=["Verifiable criterion"],
            affected_modules=["unresolved-module"],
        ),
        _call("work.list", 5, root=root, scope_level="CHANGE"),
        _call("work.tree", 6, root=root),
        _call("project.summary", 7, root=root),
        _call("system.shutdown", 8),
    ]
    result_schemas = (
        "handshake-result",
        "project-summary-result",
        "projects-list-result",
        "work-item-result",
        "work-list-result",
        "work-tree-result",
        "project-summary-result",
        "shutdown-result",
    )
    messages = _transcript(requests)
    wire("notification", messages[0])
    wire("notification", messages[-1])
    for request, response, schema in zip(requests, messages[1:-1], result_schemas, strict=True):
        wire("request", request)
        wire("response", response)
        assert response["id"] == request["id"]
        wire(schema, response["result"])
    handshake = messages[1]["result"]
    assert {"projects.list", "project.summary", "work.create"} <= set(handshake["methods"])
    wire("handshake-result", {**handshake, "methods": handshake["methods"] + ["future.optional"]})
    assert messages[2]["result"]["initialized"]
    assert messages[3]["result"]["projects"][0]["root"] == root
    item = messages[4]["result"]
    assert item["affected_modules"][0]["module_ref"] is None
    assert messages[5]["result"]["items"] == [item]
    assert messages[6]["result"]["nodes"][0]["work_item_id"] == item["work_item_id"]
    summary = messages[7]["result"]
    assert summary["schema_version"] == 9
    assert summary["active_project"] is None
    assert summary["work_items"]["total"] == 1


def test_v2_import_operations_use_real_backend_and_strict_shapes(
    tmp_path, monkeypatch, wire, capsys
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user-state")
    root = tmp_path / "external"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout

    git("init")
    git("config", "core.autocrlf", "false")
    (root / "README.md").write_text("# Imported fixture\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "wire-fixture"\n', encoding="utf-8")
    (root / ".env").write_text("PASSWORD=fixture_secret\n", encoding="utf-8")
    git("add", "--all")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "External fixture",
    )
    before = git("status", "--porcelain")
    methods = {f"import.{action}" for action in ("inspect", "status", "commit", "refresh", "files")}
    legacy = _transcript(
        [
            _call("system.handshake", 1, protocol_version=1),
            _call("import.status", 2, root=str(root)),
        ]
    )
    assert methods.isdisjoint(legacy[1]["result"]["methods"])
    assert legacy[2]["error"]["code"] == -32601

    def transcript(*operations):
        handshake = _call("system.handshake", 1, protocol_version=2)
        replies = _transcript([handshake, *operations])
        assert methods <= set(replies[1]["result"]["methods"])
        wire("handshake-result", replies[1]["result"])
        for reply in replies[2:]:
            wire("response", reply)
        return replies[2:]

    def send(method, result_schema, **params):
        request = _call(method, 2, root=str(root), **params)
        wire("request", request)
        [reply] = transcript(request)
        result = reply["result"]
        wire(result_schema, result)
        return result

    missing = send("import.status", "import-status-result")
    assert missing["status"] == "NOT_IMPORTED" and missing["label"] is None
    observed = send("import.inspect", "import-inspect-result", limit=1)
    assert observed["label"] == "DISCOVERED" and observed["next_offset"] == 1
    assert not observed["manifest_facts_truncated"]
    assert main(["import", "inspect", str(root), "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == observed
    assert git("status", "--porcelain") == before and not (root / ".corinthos").exists()

    digest = observed["discovery_digest"]
    bad_calls = [
        (_call("import.inspect", 2, root=str(root), limit=26), -32602),
        (_call("import.status", 3, root=str(root), extra=True), -32602),
        (_call("import.commit", 4, root=str(root), discovery_digest="bad"), -32602),
        (_call("import.refresh", 5, root=str(root), discovery_digest=digest), -32602),
        (_call("import.files", 6, root=str(root), offset=-1), -32602),
    ]
    bad_notifications = [
        _call("import.commit", None, root=str(root), discovery_digest=digest),
        _call(
            "import.refresh",
            None,
            root=str(root),
            discovery_digest=digest,
            expected_snapshot_digest=digest,
        ),
    ]
    for request in [*(request for request, _ in bad_calls), *bad_notifications]:
        with pytest.raises(ValidationError):
            wire("request", request)
    replies = transcript(*bad_notifications, *(request for request, _ in bad_calls))
    assert [(row["id"], row["error"]["code"]) for row in replies] == [
        (request["id"], code) for request, code in bad_calls
    ]
    assert not (root / ".corinthos").exists()

    confirmations = [row["code"] for row in observed["ambiguities"] if row["required"]]
    committed = send(
        "import.commit",
        "import-status-result",
        discovery_digest=digest,
        confirmations=confirmations,
    )
    assert committed["status"] == "IMPORTED" and committed["label"] == "CONFIRMED", committed
    assert send("import.status", "import-status-result") == committed
    report = send("import.files", "import-files-result", limit=25)
    assert report["next_offset"] is None and len(report["files"]) == 3
    [sensitive] = [record for record in report["files"] if record["kind"] == "EXCLUDED"]
    assert sensitive["path"] is None and sensitive["index_oid"] is None
    assert send("import.files", "import-files-result", snapshot_digest=None) == {
        **report,
        "limit": 10,
    }
    with pytest.raises(ValidationError):
        wire("import-files-result", {**report, "files": [{**sensitive, "path": ".env"}]})
    with pytest.raises(ValidationError):
        wire("import-status-result", {**committed, "label": "CANONICAL"})
    with pytest.raises(ValidationError):
        wire("import-inspect-result", {**observed, "unexpected": 1})
    reread = send("import.inspect", "import-inspect-result")
    refreshed = send(
        "import.refresh",
        "import-status-result",
        discovery_digest=reread["discovery_digest"],
        expected_snapshot_digest=committed["snapshot_digest"],
        confirmations=confirmations,
    )
    assert refreshed == committed


def test_import_preview_stays_within_wire_limit_with_large_manifest_facts(monkeypatch, wire):
    from corinthos.external_import.service import ExternalProjectImportService

    root = "/" + "x" * 4095
    digest = "a" * 64
    paths = [f"{index:02d}/" + "日" * 160 + "/path" for index in range(30)]
    files = [
        {
            "path": path,
            "kind": "SOURCE",
            "index_oid": "a" * 40,
            "index_mode": "100644",
            "worktree_sha256": digest,
            "state": "CLEAN",
            "size": 0,
            "content_excluded": False,
        }
        for path in paths
    ]
    manifests = [
        {
            "path": path,
            "ecosystem": "node",
            "name": "fixture",
            "parsed": True,
            "dependencies": [f"{index:02d}" + "a" * 90 for index in range(16)],
            "scripts": [f"{index:02d}" + "s" * 70 for index in range(16)],
            "members": paths[:16],
        }
        for path in paths[:16]
    ]
    ambiguity = {
        "code": "DIRTY_TRACKED",
        "explanation": "Review the exact tracked changes.",
        "required": True,
        "options": ["CONFIRM"],
    }
    observation = {
        "root": root,
        "label": "DISCOVERED",
        "policy_version": 1,
        "head": "a" * 40,
        "branch": "main",
        "branch_digest": digest,
        "dirty": True,
        "tracked_count": len(files),
        "staged_count": 0,
        "index_digest": digest,
        "untracked_count": 0,
        "untracked_digest": digest,
        "files": files,
        "manifests": manifests,
        "docs": paths[:16],
        "tests": paths[:16],
        "languages": {"Python": len(files)},
        "candidates": [{"path": path, "ecosystem": "node"} for path in paths[:16]],
        "exclusions": [],
        "ambiguities": [ambiguity],
        "commit_allowed": True,
        "existing_state": False,
        "discovery_digest": digest,
    }
    monkeypatch.setattr(
        ExternalProjectImportService, "inspect", lambda self, root: dict(observation)
    )
    found, offset, previews = [], 0, []
    while offset is not None:
        reply = _transcript(
            [
                _call("system.handshake", 1, protocol_version=2),
                _call("import.inspect", "⭐" * 128, root=root, limit=25, offset=offset),
            ]
        )[2]
        result = reply["result"]
        wire("import-inspect-result", result)
        assert result["manifest_facts_truncated"]
        assert result["discovery_digest"] == digest and result["ambiguities"] == [ambiguity]
        assert [item["path"] for item in result["manifests"]] == paths[:16]
        assert result["docs"] == result["tests"] == paths[:16]
        assert result["files"] and (result["next_offset"] is None or result["next_offset"] > offset)
        found.extend(result["files"])
        previews.append(result["manifests"])
        offset = result["next_offset"]
    assert found == files and len(previews) > 1
    assert all(page == previews[0] for page in previews)


def test_large_unicode_pages_continue_without_losing_items(monkeypatch):
    @dataclass
    class RegistryRow:
        root: str

    records = [RegistryRow(str(index) + "é😀" * 2000) for index in range(15)]

    class Registry:
        def list(self, *, limit, offset):
            return records[offset : offset + limit]

    monkeypatch.setattr("corinthos.project_registry.ProjectRegistryService", Registry)
    seen = []
    offset = 0
    while offset is not None:
        _, _, response = _transcript(
            [
                _call("system.handshake", "h", protocol_version=2),
                _call("projects.list", "😀" * 128, limit=10, offset=offset),
            ]
        )
        result = response["result"]
        seen.extend(item["root"] for item in result["projects"])
        offset = result["next_offset"]
        assert offset is None or offset == len(seen)
    assert seen == [row.root for row in records]


def test_v2_status_update_dependency_reads_and_failures(initialized_root, wire):
    root = initialized_root
    fields = {
        "root": root,
        "scope_level": "TASK",
        "objective": "Goal",
        "acceptance_criteria": ["Proof"],
    }
    one = workspace.work_create(**fields)
    two = workspace.work_create(**fields, depends_on=[one["work_item_id"]])
    key = {"root": root, "work_item_id": two["work_item_id"]}
    mutation = _call("work.create", None, **fields)
    with pytest.raises(ValidationError):
        wire("request", mutation)
    requests = [
        _call("system.handshake", "h", protocol_version=2),
        mutation,
        _call("work.get", "get", **key),
        _call("work.dependencies", "deps", **key),
        _call("work.status", "move", **key, status="ACTIVE", expected_updated_at=two["updated_at"]),
        _call("work.update", "update", **key, objective="New objective"),
        _call(
            "work.status", "stale", **key, status="BLOCKED", expected_updated_at=two["updated_at"]
        ),
        _call("work.list", "bad", root=root, limit=True),
        _call("project.open", "missing", root=root + "-missing"),
        _call("work.status", "bad-status", **key, status={"secret": "not echoed"}),
    ]
    messages = _transcript(requests)
    get, deps, status, updated, stale, bad, missing, bad_status = messages[2:]
    for response in messages[1:]:
        wire("response", response)
    for response in (get, status, updated):
        wire("work-item-result", response["result"])
    wire("work-dependencies-result", deps["result"])
    assert deps["result"]["dependency_ids"] == [one["work_item_id"]]
    assert status["result"]["status"] == "ACTIVE"
    assert updated["result"]["objective"] == "New objective"
    assert stale["error"]["code"] in {-32002, -32602}
    assert [bad["error"]["code"], missing["error"]["code"], bad_status["error"]["code"]] == [
        -32602,
        -32002,
        -32602,
    ]
    assert "not echoed" not in json.dumps(messages)
    assert workspace.work_list(root)["items"][0]
    assert len(workspace.work_list(root)["items"]) == 2


def test_cli_operations_match_application_without_passive_writes(initialized_root, capsys):
    root = initialized_root

    def cli(*args):
        assert main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    assert cli("projects", "list") == workspace.projects_list()
    assert cli("project", "summary", root) == workspace.project_summary(root)
    assert not (Path(root) / ".corinthos/state.db").exists()
    assert cli("project", "--root", root, "open") == workspace.project_summary(root)
    item = cli(
        "work",
        "create",
        "--root",
        root,
        "--scope",
        "CRITICAL",
        "--objective",
        "Operational record",
        "--criterion",
        "Keep this exact",
    )
    key = item["work_item_id"]
    assert cli("work", "show", key, "--root", root) == item
    assert cli("work", "list", "--root", root, "--scope", "CRITICAL")["items"] == [item]
    assert cli("work", "status", key, "ACTIVE", "--root", root)["status"] == "ACTIVE"
    assert cli("work", "tree", "--root", root)["nodes"][0]["work_item_id"] == key
    assert cli("work", "dependencies", key, "--root", root)["dependency_ids"] == []
    assert cli("work", "update", key, "--root", root, "--clear-modules")["affected_modules"] == []
    assert ContractStore(root).list_events() == []
