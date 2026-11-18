# SPDX-License-Identifier: MPL-2.0
"""Keep runtime diagnostics passive, bounded and negotiated over current RPC."""

import platform

import pytest
from jsonschema import ValidationError
from test_application_workspace import _call, _transcript
from test_application_workspace import wire as wire

from corinthos.contracts import ContractStore


def test_diagnostics_reads_running_values_without_project_or_user_state(
    tmp_path, monkeypatch, wire
):
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: tmp_path / "user")
    monkeypatch.chdir(tmp_path)
    owner = tmp_path / "owner.txt"
    owner.write_bytes(b"Owner bytes\x00\xff\r\n")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Diagnostics attempted mutation or provider work")

    monkeypatch.setattr(ContractStore, "__init__", forbidden)
    monkeypatch.setattr(ContractStore, "publish", forbidden)
    monkeypatch.setattr("corinthos.project_registry.ProjectRegistryService.remember", forbidden)
    for method in ("invoke", "probe", "discover"):
        monkeypatch.setattr(f"corinthos.adapters.service.AdapterManager.{method}", forbidden)
    requests = [
        _call("system.handshake", 1, protocol_version=2),
        _call("system.diagnostics", 2),
        _call("system.diagnostics", 3),
    ]
    messages = _transcript(requests)
    assert messages[0] == {
        "jsonrpc": "2.0",
        "method": "system.ready",
        "params": {"protocol_version": 1},
    }
    assert "system.diagnostics" in messages[1]["result"]["methods"]
    expected = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "supported_schema_version": ContractStore.current_schema_version(),
    }
    for request, response in zip(requests[1:], messages[2:], strict=True):
        wire("request", request)
        wire("response", response)
        wire("system-diagnostics-result", response["result"])
        assert response["result"] == expected
    assert list(tmp_path.iterdir()) == [owner]
    assert owner.read_bytes() == b"Owner bytes\x00\xff\r\n"


def test_diagnostics_requires_v2_negotiation():
    unnegotiated = _transcript([_call("system.diagnostics", 1)])
    assert unnegotiated[1]["error"]["code"] == -32001
    messages = _transcript(
        [
            _call("system.handshake", 1, protocol_version=1),
            _call("system.diagnostics", 2),
        ]
    )
    assert "system.diagnostics" not in messages[1]["result"]["methods"]
    assert messages[2]["error"]["code"] == -32601


def test_diagnostics_rejects_parameters_and_extra_result_fields(wire):
    request = _call("system.diagnostics", 2, root="/unused")
    with pytest.raises(ValidationError):
        wire("request", request)
    messages = _transcript([_call("system.handshake", 1, protocol_version=2), request])
    assert messages[2]["error"]["code"] == -32602
    with pytest.raises(ValidationError):
        wire(
            "system-diagnostics-result",
            {
                "python_implementation": "CPython",
                "python_version": "3.12.14",
                "supported_schema_version": 9,
                "private_path": "/unused",
            },
        )
