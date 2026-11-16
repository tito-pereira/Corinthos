# SPDX-License-Identifier: MPL-2.0
"""Keep creation identified, strictly bounded and retryable over the existing v2 transport."""

import pytest
from jsonschema import ValidationError
from test_application_workspace import _call, _transcript
from test_application_workspace import wire as wire

from corinthos.application import sidecar
from corinthos.contracts import ContractError


def test_creation_round_trip_reuses_summary_and_recovers_matching_retry(
    tmp_path, monkeypatch, wire
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    root = tmp_path / "existing ordinary directory"
    root.mkdir()
    owner_file = root / "AGENTS.md"
    owner_file.write_bytes(b"Owner instructions\x00\xff\r\n")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Creation dispatched provider or publication work")

    for method in ("invoke", "probe", "discover"):
        monkeypatch.setattr(f"corinthos.adapters.service.AdapterManager.{method}", forbidden)
    monkeypatch.setattr("corinthos.contracts.ContractStore.publish", forbidden)
    requests = [
        _call("system.handshake", 1, protocol_version=2),
        _call("project.create", 2, root=str(root), display_name="Café project"),
        _call("project.create", 3, root=str(root), display_name="Café project"),
        _call("projects.list", 4),
        _call("project.summary", 5, root=str(root)),
        _call("project.create", 6, root=str(root), display_name="Conflicting name"),
    ]
    messages = _transcript(requests)
    for request, response in zip(requests, messages[1:], strict=True):
        wire("request", request)
        wire("response", response)
    for message in (messages[2], messages[3], messages[5]):
        summary = message["result"]
        wire("project-summary-result", summary)
        assert summary["display_name"] == "Café project"
        assert summary["initialized"] and summary["storage"] == "ABSENT"
        assert summary["active_project"] is summary["active_workflow"] is None
    assert len(messages[4]["result"]["projects"]) == 1
    assert messages[6]["error"]["data"] == {"creation_code": "CONFLICT"}
    assert owner_file.read_bytes() == b"Owner instructions\x00\xff\r\n"
    assert not (root / ".corinthos/state.db").exists()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"root": "/project"},
        {"root": "/project", "display_name": "Name", "extra": True},
        {"root": 1, "display_name": "Name"},
        {"root": "/project", "display_name": 1},
        {"root": "", "display_name": "Name"},
        {"root": "x" * 4097, "display_name": "Name"},
        {"root": "/project", "display_name": ""},
        {"root": "/project", "display_name": "   "},
        {"root": "/project", "display_name": "x" * 129},
    ],
)
def test_creation_rejects_invalid_params_before_dispatch(params, monkeypatch, wire):
    def mutation(**_params):
        pytest.fail("Invalid creation reached project mutation")

    monkeypatch.setitem(
        sidecar._V2_OPERATIONS, "project.create", (mutation, ("root", "display_name"), ())
    )
    request = _call("project.create", 2, **params)
    with pytest.raises(ValidationError):
        wire("request", request)
    messages = _transcript([_call("system.handshake", 1, protocol_version=2), request])
    response = messages[-1]
    wire("response", response)
    wire("error", response["error"])
    assert response["error"]["code"] == -32602
    assert response["error"]["data"] == {"creation_code": "INVALID_INPUT"}


def test_creation_requires_identified_v2_negotiation(monkeypatch, wire):
    calls = []

    def mutation(**params):
        calls.append(params)
        return {}

    monkeypatch.setitem(
        sidecar._V2_OPERATIONS, "project.create", (mutation, ("root", "display_name"), ())
    )
    params = {"root": "/project", "display_name": "Name"}
    request = _call("project.create", 1, **params)
    assert _transcript([request])[-1]["error"]["code"] == -32001
    v1 = _transcript([_call("system.handshake", 2, protocol_version=1), request])
    assert "project.create" not in v1[1]["result"]["methods"]
    assert v1[-1]["error"]["code"] == -32601
    notification = _call("project.create", None, **params)
    with pytest.raises(ValidationError):
        wire("request", notification)
    v2 = _transcript([_call("system.handshake", 3, protocol_version=2), notification])
    assert len(v2) == 2
    assert "project.create" in v2[1]["result"]["methods"]
    assert not calls


@pytest.mark.parametrize(
    "creation_code", ["CONFLICT", "ACCESS_DENIED", "REGISTRY_INCOMPLETE", "STATE_UNAVAILABLE"]
)
def test_creation_errors_expose_only_bounded_recovery_codes(creation_code, monkeypatch, wire):
    class Failure(ContractError):
        pass

    failure = Failure("private project or registry diagnostic")
    failure.creation_code = creation_code

    def mutation(**_params):
        raise failure

    monkeypatch.setitem(
        sidecar._V2_OPERATIONS, "project.create", (mutation, ("root", "display_name"), ())
    )
    response = _transcript(
        [
            _call("system.handshake", 1, protocol_version=2),
            _call("project.create", 2, root="/project", display_name="Name"),
        ]
    )[-1]
    wire("response", response)
    wire("error", response["error"])
    assert response["error"] == {
        "code": -32002,
        "message": "Requested state unavailable",
        "data": {"creation_code": creation_code},
    }
