# SPDX-License-Identifier: MPL-2.0
"""Keep desktop decisions exact and native provider operations explicit."""

import json

import pytest
from jsonschema import ValidationError
from test_application_workspace import _call, _transcript
from test_application_workspace import wire as wire
from test_project_manager import _edit, _ready
from test_workflow_authorization import _ready as _ready_workflow

from corinthos.application import command, reviews, settings
from corinthos.contracts import ConflictError
from corinthos.project_manager.approval import local_actor_id
from corinthos.project_manager.service import ProjectManager


@pytest.mark.parametrize("kind", ["PROJECT", "WORKFLOW"])
def test_frozen_decision_is_passive_and_can_only_be_deferred_by_exact_identity(
    tmp_path, wire, kind
):
    if kind == "PROJECT":
        owner = ProjectManager(tmp_path)
        draft = _ready(owner)
    else:
        owner, draft = _ready_workflow(tmp_path)
    session_id = draft["session_id"]
    snapshot = owner.review(session_id, draft["version"], actor_id=local_actor_id())
    database = tmp_path / ".corinthos/state.db"
    initial = database.read_bytes()
    args = dict(root=str(tmp_path), kind=kind, session_id=session_id)
    listing = _call("review.list", 2, root=str(tmp_path))
    lookup = _call("review.get", 3, **args)
    [_, _, listed, found] = _transcript(
        [_call("system.handshake", 1, protocol_version=2), listing, lookup]
    )
    for request, result, schema in (
        (listing, listed, "review-list-result"),
        (lookup, found, "review-get-result"),
    ):
        wire("request", request)
        wire(schema, result["result"])
    detail = found["result"]
    assert detail["review"] in listed["result"]["reviews"]
    digest = snapshot["review"]["sha256"]
    assert detail["review"]["digest"] == digest
    assert digest in detail["presentation"] and "Complete candidate" in detail["presentation"]
    assert detail["approve_command"].endswith(session_id) and detail["can_defer"]
    card = command.command_project(str(tmp_path))
    wire("command-project-result", card)
    assert any(
        entry["owner_type"] == kind + "_PLANNING" and entry["owner_id"] == session_id
        for entry in card["attention"]
    )
    assert database.read_bytes() == initial
    with pytest.raises(ConflictError, match="changed"):
        reviews.review_defer(**args, version=snapshot["version"], digest="0" * 64)
    mutation = _call("review.defer", None, **args, version=snapshot["version"], digest=digest)
    with pytest.raises(ValidationError):
        wire("request", mutation)
    messages = _transcript([_call("system.handshake", 1, protocol_version=2), mutation])
    assert len(messages) == 2 and database.read_bytes() == initial
    mutation["id"] = 4
    wire("request", mutation)
    [_, _, response] = _transcript([_call("system.handshake", 1, protocol_version=2), mutation])
    wire("review-summary", response["result"])
    assert response["result"]["state"] == "DEFERRED"
    assert owner.get(session_id)["review"] is None
    assert not reviews.review_list(tmp_path)["reviews"]


def test_saved_provider_reads_and_notification_mutations_never_probe(tmp_path, monkeypatch, wire):
    saved = tmp_path / "user"
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: saved)
    called = []
    monkeypatch.setattr(settings, "refresh_providers", lambda: called.append(("refresh",)))
    monkeypatch.setattr(settings, "configure_provider", lambda *args: called.append(args))
    methods = [
        _call("providers.refresh", None),
        _call("provider.configure", None, provider="codex", executable=str(tmp_path / "codex.exe")),
        _call("provider.reset", None, provider="claude"),
    ]
    messages = _transcript(
        [_call("system.handshake", 1, protocol_version=2), _call("settings.providers", 2), *methods]
    )
    assert not called and not saved.exists()
    wire("settings-providers-result", messages[-1]["result"])
    assert {row["provider"] for row in messages[-1]["result"] if row["supported"]} == {
        "codex",
        "claude",
    }
    for index, request in enumerate(methods, 3):
        with pytest.raises(ValidationError):
            wire("request", request)
        request["id"] = index
        wire("request", request)
    messages = _transcript([_call("system.handshake", 1, protocol_version=2), *methods])
    assert len(called) == 3 and called[-1] == ("claude", None)
    for response in messages[2:]:
        wire("settings-providers-result", response["result"])
    assert "approve" not in json.dumps(messages[1]["result"]["methods"])


def test_sensitive_exact_review_uses_the_foreground_terminal_instead(tmp_path, wire):
    owner = ProjectManager(tmp_path)
    draft = _edit(
        owner,
        _ready(owner),
        action="set_scope",
        section="included",
        items=["Inventory includes password=synthetic-sensitive-review-value"],
    )
    version = owner.review(draft["session_id"], draft["version"], actor_id=local_actor_id())
    detail = reviews.review_get(str(tmp_path), "PROJECT", version["session_id"])
    wire("review-get-result", detail)
    assert detail["review"]["digest"] == version["review"]["sha256"]
    assert detail["presentation"] is None and detail["redactions"]
    assert "synthetic-sensitive-review-value" not in json.dumps(detail)
