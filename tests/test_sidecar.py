# SPDX-License-Identifier: MPL-2.0
"""Current wire bounds, native core handshake and shared project operations."""

import io
import json

import pytest

from corinthos.application import sidecar


def _call(method, request_id, **params):
    value = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        value["id"] = request_id
    return value


def _transcript(requests, *, health=False):
    source = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
    output, diagnostics = io.BytesIO(), io.StringIO()
    sidecar.serve(source, output, diagnostics, health=health)
    assert not diagnostics.getvalue()
    assert all(len(line) <= sidecar.MAX_FRAME_BYTES for line in output.getvalue().splitlines())
    return [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.fixture
def wire():
    def validate(name, value):
        assert name == "update-result"
        assert set(value) == {
            "operation_id",
            "installed_version",
            "candidate_version",
            "phase",
            "progress",
            "message",
            "error",
            "can_start",
            "can_finish",
        }
        assert type(value["progress"]) is int and 0 <= value["progress"] <= 100
        assert type(value["can_start"]) is bool and type(value["can_finish"]) is bool
        assert value["error"] is None or set(value["error"]) == {"code", "message"}

    return validate


def test_core_native_handshake_and_new_project_identity(tmp_path):
    root = str(tmp_path / "project")
    messages = _transcript(
        [
            _call("system.handshake", 1, protocol_version=2),
            _call("project.init", 2, root=root),
            _call("project.init", 3, root=root),
            _call("project.status", 4, root=root),
            _call("providers.status", 5),
            _call("work.create", 6),
            _call("system.shutdown", 7),
        ]
    )
    methods = messages[1]["result"]["methods"]
    assert {
        "system.handshake",
        "system.version",
        "project.status",
        "providers.status",
        "system.shutdown",
        "skill.run",
        "update.finish",
    } <= set(methods)
    assert messages[2]["result"] == messages[3]["result"]
    assert messages[4]["result"]["workspace_format"] == 1
    assert messages[5]["result"] == {"providers": []}
    assert messages[6]["error"]["code"] == -32601
    assert messages[-1]["method"] == "system.stopping"


def test_wire_rejects_duplicate_keys_oversize_and_prehandshake():
    for frame, code in [
        (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"system.version"}\n', -32700),
        (b"x" * 70000 + b"\n", -32600),
    ]:
        output = io.BytesIO()
        sidecar.serve(io.BytesIO(frame), output, io.StringIO(), health=True)
        assert json.loads(output.getvalue().splitlines()[1])["error"]["code"] == code
    assert _transcript([_call("system.version", 1)])[1]["error"]["code"] == -32001
