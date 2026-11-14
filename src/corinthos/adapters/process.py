# SPDX-License-Identifier: MPL-2.0
"""Bounded subprocess transport with observable, non-authoritative JSON events."""

import json
import os
import queue
import subprocess
import threading
from decimal import MAX_EMAX, MIN_ETINY, Decimal, DecimalException
from time import monotonic

from .model import AgentResult
from .process_control import start_process

_MAX_LINE = 1_048_576
_MAX_OUTPUT = 8_388_608
_MAX_EVENTS = 10_000


def _session_decimal(value):
    # Decimal backends differ in how they handle out-of-range input exponents.
    _, _, exponent = value.lower().partition("e")
    if exponent:
        bound = str(-MIN_ETINY if exponent.startswith("-") else MAX_EMAX)
        magnitude = exponent.lstrip("+-").lstrip("0")
        if len(magnitude) > len(bound) or (len(magnitude) == len(bound) and magnitude > bound):
            raise ValueError("Native event decimal exponent is out of range.")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Native event decimal is not finite.")
    return result


def run_process(
    provider,
    prefix,
    prompt,
    workspace,
    *,
    capabilities,
    task_id,
    attempt_id,
    timeout,
    cancel,
    on_event,
    resume=None,
    read_only=False,
    on_session_event=None,
    on_start=None,
    owner_fd=None,
    session=False,
):
    started = monotonic()
    state = {
        "session_id": None,
        "completed": False,
        "failed": False,
        "output": [],
        "usage": None,
        "cost_usd": None,
        "telemetry_scope": None,
        "error_code": None,
        "model": None,
        "reported_cost": None,
        "reported_cost_currency": None,
        "normalized_usage": None,
    }
    events, status, error_kind, event_count, observed_session = [], None, None, 0, None
    command = provider.command(prefix, resume=resume, read_only=read_only)
    try:
        managed = start_process(
            command,
            owner_fd=owner_fd,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            shell=False,
        )
        process = managed.process
    except OSError as error:
        return AgentResult(
            provider.name,
            "UNAVAILABLE",
            task_id,
            attempt_id,
            version=capabilities.version,
            error_kind="PROCESS_START_FAILED",
            cleanup_confirmed=getattr(error, "cleanup_confirmed", True),
        )
    messages = queue.Queue(maxsize=64)
    stopping = threading.Event()

    def notify(event):
        if on_session_event is not None:
            on_session_event(
                {**event, "native_session_id": state["session_id"], "model": state["model"]}
            )

    def enqueue(message):
        while not stopping.is_set():
            try:
                messages.put(message, timeout=0.05)
                return
            except queue.Full:
                continue

    def read(stream, name):
        try:
            while line := stream.readline(_MAX_LINE + 1):
                enqueue((name, line))
            enqueue((name, None))
        except (OSError, ValueError):
            enqueue((name, None))

    def write():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except (OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=read, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=read, args=(process.stderr, "stderr"), daemon=True),
    ]
    writer = threading.Thread(target=write, daemon=True)
    streams, total = 0, 0
    cleanup = True
    try:
        if on_start is not None:
            try:
                on_start()
            except Exception:
                status, error_kind = "FAILED", "EVENT_CALLBACK_FAILED"
        for thread in (*readers, writer):
            if status is None:
                thread.start()
        while status is None and (streams < 2 or process.poll() is None):
            if cancel is not None and cancel.is_set():
                status, error_kind = "CANCELLED", "CANCEL_REQUESTED"
                break
            if monotonic() - started >= timeout:
                status, error_kind = "TIMEOUT", "TIME_LIMIT"
                break
            try:
                source, line = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                streams += 1
                continue
            total += len(line)
            if total > _MAX_OUTPUT or len(line) > _MAX_LINE or event_count >= _MAX_EVENTS:
                status, error_kind = "PROTOCOL_ERROR", "OUTPUT_LIMIT"
                break
            if source == "stderr":
                continue
            try:
                native = json.loads(
                    line.decode("utf-8"), **({"parse_float": _session_decimal} if session else {})
                )
                if not isinstance(native, dict) or not isinstance(native.get("type"), str):
                    raise ValueError
                provider.parse(native, state)
                if state["error_code"] is not None and not isinstance(state["error_code"], str):
                    raise ValueError
                if session and state["session_id"] is not None:
                    previous = observed_session or resume
                    if previous is not None and state["session_id"] != previous:
                        state["session_id"] = observed_session
                        raise ValueError
                    observed_session = state["session_id"]
                normalized = provider.session_events(native, state) if session else []
                if session and len(normalized) + event_count > _MAX_EVENTS:
                    status, error_kind = "PROTOCOL_ERROR", "OUTPUT_LIMIT"
                    break
                event_count += 1
                event = {"sequence": event_count, "provider_type": native["type"]}
                if native["type"] in {"turn.completed", "result"}:
                    event["kind"] = "completed" if state["completed"] else "failed"
                else:
                    event["kind"] = "progress"
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                RecursionError,
                DecimalException,
            ):
                if not isinstance(state["error_code"], str):
                    state["error_code"] = None
                if session:
                    try:
                        notify(
                            {
                                "kind": "WARNING",
                                "provider_type": None,
                                "payload": {"message": "Malformed native event."},
                            }
                        )
                    except Exception:
                        status, error_kind = "FAILED", "EVENT_CALLBACK_FAILED"
                        break
                status, error_kind = "PROTOCOL_ERROR", "MALFORMED_EVENT"
                break
            try:
                if session:
                    for entry in normalized:
                        notify(entry)
                    state["output"].clear()
                else:
                    events.append(event)
                    if on_event is not None:
                        on_event(dict(event))
            except Exception:
                status, error_kind = "FAILED", "EVENT_CALLBACK_FAILED"
                break
    finally:
        cleanup = managed.finish()
        stopping.set()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not cleanup:
                continue
            try:
                stream.close()
            except OSError:
                pass
        for thread in (*readers, writer):
            if thread.ident is not None:
                thread.join(timeout=0.2)
    if status is None:
        if managed.returncode != 0 or state["failed"]:
            status, error_kind = "FAILED", "PROCESS_OR_PROVIDER_FAILURE"
        elif not state["completed"]:
            status, error_kind = "PROTOCOL_ERROR", "MISSING_COMPLETION"
        else:
            status = "COMPLETED"
    if state["error_code"] in {
        "invalid_api_key",
        "authentication_error",
        "unauthorized",
        "not_authenticated",
        "auth_required",
    }:
        error_kind = "AUTHENTICATION_REQUIRED"
    elif state["error_code"] in {
        "insufficient_quota",
        "rate_limit_exceeded",
        "rate_limit_error",
        "usage_limit_exceeded",
        "quota_exhausted",
    }:
        error_kind = "QUOTA_OR_RATE_LIMIT"
    return AgentResult(
        provider.name,
        status,
        task_id,
        attempt_id,
        version=capabilities.version,
        exit_code=managed.returncode,
        session_id=state["session_id"],
        events=tuple(events),
        output="\n".join(state["output"]),
        usage=state["usage"],
        cost_usd=state["cost_usd"],
        telemetry_scope=state["telemetry_scope"],
        error_kind=error_kind,
        duration_seconds=monotonic() - started,
        cleanup_confirmed=cleanup,
        model=state["model"],
        reported_cost=state["reported_cost"],
        reported_cost_currency=state["reported_cost_currency"],
        normalized_usage=state["normalized_usage"],
    )
