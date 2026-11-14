# SPDX-License-Identifier: MPL-2.0
"""Bounded JSON-RPC 2.0 over UTF-8 JSON Lines on standard streams."""

import json
import sys
from dataclasses import asdict
from typing import BinaryIO, TextIO

from corinthos.application import (
    adaptive,
    context,
    external_import,
    project_status,
    providers_status,
    version,
    workspace,
)
from corinthos.application.sessions import SessionApplication
from corinthos.contracts import ContractError

BOOTSTRAP_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS = (1, 2)
MAX_FRAME_BYTES = 65_536
_MAX_ID = 2**53 - 1
_V1_METHODS = (
    "system.handshake",
    "system.version",
    "project.status",
    "providers.status",
    "system.shutdown",
)
_PAGE_OPTIONS = ("limit", "offset")
_WORK_CONTENT = (
    "objective",
    "acceptance_criteria",
    "parent_work_item_id",
    "validation_policy",
    "affected_modules",
    "depends_on",
)
_V2_OPERATIONS = {
    "context.create": (context.context_create, ("root", "work_item_id", "provider"), ()),
    "context.get": (context.context_get, ("root", "projection_id"), ()),
    "context.list": (context.context_list, ("root",), ("work_item_id", *_PAGE_OPTIONS)),
    "context.sources": (context.context_sources, ("root", "projection_id"), _PAGE_OPTIONS),
    "context.verify": (context.context_verify, ("root", "projection_id"), ()),
    "context.for_session": (context.context_for_session, ("root", "session_id"), ()),
    "handoff.create": (context.handoff_create, ("root", "session_id"), ()),
    "handoff.get": (context.handoff_get, ("root", "handoff_id"), ()),
    "handoff.list": (
        context.handoff_list,
        ("root",),
        ("work_item_id", "session_id", *_PAGE_OPTIONS),
    ),
    "projects.list": (workspace.projects_list, (), _PAGE_OPTIONS),
    "project.summary": (workspace.project_summary, ("root",), ()),
    "project.open": (workspace.project_open, ("root",), ()),
    "work.create": (
        workspace.work_create,
        ("root", "scope_level", "objective", "acceptance_criteria"),
        (*_WORK_CONTENT[2:], "project_id"),
    ),
    "work.get": (workspace.work_get, ("root", "work_item_id"), ()),
    "work.list": (workspace.work_list, ("root",), ("scope_level", "status", *_PAGE_OPTIONS)),
    "work.update": (
        workspace.work_update,
        ("root", "work_item_id"),
        (*_WORK_CONTENT, "expected_updated_at"),
    ),
    "work.status": (
        workspace.work_status,
        ("root", "work_item_id", "status"),
        ("expected_updated_at",),
    ),
    "work.tree": (workspace.work_tree, ("root",), ("parent_work_item_id", *_PAGE_OPTIONS)),
    "work.dependencies": (
        workspace.work_dependencies,
        ("root", "work_item_id"),
        _PAGE_OPTIONS,
    ),
    "scope.assess": (adaptive.scope_assess, ("root", "work_item_id"), ()),
    "scope.set": (
        adaptive.scope_set,
        ("root", "work_item_id", "scope_level", "expected_updated_at"),
        ("confirm_downgrade",),
    ),
    "flow.status": (adaptive.flow_status, ("root", "work_item_id"), ()),
    "flow.complete": (adaptive.flow_complete, ("root", "work_item_id", "expected_updated_at"), ()),
    "work.report": (adaptive.work_report, ("root", "work_item_id"), ()),
    "import.inspect": (external_import.import_inspect, ("root",), _PAGE_OPTIONS),
    "import.commit": (
        external_import.import_commit,
        ("root", "discovery_digest"),
        ("confirmations",),
    ),
    "import.status": (external_import.import_status, ("root",), ()),
    "import.refresh": (
        external_import.import_refresh,
        ("root", "expected_snapshot_digest", "discovery_digest"),
        ("confirmations",),
    ),
    "import.files": (external_import.import_files, ("root",), ("snapshot_digest", *_PAGE_OPTIONS)),
}
_SESSION_OPERATIONS = {
    "session.start": (
        "start",
        ("root", "work_item_id", "provider"),
        ("instruction", "timeout_seconds", "read_only"),
    ),
    "session.get": ("get", ("root", "session_id"), ()),
    "session.list": ("list", ("root",), _PAGE_OPTIONS),
    "session.events": ("events", ("root", "session_id"), ("limit", "after_sequence")),
    "session.cancel": ("cancel", ("root", "session_id"), ("generation",)),
    "session.recover": ("recover", ("root", "session_id"), ("instruction",)),
    "switch.status": (
        "switch_status",
        ("root", "source_session_id", "target_provider"),
        ("source_generation",),
    ),
    "switch.execute": (
        "switch_execute",
        ("root", "source_session_id", "target_provider"),
        ("source_generation",),
    ),
    "flow.start": (
        "flow_start",
        ("root", "work_item_id", "provider"),
        ("instruction", "timeout_seconds", "read_only"),
    ),
}
_MUTATIONS = {
    "context.create",
    "handoff.create",
    "project.open",
    "work.create",
    "work.update",
    "work.status",
    "session.start",
    "session.cancel",
    "session.recover",
    "switch.execute",
    "scope.set",
    "flow.start",
    "flow.complete",
    "import.commit",
    "import.refresh",
}
_ERROR_MESSAGES = {
    -32700: "Parse error",
    -32600: "Invalid Request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
    -32001: "Compatible protocol handshake required",
    -32002: "Requested state unavailable",
}


class _RpcError(Exception):
    def __init__(self, code: int):
        self.code = code


def _json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate keys.")
    return result


def _reject_constant(_value):
    raise ValueError("Non-JSON constant.")


def _request_id(value):
    return (type(value) is int and -_MAX_ID <= value <= _MAX_ID) or (
        isinstance(value, str)
        and len(value) <= 128
        and all(not "\ud800" <= char <= "\udfff" for char in value)
    )


def _parse(frame: bytes) -> dict:
    try:
        message = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise _RpcError(-32700) from None
    if (
        not isinstance(message, dict)
        or message.keys() - {"jsonrpc", "id", "method", "params"}
        or message.get("jsonrpc") != "2.0"
        or not isinstance(message.get("method"), str)
        or not 0 < len(message["method"]) <= 64
        or ("id" in message and not _request_id(message["id"]))
    ):
        raise _RpcError(-32600)
    return message


def _params(message: dict, *, required=(), optional=()) -> dict:
    params = message.get("params", {})
    if (
        not isinstance(params, dict)
        or not set(required) <= params.keys()
        or params.keys() - set(required) - set(optional)
    ):
        raise _RpcError(-32602)
    return params


def _dispatch(
    message: dict, negotiated: int | None, sessions: SessionApplication
) -> tuple[dict, int | None, bool]:
    method = message["method"]
    if method == "system.handshake":
        received = _params(message, required=("protocol_version",))["protocol_version"]
        if type(received) is not int:
            raise _RpcError(-32602)
        if received not in SUPPORTED_PROTOCOLS or (
            negotiated is not None and negotiated != received
        ):
            raise _RpcError(-32001)
        return (
            {
                "protocol_version": received,
                "version": version().version,
                "methods": [*_V1_METHODS, *_V2_OPERATIONS, *_SESSION_OPERATIONS]
                if received == 2
                else list(_V1_METHODS),
            },
            received if "id" in message else negotiated,
            False,
        )
    if method in _V2_OPERATIONS or method in _SESSION_OPERATIONS:
        if negotiated is None:
            raise _RpcError(-32001)
        if negotiated != 2:
            raise _RpcError(-32601)
        if method in _MUTATIONS and "id" not in message:
            raise _RpcError(-32600)
        if method in _SESSION_OPERATIONS:
            action, required, optional = _SESSION_OPERATIONS[method]
            operation = getattr(sessions, action)
        else:
            operation, required, optional = _V2_OPERATIONS[method]
        params = _params(message, required=required, optional=optional)
        if "root" in params and not isinstance(params["root"], str):
            raise _RpcError(-32602)
        from corinthos.sessions.model import SessionError
        from corinthos.work import WorkError

        try:
            return operation(**params), negotiated, False
        except (SessionError, WorkError):
            raise _RpcError(-32602) from None
        except ContractError:
            raise _RpcError(-32002) from None
        except ValueError:
            raise _RpcError(-32602) from None
    if method not in _V1_METHODS:
        raise _RpcError(-32601)
    if negotiated is None:
        raise _RpcError(-32001)
    if method in {"project.status", "providers.status"}:
        params = (
            _params(message, required=("root",))
            if method == "project.status"
            else _params(message, optional=("root",))
        )
        root = params.get("root")
        if "root" in params and not isinstance(root, str):
            raise _RpcError(-32602)
        try:
            if method == "project.status":
                return asdict(project_status(root)), negotiated, False
            providers = [asdict(provider) for provider in providers_status(root)]
            return {"providers": providers}, negotiated, False
        except ContractError:
            raise _RpcError(-32002) from None
        except ValueError:
            raise _RpcError(-32602) from None
    _params(message)
    if method == "system.version":
        return asdict(version()), negotiated, False
    return {"stopping": True}, negotiated, "id" in message


def _error(code: int, request_id=None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": _ERROR_MESSAGES[code]},
    }


def _write(output: BinaryIO, message: dict, *, ascii_only: bool = True) -> None:
    serialized = json.dumps(message, separators=(",", ":"), ensure_ascii=ascii_only)
    try:
        data = serialized.encode("utf-8")
    except UnicodeEncodeError:
        data = b""
    if not data or len(data) > MAX_FRAME_BYTES:
        data = json.dumps(_error(-32002, message.get("id")), separators=(",", ":")).encode()
    output.write(data + b"\n")
    output.flush()


def _notify(output: BinaryIO, method: str, params: dict) -> None:
    _write(output, {"jsonrpc": "2.0", "method": method, "params": params})


def serve(source: BinaryIO, output: BinaryIO, diagnostics: TextIO) -> None:
    """Serve one local peer until EOF or an identified shutdown request."""
    sessions = SessionApplication()
    try:
        _serve(source, output, diagnostics, sessions)
    finally:
        sessions.supervisor.close()


def _serve(source, output, diagnostics, sessions):
    # The frozen v1 ready event precedes explicit negotiation on every connection.
    _notify(output, "system.ready", {"protocol_version": BOOTSTRAP_PROTOCOL_VERSION})
    negotiated = None
    while frame := source.readline(MAX_FRAME_BYTES + 2):
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            _write(output, _error(-32600))
            # Discard the rest of an oversized frame without growing memory.
            # A peer that keeps streaming a single huge line loses the connection.
            for _ in range(16):
                if not frame or frame.endswith(b"\n"):
                    break
                frame = source.readline(MAX_FRAME_BYTES + 2)
            else:
                if frame and not frame.endswith(b"\n"):
                    return
            continue
        try:
            message = _parse(frame)
        except _RpcError as error:
            _write(output, _error(error.code))
            continue
        try:
            result, negotiated, stopping = _dispatch(message, negotiated, sessions)
            if stopping and not sessions.supervisor.close():
                print("Session cleanup exceeded the shutdown deadline.", file=diagnostics)
            reply = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        except _RpcError as error:
            reply = _error(error.code, message.get("id"))
            stopping = False
        except (ContractError, OSError, RuntimeError):
            reply = _error(-32002, message.get("id"))
            stopping = False
        except Exception:
            print("Unexpected application operation failure.", file=diagnostics)
            reply = _error(-32603, message.get("id"))
            stopping = False
        if "id" in message:
            _write(output, reply, ascii_only=negotiated != 2)
        if stopping:
            _notify(output, "system.stopping", {})
            return


def main() -> int:
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, sys.stderr)
    except BrokenPipeError:
        return 0
    except OSError:
        print("Sidecar transport failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
