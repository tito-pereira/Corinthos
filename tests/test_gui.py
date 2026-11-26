# SPDX-License-Identifier: MPL-2.0
"""Disposable GUI operations use real local workers, never an AI account."""

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

from corinthos import gui, providers, runtime, workspace
from corinthos.errors import ConflictError, IntegrityError
from corinthos.filesystem import read_json


@pytest.fixture
def local_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CORINTHOS_PROJECTS_DIR", str(tmp_path / "projects"))
    script = tmp_path / "provider.py"
    script.write_text(
        """import sys,time
from pathlib import Path
prompt=sys.stdin.read()
if 'slow-request' in prompt: time.sleep(2)
if 'Skill: create-project' in prompt:
 Path('context/skill-created.md').write_text('actual create-project invocation')
 print('Created by the selected local skill')
elif 'Skill: orchestrator' in prompt:
 Path('repo/task-result.txt').write_text('actual orchestrator invocation')
 print('Task execution reply')
else:
 if 'edit-request' in prompt: Path('repo/chat-result.txt').write_text('isolated chat result')
 print('Conversation reply: '+('project-context' if 'fixture supporting knowledge' in prompt else 'neutral'))
""",
        encoding="utf-8",
    )
    providers.configure_provider("fixture", [sys.executable, str(script)])
    return script


def wait(operation):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = gui.operation_get(operation["operation_id"])
        if value["status"] not in runtime.ACTIVE:
            return value
        time.sleep(0.03)
    raise AssertionError(value)


def project(tmp_path, name):
    root = tmp_path / name
    workspace.initialize(root)
    (root / "context_agents/knowledge.md").write_text("fixture supporting knowledge")
    return gui.project_ref(root)


def test_creation_runs_skill_before_publication_and_retains_metadata(local_provider):
    started = time.monotonic()
    op = gui.create("Temple Project", "The supplied description", "fixture")
    assert time.monotonic() - started < 2
    final = wait(op)
    assert final["status"] == "SUCCEEDED", final
    selected = final["project"]
    root = Path(selected["root"])
    assert selected["name"] == "Temple Project"
    assert selected["description"] == "The supplied description"
    assert (root / "context/skill-created.md").read_text() == "actual create-project invocation"
    assert "The supplied description" in (root / "context/PROJECT.md").read_text()
    assert workspace.open_project(root)["root"] == str(root)
    assert runtime.list_runs(root) == []
    assert final["run"] is None
    assert gui.projects()["projects"][0]["project_id"] == selected["project_id"]
    assert not list(root.parent.glob(".corinthos-stage-*"))
    with pytest.raises(ConflictError):
        gui.create("Temple Project", "collision", "fixture")


def test_creation_cancel_and_bad_provider_never_publish_or_register(local_provider):
    op = gui.create("Cancelled", "slow-request", "fixture")
    gui.operation_cancel(op["operation_id"])
    final = wait(op)
    assert final["status"] == "CANCELLED", final
    assert not (gui.projects_base() / "Cancelled").exists()
    assert gui.projects()["projects"] == []
    with pytest.raises(IntegrityError):
        gui.create("Bad provider", "description", "missing")
    assert not (gui.projects_base() / "Bad provider").exists()


def test_import_preserves_original_dirty_source_context_and_resolves_enclosing_root(
    local_provider, tmp_path
):
    # An unmanaged source must be outside this checkout's containing workspace.
    with tempfile.TemporaryDirectory(prefix="corinthos-import-") as temporary:
        source = Path(temporary) / "Existing"
        (source / "src").mkdir(parents=True)
        (source / ".git").mkdir()
        (source / ".git/config").write_text("original metadata")
        (source / "src/untracked.py").write_text("uncommitted source")
        (source / "context").mkdir()
        (source / "context/PROJECT.md").write_text("custom project context")
        (source / "README.md").write_text("custom readme")
        before = {
            str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()
        }
        final = wait(gui.import_selected(str(source))["operation"])
        assert final["status"] == "SUCCEEDED", final
        root = Path(final["project"]["root"])
        assert (root / "repo/src/untracked.py").read_text() == "uncommitted source"
        assert "custom project context" in (root / "context/PROJECT.md").read_text()
        assert (root / "README.md").read_text() == "custom readme"
        assert not (root / "repo/.git").exists()
        assert {
            str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()
        } == before
        assert gui.open_selected(str(root / "repo/src"))["root"] == str(root)
        assert "independent working copy" in final["message"]


def test_main_and_project_conversation_scope_context_output_and_apply(local_provider, tmp_path):
    first = project(tmp_path, "First")
    second = project(tmp_path, "Second")
    main = gui.conversation_open()
    one = gui.conversation_open(first)
    two = gui.conversation_open(second)
    neutral = wait(gui.conversation_send(main["conversation_id"], "hello", "fixture"))
    assert neutral["status"] == "SUCCEEDED", neutral
    op = gui.conversation_send(one["conversation_id"], "slow-request edit-request", "fixture")
    assert gui.conversation_get(two["conversation_id"])["messages"] == []
    final = wait(op)
    assert final["status"] == "SUCCEEDED", final
    assert "project-context" in gui.conversation_get(one["conversation_id"])["messages"][-1]["text"]
    run_id = final["run"]["run_id"]
    assert "project-context" in gui.conversation_output(one["conversation_id"], run_id)["text"]
    with pytest.raises(ConflictError):
        gui.conversation_output(two["conversation_id"], run_id)
    assert not (Path(first["root"]) / "repo/chat-result.txt").exists()
    applied = wait(gui.run_action(first, run_id, "apply"))
    assert applied["status"] == "SUCCEEDED", applied
    assert applied["result"]["applied_count"] == 1
    assert (Path(first["root"]) / "repo/chat-result.txt").read_text() == "isolated chat result"
    assert not (Path(second["root"]) / "repo/chat-result.txt").exists()
    discarded = wait(
        gui.run_action(first, run_id, "discard", "Reviewed and applied the fixture result.")
    )
    assert discarded["status"] == "SUCCEEDED", discarded
    assert runtime.list_runs(first["root"]) == []
    assert read_json(Path(first["root"]) / "context_agents/corinthos/continuity.json")["completed"]


def test_task_actions_are_explicit_deduplicated_and_conflicts_preserve_output(
    local_provider, tmp_path
):
    selected = project(tmp_path, "Tasks")
    assert gui.execute_tasks(selected, "missing") == {"empty": True, "message": "No Tasks Yet"}
    root = Path(selected["root"])
    plan = root / "task/implementation-groups.md"
    plan.write_text(
        "| Group | Status | Objective |\n|---|---|---|\n| 1 | Not Implemented | Build |"
    )
    assert gui.summary(selected, "tasks")["empty"] is False
    assert runtime.list_runs(root) == []
    op = gui.execute_tasks(selected, "fixture")["operation"]
    same = gui.execute_tasks(selected, "fixture")["operation"]
    assert same["operation_id"] == op["operation_id"]
    final = wait(op)
    assert final["status"] == "SUCCEEDED", final
    run_id = final["run"]["run_id"]
    (root / "repo/task-result.txt").write_text("user changed destination")
    conflict = wait(gui.run_action(selected, run_id, "apply"))
    assert conflict["status"] == "FAILED"
    assert runtime.get(root, run_id)["integrated"] is False
    assert Path(runtime.get(root, run_id)["workspace"]).is_dir()
    assert (root / "repo/task-result.txt").read_text() == "user changed destination"


def test_recent_limit_and_typed_gui_envelopes(local_provider, tmp_path):
    from corinthos.application.service import invoke

    for index in range(12):
        selected = project(tmp_path, f"Project{index}")
        gui.open_selected(selected["root"])
    assert len(gui.projects()["projects"]) == 10
    assert gui.projects()["projects"][0]["name"] == "Project11"
    response = invoke("gui.open", {"path": str(tmp_path / "missing")})
    assert response["ok"] is False
    assert response["error"]
    assert len(json.dumps(gui.projects()).encode()) < 48000


def test_interrupted_publication_restores_only_owned_staging(local_provider):
    import uuid

    from corinthos.filesystem import atomic_json

    identity = uuid.uuid4().hex
    atomic_json(
        gui._op_path(identity),
        {
            "operation_id": identity,
            "run_id": identity,
            "kind": "create",
            "status": "RUNNING",
            "created_at": time.time() - 30,
            "payload": {"name": "Recovered"},
            "project": None,
        },
    )
    stage = gui._stage(identity)
    target = gui.projects_base() / "Recovered"
    marker = stage / "context_agents/corinthos/state.json"
    state = read_json(marker)
    state["root"] = str(target)
    atomic_json(marker, state)
    gui._change_op(identity, phase="Publishing", final=str(target))
    outcome = gui.operation_get(identity)
    assert outcome["status"] == "INTERRUPTED"
    assert read_json(marker)["root"] == str(stage)
    assert not target.exists()
    with pytest.raises(ConflictError, match="not finished"):
        gui.open_selected(str(stage))


def test_status_read_does_not_wait_for_long_project_integration(local_provider, tmp_path):
    from corinthos.application.service import invoke
    from corinthos.filesystem import file_lock

    selected = project(tmp_path, "Busy integration")
    conversation = gui.conversation_open(selected)
    completed = wait(gui.conversation_send(conversation["conversation_id"], "hello", "fixture"))
    assert completed["status"] == "SUCCEEDED", completed
    run_id = completed["run"]["run_id"]
    operations = set((gui._home() / "operations").glob("*.json"))
    started = time.monotonic()
    with file_lock(Path(selected["root"]) / "context_agents/corinthos/project.lock"):
        with pytest.raises(ConflictError, match="owns"):
            gui.run_get(selected, "a" * 32)
        with pytest.raises(ConflictError, match="owns"):
            gui.runs(selected)
        output = invoke(
            "gui.conversation.output",
            {"conversation_id": conversation["conversation_id"], "run_id": run_id, "cursor": 0},
        )
        assert output["ok"] is False and "Retry output" in output["error"]
        action = invoke(
            "gui.run.action", {"project": selected, "run_id": run_id, "action": "apply"}
        )
        assert action["ok"] is False and "not queued" in action["error"]
    assert time.monotonic() - started < 1
    assert set((gui._home() / "operations").glob("*.json")) == operations
    retried = gui.conversation_output(conversation["conversation_id"], run_id, cursor=0)
    assert "Conversation reply" in retried["text"] and retried["complete"]
    assert wait(gui.run_action(selected, run_id, "apply"))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("admitted_run", [False, True])
def test_dead_conversation_recovers_message_and_allows_later_send(
    local_provider, tmp_path, monkeypatch, admitted_run
):
    selected = project(tmp_path, "Interrupted conversation")
    conversation = gui.conversation_open(selected)
    with monkeypatch.context() as paused:
        paused.setattr(gui, "_launch_operation", lambda identity: None)
        operation = gui.conversation_send(conversation["conversation_id"], "first", "fixture")
    gui._change_op(operation["operation_id"], status="RUNNING", created_at=time.time() - 30)
    changes = {"text": "Already retained output", "status": "RUNNING"}
    if admitted_run:
        run = runtime.start(
            selected["root"],
            "conversation",
            provider="fixture",
            prepared_prompt="slow-request",
            claims=gui._CLAIMS,
        )
        changes["run_id"] = run["run_id"]
        gui._change_op(operation["operation_id"], run=run)
    gui._update_message(conversation["conversation_id"], operation["operation_id"], **changes)
    assert gui.operation_get(operation["operation_id"])["status"] == "INTERRUPTED"
    recovered = gui.conversation_get(conversation["conversation_id"])["messages"][-1]
    assert recovered["status"] == "INTERRUPTED"
    assert recovered["text"] == changes["text"]
    assert recovered["operation_id"] == operation["operation_id"]
    if admitted_run:
        assert recovered["run_id"] == run["run_id"]
        # Recovering the coordinator must neither cancel nor discard a live provider.
        assert runtime.get(selected["root"], run["run_id"])["status"] in runtime.ACTIVE
        deadline = time.monotonic() + 10
        while runtime.get(selected["root"], run["run_id"])["status"] in runtime.ACTIVE:
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert (
            "Conversation reply"
            in gui.conversation_output(conversation["conversation_id"], run["run_id"])["text"]
        )
        assert runtime.get(selected["root"], run["run_id"])["integrated"] is False
    later = wait(gui.conversation_send(conversation["conversation_id"], "later", "fixture"))
    assert later["status"] == "SUCCEEDED", later


def test_live_conversation_owner_is_not_recovered(local_provider, tmp_path, monkeypatch):
    selected = project(tmp_path, "Owned conversation")
    conversation = gui.conversation_open(selected)
    with monkeypatch.context() as paused:
        paused.setattr(gui, "_launch_operation", lambda identity: None)
        operation = gui.conversation_send(conversation["conversation_id"], "first", "fixture")
    identity = operation["operation_id"]
    gui._change_op(identity, status="RUNNING", created_at=time.time() - 30)
    runtime._register(gui._home(), identity)
    with runtime.worker_ownership(gui._home(), identity):
        assert gui.operation_get(identity)["status"] == "RUNNING"
        assert (
            gui.conversation_get(conversation["conversation_id"])["messages"][-1]["status"]
            == "STARTING"
        )
        with pytest.raises(ConflictError, match="active request"):
            gui.conversation_send(conversation["conversation_id"], "too early", "fixture")
    # Direct admission also performs recovery, without requiring an earlier status poll.
    later = wait(gui.conversation_send(conversation["conversation_id"], "later", "fixture"))
    assert later["status"] == "SUCCEEDED", later
    assert (
        gui.conversation_get(conversation["conversation_id"])["messages"][1]["status"]
        == "INTERRUPTED"
    )


def test_recent_hints_do_not_duplicate_large_project_descriptions(tmp_path):
    from corinthos.application.service import _remember
    from corinthos.user_state import read_state

    for index in range(64):
        original = {
            "root": str(tmp_path / str(index)),
            "project_id": str(index),
            "workspace_format": 1,
            "name": str(index),
            "description": "d" * 4000,
        }
        assert _remember(original) == original
    hints = read_state("recent-projects.json", {})["projects"]
    assert len(hints) == 64
    assert all("description" not in hint for hint in hints)
