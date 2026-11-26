# SPDX-License-Identifier: MPL-2.0
"""Bounded JSON-RPC over UTF-8 JSON Lines; health constructs no project runtime."""

import json
import sys
from contextlib import nullcontext
from typing import BinaryIO

from corinthos import __version__
from corinthos.errors import ConflictError, IntegrityError

BOOTSTRAP_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS = (1, 2)
MAX_FRAME_BYTES = 65536
_MAX_ID = 2**53 - 1
_CORE = (
    "system.handshake",
    "system.version",
    "project.status",
    "providers.status",
    "system.shutdown",
)
_HEALTH = ("system.handshake", "system.version", "system.shutdown")
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
    def __init__(self, code, creation_code=None):
        self.code = code
        self.creation_code = creation_code


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


def _dispatch(message, negotiated, *, health=False):
    method = message["method"]
    if health and method not in _HEALTH:
        raise _RpcError(-32601)
    if method == "system.handshake":
        received = _params(message, required=("protocol_version",))["protocol_version"]
        if type(received) is not int:
            raise _RpcError(-32602)
        if received not in SUPPORTED_PROTOCOLS or (
            negotiated is not None and received != negotiated
        ):
            raise _RpcError(-32001)
        methods = list(_HEALTH if health else _CORE)
        if not health and received == 2:
            from corinthos.application.service import OPERATIONS

            methods = list(dict.fromkeys([*methods, *OPERATIONS]))
        return (
            {"protocol_version": received, "version": __version__, "methods": methods},
            received if "id" in message else negotiated,
            False,
        )
    if negotiated is None:
        raise _RpcError(-32001)
    if method == "system.version":
        _params(message)
        return {"version": __version__}, negotiated, False
    if method == "system.shutdown":
        _params(message)
        return {"stopping": True}, negotiated, "id" in message
    from corinthos.application.service import OPERATIONS, invoke

    if method not in OPERATIONS or (negotiated == 1 and method not in _CORE):
        raise _RpcError(-32601)
    if "id" not in message and method not in {
        "project.status",
        "providers.status",
        "projects.list",
        "skill.list",
        "skill.show",
        "run.get",
        "run.list",
        "continuity.show",
        "update.status",
        "system.diagnostics",
    }:
        raise _RpcError(-32600)
    try:
        return invoke(method, message.get("params", {})), negotiated, False
    except (IntegrityError, ValueError, TypeError):
        raise _RpcError(-32602) from None
    except (ConflictError, OSError, RuntimeError):
        raise _RpcError(-32002) from None


def _error(code: int, request_id=None, creation_code: str | None = None) -> dict:
    reply = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": _ERROR_MESSAGES[code]},
    }
    if creation_code is not None:
        reply["error"]["data"] = {"creation_code": creation_code}
    return reply


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


def serve(source, output, diagnostics, *, health=False):
    from corinthos.runtime_gate import client_runtime

    with nullcontext() if health else client_runtime():
        _serve(source, output, diagnostics, health=health)


def _serve(source, output, diagnostics, *, health=False):
    _notify(output, "system.ready", {"protocol_version": BOOTSTRAP_PROTOCOL_VERSION})
    negotiated = None
    while frame := source.readline(MAX_FRAME_BYTES + 2):
        if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
            _write(output, _error(-32600))
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
            result, negotiated, stopping = _dispatch(message, negotiated, health=health)
            reply = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        except _RpcError as error:
            reply, stopping = _error(error.code, message.get("id")), False
        except Exception:
            print("Unexpected application operation failure.", file=diagnostics)
            reply, stopping = _error(-32603, message.get("id")), False
        if "id" in message:
            _write(output, reply, ascii_only=negotiated != 2)
        if stopping:
            _notify(output, "system.stopping", {})
            return


def main():
    if sys.argv[1:] not in ([], ["--installation-check"]):
        print("Unknown sidecar mode.", file=sys.stderr)
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, sys.stderr, health=bool(sys.argv[1:]))
    except BrokenPipeError:
        return 0
    except (OSError, RuntimeError):
        print("Sidecar transport failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
