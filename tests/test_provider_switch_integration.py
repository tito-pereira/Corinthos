# SPDX-License-Identifier: MPL-2.0
"""Verify bidirectional switches through two synthetic native provider protocols."""

import json
import sys
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from corinthos.adapters import AdapterManager
from corinthos.bootstrap import initialize
from corinthos.context import ContextService, HandoffService
from corinthos.contracts import ConflictError, StoreError
from corinthos.sessions.repository import SessionRepository
from corinthos.sessions.supervisor import SessionSupervisor
from corinthos.sessions.switch import ProviderSwitchService
from corinthos.work import WorkService

_ROOT = Path(__file__).resolve().parents[1]
_NATIVE_IDS = {
    "codex": "a1b2c3d4-1111-4111-8111-123456789abc",
    "claude": "a1b2c3d4-2222-4222-8222-123456789abc",
}
_NATIVE_ARGS = {
    "codex": ["--sandbox", "read-only", "exec", "--json", "-"],
    "claude": [
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
    ],
}
_FIXTURE_HOST = """
import json
import sys
from pathlib import Path
from threading import Event

args = sys.argv[1:]
log = Path(__file__).with_suffix(".jsonl")

def record(**value):
    with log.open("a", encoding="utf-8") as stream:
        print(json.dumps({"args": args, **value}), file=stream)

if "--version" in args:
    record(kind="probe")
    print("synthetic " + PROVIDER + " fixture 1.2.3")
    raise SystemExit
if "--help" in args:
    record(kind="probe")
    print(HELP)
    raise SystemExit

request = json.loads(sys.stdin.buffer.read().decode("utf-8").split("\\n", 1)[1])
envelope = json.loads(request["context"]["objective"].split("\\n")[-1])
projection = envelope["projection"]
assert projection["target_provider"] == PROVIDER
assert args == ARGV
record(kind="run", envelope=envelope)

def emit(**event):
    print(json.dumps(event), flush=True)

def continuation():
    work = next(source for source in projection["sources"] if source["reason"] == "WORK_ITEM")
    handoff, = [source for source in projection["sources"] if source["kind"] == "SESSION_HANDOFF"]
    source = handoff["capture"]
    assert handoff["authority"] == source["authority"] == "DERIVED"
    assert source["source_session"]["provider"] != PROVIDER
    assert source["source_session"]["work_item_id"] == work["capture"]["work_item_id"]
    marker, = [event["payload"]["text"] for event in source["observations"]
               if event["payload"].get("text", "").startswith("Continuity observation ")]
    return work["identity"] + "|" + handoff["identity"] + "|" + marker
"""
_CODEX = """
emit(type="thread.started", thread_id=NATIVE_ID)

def output(text):
    emit(type="item.completed", item={"type": "agent_message", "text": text})

if IS_SOURCE:
    output(envelope["operator_instruction"])
    for _ in range(5):
        emit(type="item.completed", item={"type": "command_execution"})
    Event().wait(60)
else:
    output(continuation())
    emit(type="turn.completed", usage={"output_tokens": 1})
"""
_CLAUDE = """
emit(type="system", subtype="init", session_id=NATIVE_ID)

def output(text):
    emit(type="result", subtype="success", is_error=False, session_id=NATIVE_ID, result=text)

if IS_SOURCE:
    output(envelope["operator_instruction"])
    for _ in range(5):
        emit(type="assistant", message={"content": [{"type": "tool_use"}]})
    Event().wait(60)
else:
    output(continuation())
"""


def _fixture_programs(directory, source_provider):
    programs = {}
    for provider, help_text, script in (
        ("codex", "--sandbox read-only --json SESSION_ID stdin", _CODEX),
        (
            "claude",
            "--print --output-format stream-json --verbose --resume --tools --disallowedTools",
            _CLAUDE,
        ),
    ):
        path = directory / f"synthetic_{provider}.py"
        header = (
            f"PROVIDER = {provider!r}\nHELP = {help_text!r}\nNATIVE_ID = {_NATIVE_IDS[provider]!r}\n"
            f"ARGV = {_NATIVE_ARGS[provider]!r}\nIS_SOURCE = {provider == source_provider!r}\n"
        )
        path.write_text(header + _FIXTURE_HOST + script, encoding="utf-8")
        programs[provider] = path
    return programs


def _log(program):
    path = program.with_suffix(".jsonl")
    return (
        [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []
    )


def _wait_for_source_observations(repository, session_id):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        try:
            events = repository.events(session_id, limit=25)
        except ConflictError:
            pass
        else:
            if sum(event.kind == "TOOL_ACTIVITY" for event in events) == 5:
                return
        sleep(0.05)
    pytest.fail("The synthetic source did not emit its bounded observations.")


def test_wait_for_source_observations_retries_transient_conflict(monkeypatch):
    clock = Mock(side_effect=[0, 0, 0.05])
    pause = Mock()
    monkeypatch.setattr(sys.modules[__name__], "monotonic", clock)
    monkeypatch.setattr(sys.modules[__name__], "sleep", pause)
    observations = [SimpleNamespace(kind="TOOL_ACTIVITY")] * 5
    repository = SimpleNamespace(events=Mock(side_effect=[ConflictError("busy"), observations]))

    _wait_for_source_observations(repository, "source")

    assert repository.events.call_count == 2
    repository.events.assert_called_with("source", limit=25)
    pause.assert_called_once_with(0.05)


def test_wait_for_source_observations_times_out_on_persistent_conflict(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "monotonic", Mock(side_effect=[0, 0, 15]))
    pause = Mock()
    monkeypatch.setattr(sys.modules[__name__], "sleep", pause)
    repository = SimpleNamespace(events=Mock(side_effect=ConflictError("busy")))

    with pytest.raises(pytest.fail.Exception, match="did not emit its bounded observations"):
        _wait_for_source_observations(repository, "source")

    repository.events.assert_called_once_with("source", limit=25)
    pause.assert_called_once_with(0.05)


def test_wait_for_source_observations_propagates_other_errors(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "monotonic", Mock(return_value=0))
    pause = Mock()
    monkeypatch.setattr(sys.modules[__name__], "sleep", pause)
    error = StoreError("invalid store")
    repository = SimpleNamespace(events=Mock(side_effect=error))

    with pytest.raises(StoreError) as raised:
        _wait_for_source_observations(repository, "source")

    assert raised.value is error
    repository.events.assert_called_once_with("source", limit=25)
    pause.assert_not_called()


@pytest.mark.parametrize(
    ("source_provider", "target_provider"), [("codex", "claude"), ("claude", "codex")]
)
def test_provider_switch_uses_exact_handoff_through_both_native_protocols(
    tmp_path, source_provider, target_provider
):
    root = tmp_path / "project"
    initialize(root)
    work = WorkService(root)
    item = work.create(
        scope_level="TASK",
        objective="Continue the same independent local Work Item.",
        acceptance_criteria=["Observe an exact continuation through the other provider."],
    )
    programs = _fixture_programs(tmp_path, source_provider)
    manager = AdapterManager(
        _ROOT / "policies/standard.yaml",
        _ROOT / "schemas/standard.schema.json",
        commands={name: (sys.executable, str(program)) for name, program in programs.items()},
    )
    supervisor = SessionSupervisor(manager_factory=lambda *args, **kwargs: manager)
    switches = ProviderSwitchService(supervisor)
    repository = SessionRepository(root)
    contexts = ContextService(root)
    handoffs = HandoffService(root)
    source_program, target_program = programs[source_provider], programs[target_provider]
    marker = f"Continuity observation {item.work_item_id[-12:]}"
    try:
        source = supervisor.start(
            root,
            item.work_item_id,
            source_provider,
            instruction=marker,
            read_only=True,
            timeout_seconds=30,
        )
        _wait_for_source_observations(repository, source.session_id)
        assert repository.get(source.session_id).state == "RUNNING"
        source_log = _log(source_program)
        assert not _log(target_program)
        status = switches.status(root, source.session_id, target_provider)
        assert status["stage"] == "SOURCE_ACTIVE"
        assert _log(source_program) == source_log and not _log(target_program)
        assert handoffs.list(session_id=source.session_id) == []

        pending = switches.execute(root, source.session_id, target_provider)
        assert pending["stage"] in {"STOPPING_SOURCE", "SOURCE_TERMINAL"}
        assert (
            pending["handoff_id"]
            is pending["projection_id"]
            is pending["target_session_id"]
            is None
        )
        assert _log(source_program) == source_log
        target_probes = _log(target_program)
        assert target_probes and all(entry["kind"] == "probe" for entry in target_probes)
        stopped = supervisor.wait(root, source.session_id)
        assert stopped.state == "CANCELLED" and not stopped.cleanup_unconfirmed
        assert stopped.native_session_id == _NATIVE_IDS[source_provider]
        assert stopped.owner_id is None
        source_events = repository.events(source.session_id, limit=25)
        assert [event.kind for event in source_events[-2:]] == ["CANCEL_REQUESTED", "CANCELLED"]
        assert handoffs.list(session_id=source.session_id) == []

        advanced = switches.execute(root, source.session_id, target_provider)
        target_id = advanced["target_session_id"]
        assert target_id != source.session_id
        followed = supervisor.wait(root, target_id)
        assert followed.state == "SUCCEEDED" and not followed.cleanup_unconfirmed
        assert followed.owner_id is None
        assert followed.native_session_id == _NATIVE_IDS[target_provider]
        assert followed.provider == target_provider
        assert followed.work_item_id == stopped.work_item_id == item.work_item_id
        assert followed.project_id == stopped.project_id == item.project_id
        assert (
            followed.read_only == stopped.read_only
            and followed.timeout_seconds == stopped.timeout_seconds
        )
        assert followed.run_generation == 1
        assert followed.context_projection_id == advanced["projection_id"]
        projection = contexts.verify(followed.context_projection_id)
        handoff = handoffs.get(advanced["handoff_id"])
        assert handoff.source_session_id == stopped.session_id
        assert handoff.run_generation == stopped.run_generation == advanced["source_generation"]
        assert handoff.event_cutoff == source_events[-1].sequence
        assert handoff.terminal_state == "CANCELLED"
        assert projection.target_provider == target_provider
        assert projection.work_item_id == handoff.work_item_id == item.work_item_id
        (required,) = [
            s for s in projection.canonical_payload["sources"] if s["kind"] == "SESSION_HANDOFF"
        ]
        assert required["identity"] == handoff.handoff_id and required["revision"] == handoff.digest
        assert required["capture"] == handoff.canonical_payload
        except_handoff = [s for s in projection.canonical_payload["sources"] if s != required]
        assert marker in json.dumps(required) and marker not in json.dumps(except_handoff)

        target_log = _log(target_program)
        [run] = [entry for entry in target_log if entry["kind"] == "run"]
        assert run["args"] == _NATIVE_ARGS[target_provider]
        assert _NATIVE_IDS[source_provider] not in json.dumps(run)
        assert run["envelope"]["projection"] == projection.canonical_payload
        assert run["envelope"]["projection_id"] == projection.projection_id
        assert run["envelope"]["projection_digest"] == projection.projection_digest
        text = f"{item.work_item_id}|{handoff.handoff_id}|{marker}"
        outputs = repository.events(target_id, limit=25)
        assert any(
            event.kind == "OUTPUT" and event.payload.get("text") == text for event in outputs
        )
        assert outputs[-1].kind == "SESSION_FINISHED"

        replay = switches.execute(root, source.session_id, target_provider)
        assert replay["stage"] == "TARGET_SESSION_TERMINAL"
        assert replay["target_session_id"] == target_id
        assert replay["projection_id"] == projection.projection_id
        assert replay["handoff_id"] == handoff.handoff_id
        assert switches.status(root, source.session_id, target_provider) == replay
        assert repository.get(source.session_id) == stopped
        assert repository.events(source.session_id, limit=25) == source_events
        assert {row.session_id for row in repository.list()} == {source.session_id, target_id}
        assert handoffs.list(session_id=source.session_id) == [handoff]
        assert work.get(item.work_item_id) == item
        assert _log(source_program) == source_log and _log(target_program) == target_log
    finally:
        assert supervisor.close()
