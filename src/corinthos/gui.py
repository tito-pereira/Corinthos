# SPDX-License-Identifier: MPL-2.0
"""Bounded desktop interactions composed from the shared workspace/runtime."""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from corinthos import providers, runtime, workspace
from corinthos.errors import ConflictError, IntegrityError
from corinthos.filesystem import atomic_json, file_lock, plain_path, read_json
from corinthos.user_state import state_root

_ID = re.compile(r"[a-f0-9]{32}")
_CLAIMS = ["repo", "context", "task", "task_agents", "context_agents", ".agents"]
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


class Cancelled(Exception):
    pass


def _home():
    path = state_root() / "gui"
    plain_path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise IntegrityError("Supply a valid GUI identifier.")
    return value


def _text(value, label, limit, *, empty=False):
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or "\0" in value
        or len(value.encode("utf-8")) > limit
    ):
        raise IntegrityError(f"{label} must be bounded UTF-8 text.")
    return value


def _name(value):
    value = _text(value, "Project name", 320)
    if (
        len(value) > 80
        or value != value.strip()
        or value.endswith(".")
        or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in value)
        or value in {".", ".."}
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }
    ):
        raise IntegrityError(
            "Use a plain project name without reserved filenames or path characters."
        )
    return value


def projects_base():
    override = os.environ.get("CORINTHOS_PROJECTS_DIR")
    if override:
        return workspace._root(override)
    if os.name == "nt":
        folder = (ctypes.c_byte * 16).from_buffer_copy(
            uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le
        )
        result = ctypes.c_wchar_p()
        call = ctypes.windll.shell32.SHGetKnownFolderPath
        call.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        if call(ctypes.byref(folder), 0, None, ctypes.byref(result)) != 0:
            raise IntegrityError("The Documents folder could not be located.")
        try:
            documents = Path(result.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(result)
    else:
        documents = Path.home() / "Documents"
    return workspace._root(documents / "Corinthos Projects")


def project_ref(root):
    identity = workspace.open_project(root)
    value = read_json(Path(identity["root"]) / "context_agents/corinthos/state.json")
    name = _text(value.get("name", Path(identity["root"]).name), "Project name", 320)
    description = _text(value.get("description", ""), "Project description", 4000, empty=True)
    source = value.get("import_source")
    if source is not None:
        _text(source, "Import source", 16384)
    return {**identity, "name": name, "description": description, "import_source": source}


def _project(value):
    if not isinstance(value, dict) or not isinstance(value.get("root"), str):
        raise IntegrityError("Select a project workspace.")
    current = project_ref(value["root"])
    if current["project_id"] != value.get("project_id"):
        raise ConflictError("This request belongs to a different project.")
    return current


def _remember(project):
    from corinthos.application.service import _remember as remember

    return remember(project)


def projects():
    from corinthos.application.service import projects_list

    entries = []
    for row in projects_list()["projects"]:
        try:
            entries.append({**project_ref(row["root"]), "available": True})
        except (KeyError, OSError, IntegrityError, ConflictError):
            entries.append(
                {
                    "root": str(row.get("root", ""))[:4096],
                    "name": str(row.get("name", "Unavailable project"))[:80],
                    "available": False,
                }
            )
        if len(entries) >= 10:
            break
    result = {"projects": entries, "base": str(projects_base())}
    while len(json.dumps(result).encode()) > 48000 and entries:
        entries.pop()
    return result


def _op_path(identity):
    return _home() / "operations" / (_id(identity) + ".json")


def _read_op(identity, *, locked=False):
    if not locked:
        with file_lock(_op_path(identity).with_suffix(".lock")):
            return _read_op(identity, locked=True)
    value = read_json(_op_path(identity))
    if (
        not isinstance(value, dict)
        or value.get("operation_id") != identity
        or value.get("status") not in runtime.ACTIVE | _TERMINAL
    ):
        raise IntegrityError("GUI operation is missing or invalid.")
    return value


def _change_op(identity, **changes):
    path = _op_path(identity)
    with file_lock(path.with_suffix(".lock")):
        value = _read_op(identity, locked=True)
        value.update(changes, updated_at=time.time())
        atomic_json(path, value)
    return value


def _view_op(value):
    result = {
        key: value.get(key)
        for key in (
            "operation_id",
            "kind",
            "status",
            "phase",
            "project",
            "message",
            "error",
            "cancel_requested",
        )
    }
    run = value.get("run")
    result["run"] = (
        {key: run[key] for key in ("run_id", "project_id") if key in run} if run else None
    )
    result["result"] = {
        key: item for key, item in value.get("result", {}).items() if key not in {"project", "run"}
    }
    return result


def _launch_operation(identity):
    runtime._register(_home(), identity)
    flags = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-m", "corinthos.gui_worker", identity],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **flags,
    )


def _start(kind, payload, *, project=None):
    with file_lock(_home() / "dispatch.lock"):
        paths = (
            sorted((_home() / "operations").glob("*.json"), key=lambda p: p.stat().st_mtime)
            if (_home() / "operations").exists()
            else []
        )
        for path in paths:
            item = _read_op(path.stem)
            if (
                kind == "execute"
                and item["kind"] == kind
                and item.get("project", {}).get("project_id") == project["project_id"]
                and item["status"] in runtime.ACTIVE
            ):
                return _view_op(item)
        if len(paths) >= 128:
            for path in paths:
                item = _read_op(path.stem)
                if item["status"] in _TERMINAL and not item.get("stage"):
                    path.unlink()
                    paths.remove(path)
                    break
            if len(paths) >= 128:
                raise ConflictError(
                    "GUI operation history is full; recover pending operations first."
                )
        identity = uuid.uuid4().hex
        now = time.time()
        value = {
            "operation_id": identity,
            "run_id": identity,
            "kind": kind,
            "payload": payload,
            "project": project,
            "status": "STARTING",
            "phase": "Queued",
            "created_at": now,
            "updated_at": now,
            "cancel_requested": False,
        }
        if kind == "action":
            value["run"] = {
                "root": project["root"],
                "project_id": project["project_id"],
                "run_id": payload["run_id"],
            }
        atomic_json(_op_path(identity), value)
        try:
            _launch_operation(identity)
        except (OSError, IntegrityError) as error:
            _change_op(identity, status="FAILED", error=str(error)[:2048])
            raise
    return _view_op(value)


def operation_get(operation_id):
    value = _read_op(_id(operation_id))
    if value["status"] in runtime.ACTIVE and not runtime._alive(_home(), value):
        # Completion can race the ownership probe; never replace a terminal result.
        value = _read_op(operation_id)
        if value["status"] not in runtime.ACTIVE or runtime._alive(_home(), value):
            _reconcile_conversation_operation(value)
            return _view_op(value)
        # Only a matching newly owned publication can be recovered automatically.
        final = value.get("final")
        if value.get("phase") == "Publishing" and final:
            try:
                expected = projects_base() / _name(value["payload"]["name"])
                stage = projects_base() / (".corinthos-stage-" + operation_id)
                if str(expected) != final or value.get("stage") not in {None, str(stage)}:
                    raise IntegrityError("Publication journal has an unexpected path.")
                if stage.exists():
                    marker = stage / "context_agents/corinthos/state.json"
                    stored = read_json(marker)
                    owner = read_json(stage / "context_agents/corinthos/gui-owner.json")
                    if (
                        owner
                        != {"operation_id": operation_id, "project_id": value.get("stage_uuid")}
                        or stored.get("project_id") != value.get("stage_uuid")
                        or stored.get("root") not in {str(stage), final}
                    ):
                        raise IntegrityError(
                            "Publication staging ownership could not be confirmed."
                        )
                    stored["root"] = str(stage)
                    atomic_json(marker, stored)
                project = project_ref(final)
                owner_path = Path(final) / "context_agents/corinthos/gui-owner.json"
                owner = read_json(owner_path)
                owned = owner == {
                    "operation_id": operation_id,
                    "project_id": value.get("stage_uuid"),
                } or (
                    value.get("stage") is None
                    and value.get("project", {}).get("project_id") == project["project_id"]
                )
                if (
                    project["project_id"] == value.get("stage_uuid")
                    and owned
                    and not read_json(Path(final) / "context_agents/corinthos/runs.json", {})
                ):
                    owner_path.unlink(missing_ok=True)
                    value = _change_op(
                        operation_id,
                        status="SUCCEEDED",
                        project=_remember(project),
                        stage=None,
                        message="Project publication recovered.",
                    )
            except (OSError, IntegrityError, ConflictError):
                pass
        if value["status"] in runtime.ACTIVE:
            with file_lock(_op_path(operation_id).with_suffix(".lock")):
                value = _read_op(operation_id, locked=True)
                if value["status"] in runtime.ACTIVE and not runtime._alive(_home(), value):
                    value.update(
                        status="INTERRUPTED",
                        error="The operation worker exited. Owned staging and isolated outputs were preserved.",
                    )
                    atomic_json(_op_path(operation_id), value)
    _reconcile_conversation_operation(value)
    return _view_op(value)


def operation_cancel(operation_id):
    path = _op_path(_id(operation_id))
    with file_lock(path.with_suffix(".lock")):
        value = _read_op(operation_id, locked=True)
        if value["status"] in runtime.ACTIVE:
            if value.get("phase") == "Publishing":
                raise ConflictError("Publication is committing; wait for its actual result.")
            value.update(
                cancel_requested=True,
                message="Cancellation requested; waiting for owned work to stop.",
            )
            atomic_json(path, value)
    return _view_op(value)


def create(name, description, provider):
    name = _name(name)
    description = _text(description, "Project description", 4000, empty=True)
    providers.command_for(provider)
    target = projects_base() / name
    if os.path.lexists(target):
        raise ConflictError("That project destination already exists; choose another name.")
    return _start("create", {"name": name, "description": description, "provider": provider})


def _containing(path):
    path = workspace._root(path)
    if not path.is_dir():
        raise IntegrityError("Select an existing project directory.")
    for candidate in (path, *list(path.parents)[:24]):
        marker = candidate / "context_agents/corinthos/state.json"
        if marker.exists():
            if (candidate / "context_agents/corinthos/gui-owner.json").exists():
                raise ConflictError(
                    "This owned project creation/import has not finished publication."
                )
            return project_ref(candidate)
        if all((candidate / name).is_dir() for name in ("repo", "context", "task")):
            workspace.initialize(candidate)
            return project_ref(candidate)
    return None


def open_selected(path):
    project = _containing(path)
    if project is None:
        raise IntegrityError(
            "No Corinthos workspace contains this folder. Use Import Project for an unmanaged source."
        )
    return _remember(project)


def import_selected(path):
    project = _containing(path)
    if project is not None:
        return {"project": _remember(project)}
    source = workspace._root(path)
    if (source / "context_agents/corinthos").exists():
        raise ConflictError("Orphan runtime state cannot be imported into a different identity.")
    workspace._legacy(source)
    name = _name(source.name)
    if os.path.lexists(projects_base() / name):
        raise ConflictError(
            "The imported workspace destination already exists; resume it or rename the source folder."
        )
    return {"operation": _start("import", {"source": str(source), "name": name})}


def _check_cancel(identity):
    if _read_op(identity).get("cancel_requested"):
        raise Cancelled()


def _wait_run(identity, root, run_id):
    _change_op(
        identity,
        run={
            "root": str(root),
            "project_id": workspace.open_project(root)["project_id"],
            "run_id": run_id,
        },
    )
    while True:
        if _read_op(identity).get("cancel_requested"):
            runtime.cancel(root, run_id)
        run = runtime.get(root, run_id)
        if run["status"] not in runtime.ACTIVE and not runtime._alive(root, run):
            break
        time.sleep(0.1)
    _check_cancel(identity)
    if run["status"] != "SUCCEEDED":
        if run["status"] == "CANCELLED":
            raise Cancelled()
        raise ConflictError(run.get("error") or f"Provider run ended with {run['status']}.")
    return run


def _metadata(root, name, description, source=None):
    root = Path(root)
    marker = root / "context_agents/corinthos/state.json"
    value = read_json(marker)
    value.update(name=name, description=description)
    if source:
        value["import_source"] = source
    atomic_json(marker, value)
    context = root / "context/PROJECT.md"
    plain_path(context, file=True)
    existing = context.read_text(encoding="utf-8-sig") if context.exists() else ""
    details = f"# {name}\n\n{description}\n"
    if details.strip() not in existing:
        context.write_text(details + ("\n" + existing if existing else ""), encoding="utf-8")


def _rename_new(source, destination):
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise IntegrityError(
            "This platform cannot safely publish without replacing an existing directory."
        )
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        raise OSError(
            ctypes.get_errno(), "Project destination could not be published without replacement."
        )


def _stage(identity):
    base = projects_base()
    base.mkdir(parents=True, exist_ok=True)
    stage = base / (".corinthos-stage-" + identity)
    stage.mkdir()
    _change_op(identity, stage=str(stage))
    project = workspace.initialize(stage)
    atomic_json(
        stage / "context_agents/corinthos/gui-owner.json",
        {"operation_id": identity, "project_id": project["project_id"]},
    )
    _change_op(identity, stage=str(stage), stage_uuid=project["project_id"])
    return stage


def _publish(identity, stage, name):
    _check_cancel(identity)
    target = projects_base() / name
    with file_lock(projects_base() / ".corinthos-publication.lock"):
        if os.path.lexists(target):
            raise ConflictError(
                "The destination was created by another operation; no files were replaced."
            )
        value = read_json(stage / "context_agents/corinthos/state.json")
        op = _read_op(identity)
        if value["project_id"] != op["stage_uuid"] or read_json(
            stage / "context_agents/corinthos/runs.json", {}
        ):
            raise ConflictError("Owned staging still has runtime records or a different identity.")
        with file_lock(_op_path(identity).with_suffix(".lock")):
            op = _read_op(identity, locked=True)
            if op.get("cancel_requested"):
                raise Cancelled()
            op.update(phase="Publishing", final=str(target))
            atomic_json(_op_path(identity), op)
        value["root"] = str(target)
        atomic_json(stage / "context_agents/corinthos/state.json", value)
        try:
            _rename_new(stage, target)
        except BaseException:
            value["root"] = str(stage)
            atomic_json(stage / "context_agents/corinthos/state.json", value)
            raise
        project = project_ref(target)
        (target / "context_agents/corinthos/gui-owner.json").unlink()
        _change_op(identity, stage=None, project=project, run=None)
        return _remember(project)


def _remove_stage(identity):
    op = _read_op(identity)
    if not op.get("stage"):
        return
    stage = Path(op["stage"])
    if stage != projects_base() / (".corinthos-stage-" + identity) or not stage.exists():
        return
    owner = read_json(stage / "context_agents/corinthos/gui-owner.json")
    if owner != {"operation_id": identity, "project_id": op.get("stage_uuid")}:
        raise ConflictError("Owned staging could not be confirmed; it was preserved.")
    for path in stage.rglob("*"):
        plain_path(path, file=not path.is_dir())
    shutil.rmtree(stage)
    _change_op(identity, stage=None)


def _copy_source(source, stage):
    if source == projects_base() or source in projects_base().parents:
        raise ConflictError("Import source cannot contain the managed project destination.")
    before = runtime._files(source)
    for relative, digest in before.items():
        origin, destination = source / relative, stage / "repo" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
        if runtime.hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ConflictError("Source changed during import; no workspace was published.")
    if runtime._files(source) != before:
        raise ConflictError("Source changed during import; no workspace was published.")
    for relative in before:
        first = relative.split("/")[0]
        if first in {
            "README.md",
            "AGENTS.md",
            "CLAUDE.md",
            ".agents",
            "context",
            "context_agents",
            "task",
            "task_agents",
        }:
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, destination)


def _creation_work(identity, op):
    payload = op["payload"]
    stage = _stage(identity)
    _check_cancel(identity)
    if op["kind"] == "import":
        _change_op(identity, phase="Copying source")
        _copy_source(Path(payload["source"]), stage)
        workspace.initialize(stage)
        _metadata(
            stage,
            payload["name"],
            "Imported independent working copy; original source and Git history remain unchanged.",
            payload["source"],
        )
    else:
        _change_op(identity, phase="Running create-project")
        run = runtime.start(
            stage,
            "create-project",
            provider=payload["provider"],
            claims=_CLAIMS,
            instruction=f"Initialize this project using the selected create-project skill. Project name: {payload['name']}\nProject description: {payload['description']}\nThe existing blueprint is bootstrap only; preserve it and write the requested normal project context.",
        )
        run = _wait_run(identity, stage, run["run_id"])
        _change_op(identity, output=run.get("output_tail", "")[-4096:])
        runtime.integrate(stage, run["run_id"])
        runtime.discard(
            stage,
            run["run_id"],
            summary="Create-project skill completed and its changes were integrated before publication.",
        )
        _metadata(stage, payload["name"], payload["description"])
    _check_cancel(identity)
    workspace.open_project(stage)
    project = _publish(identity, stage, payload["name"])
    return {
        "project": project,
        "message": "Project created."
        if op["kind"] == "create"
        else "Imported an independent working copy. Future work changes this copy; the original source and Git metadata remain untouched.",
    }


def _conversation_path(identity):
    return _home() / "conversations" / (_id(identity) + ".json")


def _read_conversation(identity, *, locked=False):
    if not locked:
        with file_lock(_conversation_path(identity).with_suffix(".lock")):
            return _read_conversation(identity, locked=True)
    value = read_json(_conversation_path(identity))
    if (
        not isinstance(value, dict)
        or value.get("conversation_id") != identity
        or value.get("kind") not in {"main", "project"}
    ):
        raise IntegrityError("Conversation is missing or invalid.")
    if value["kind"] == "project":
        project = _project(value["project"])
        if value.get("root") != project["root"]:
            raise IntegrityError("Conversation root differs from its project identity.")
    elif value["root"] != str(_home() / "main-workspace"):
        raise IntegrityError("Main conversation cannot target a project.")
    return value


def _conversation_view(value):
    messages = []
    for message in reversed(value.get("messages", [])):
        candidate = {**message, "text": message.get("text", "")[:4096]}
        if len(json.dumps([candidate, *messages]).encode()) > 40000:
            break
        messages.insert(0, candidate)
    result = {
        "conversation_id": value["conversation_id"],
        "kind": value["kind"],
        "project": value.get("project"),
        "messages": messages,
    }
    while len(json.dumps(result).encode()) > 48000 and messages:
        messages.pop(0)
    return {**result, "omitted": len(value.get("messages", [])) - len(messages)}


def conversation_open(project=None):
    project = _project(project) if project is not None else None
    key = project["project_id"] if project else "main"
    with file_lock(_home() / "conversations.lock"):
        index = read_json(_home() / "conversations.json", {})
        if key not in index:
            if len(index) >= 64:
                raise ConflictError(
                    "Conversation registry is full; retain existing project conversations first."
                )
            identity = uuid.uuid4().hex
            root = project["root"] if project else str(_home() / "main-workspace")
            if project is None:
                workspace.initialize(root)
            value = {
                "conversation_id": identity,
                "kind": "project" if project else "main",
                "project": project,
                "root": root,
                "messages": [],
            }
            atomic_json(_conversation_path(identity), value)
            index[key] = identity
            atomic_json(_home() / "conversations.json", index)
        return conversation_get(index[key])


def conversation_get(conversation_id):
    value = _read_conversation(_id(conversation_id))
    for message in value["messages"]:
        if message.get("status") in runtime.ACTIVE and message.get("operation_id"):
            operation_get(message["operation_id"])
    return _conversation_view(_read_conversation(conversation_id))


def _save_conversation(path, value):
    while len(json.dumps(value).encode()) > 220000 and len(value["messages"]) > 2:
        if value["messages"][0].get("status") in runtime.ACTIVE:
            raise ConflictError("Active conversation context cannot be discarded.")
        value["messages"].pop(0)
    atomic_json(path, value)


def conversation_send(conversation_id, message, provider):
    message = _text(message, "Message", 8192)
    providers.command_for(provider)
    conversation_get(conversation_id)
    path = _conversation_path(conversation_id)
    with file_lock(path.with_suffix(".lock")):
        value = _read_conversation(conversation_id, locked=True)
        if any(item.get("status") in runtime.ACTIVE for item in value["messages"]):
            raise ConflictError("Wait for or cancel this conversation's active request.")
        operation = _start(
            "conversation",
            {"conversation_id": conversation_id, "message": message, "provider": provider},
            project=value.get("project"),
        )
        value["messages"] = value["messages"][-20:]
        value["messages"].extend(
            [
                {"role": "user", "text": message},
                {
                    "role": "assistant",
                    "text": "",
                    "operation_id": operation["operation_id"],
                    "status": "STARTING",
                },
            ]
        )
        _save_conversation(path, value)
    return operation


def _update_message(conversation_id, operation_id, **changes):
    path = _conversation_path(conversation_id)
    with file_lock(path.with_suffix(".lock")):
        value = _read_conversation(conversation_id, locked=True)
        for message in value["messages"]:
            if message.get("operation_id") == operation_id:
                message.update(changes)
        _save_conversation(path, value)


def _reconcile_conversation_operation(operation):
    if operation["kind"] != "conversation" or operation["status"] not in _TERMINAL:
        return
    identity = operation["payload"]["conversation_id"]
    path = _conversation_path(identity)
    with file_lock(path.with_suffix(".lock"), blocking=False):
        value = _read_conversation(identity, locked=True)
        changed = False
        for message in value["messages"]:
            if (
                message.get("operation_id") == operation["operation_id"]
                and message.get("status") in runtime.ACTIVE
            ):
                # Coordinator recovery does not cancel/discard an admitted provider run.
                # Its immutable run/output association and existing text stay available.
                message.update(status=operation["status"], error=operation.get("error"))
                changed = True
        if changed:
            _save_conversation(path, value)


def conversation_output(conversation_id, run_id, cursor=0):
    value = _read_conversation(conversation_id)
    if not any(message.get("run_id") == run_id for message in value["messages"]):
        raise ConflictError("That run belongs to a different conversation.")
    try:
        return runtime.output(value["root"], run_id, cursor, blocking=False)
    except ConflictError as error:
        raise ConflictError(
            "Project is busy. Retry output shortly; the output cursor is unchanged."
        ) from error


def _conversation_work(identity, op):
    from corinthos.gui_content import project_context

    value = _read_conversation(op["payload"]["conversation_id"])
    history = (
        "\n".join(
            f"{message['role']}: {message.get('text', '')}"
            for message in value["messages"][-12:]
            if message.get("text")
        )
        .encode("utf-8")[-20000:]
        .decode("utf-8", errors="replace")
    )
    context = (
        project_context(value["root"])
        if value["kind"] == "project"
        else "This is the Main conversation. No project is selected. Give general guidance; do not perform project work, invoke project skills or use a remembered project."
    )
    prompt = f"You are the Corinthos conversation agent.\n{context}\n\nConversation:\n{history}\n\nUser: {op['payload']['message']}\nRespond directly in readable text. Use the isolated workspace for project work; applying changes to the canonical project is a separate explicit user action."
    run = runtime.start(
        value["root"],
        "conversation",
        provider=op["payload"]["provider"],
        prepared_prompt=prompt,
        claims=_CLAIMS if value["kind"] == "project" else [],
    )
    _update_message(value["conversation_id"], identity, run_id=run["run_id"], status="RUNNING")
    try:
        run = _wait_run(identity, value["root"], run["run_id"])
    finally:
        run = runtime.get(value["root"], run["run_id"])
        output, cursor = "", 0
        while True:
            chunk = runtime.output(value["root"], run["run_id"], cursor)
            output += chunk["text"]
            if chunk["complete"] or chunk["cursor"] == cursor:
                break
            cursor = chunk["cursor"]
        _update_message(
            value["conversation_id"],
            identity,
            text=output[:8192],
            truncated=len(output) > 8192,
            status=run["status"],
            error=run.get("error"),
        )
    return {
        "run": {"project_id": run["project_id"], "root": run["root"], "run_id": run["run_id"]},
        "message": "Provider execution completed; project changes remain isolated until applied.",
    }


def summary(project, kind):
    from corinthos.gui_content import summary as read_summary

    return read_summary(_project(project)["root"], kind)


def execute_tasks(project, provider):
    from corinthos.gui_content import pending_groups

    project = _project(project)
    if not pending_groups(project["root"]):
        return {"empty": True, "message": "No Tasks Yet"}
    providers.command_for(provider)
    return {"empty": False, "operation": _start("execute", {"provider": provider}, project=project)}


def run_action(project, run_id, action, summary=""):
    project = _project(project)
    if action not in {"apply", "discard", "cancel"}:
        raise IntegrityError("Select Apply Changes, Discard or Cancel.")
    if action == "discard":
        _text(summary, "Discard summary", 2048)
    try:
        runtime.get(project["root"], _id(run_id), blocking=False)
    except ConflictError as error:
        raise ConflictError(
            "Project is busy. Retry this action shortly; it was not queued."
        ) from error
    return _start(
        "action", {"run_id": run_id, "action": action, "summary": summary}, project=project
    )


def run_get(project, run_id):
    project = _project(project)
    value = runtime.get(project["root"], run_id, blocking=False)
    return {
        key: value.get(key) for key in ("run_id", "status", "integrated", "error", "output_tail")
    }


def runs(project):
    project = _project(project)
    return {
        "runs": [
            {key: value.get(key) for key in ("run_id", "skill", "status", "integrated", "error")}
            for value in runtime.list_runs(project["root"], blocking=False)
            if value["skill"] in {"conversation", "orchestrator"}
        ]
    }


def _forget_conversation_run(project, run_id):
    index = read_json(_home() / "conversations.json", {})
    identity = index.get(project["project_id"])
    if not identity:
        return
    path = _conversation_path(identity)
    with file_lock(path.with_suffix(".lock")):
        value = _read_conversation(identity, locked=True)
        for message in value["messages"]:
            if message.get("run_id") == run_id:
                message.pop("run_id")
                message["discarded"] = True
        _save_conversation(path, value)


def _task_work(identity, op):
    from corinthos.gui_content import pending_groups

    project = _project(op["project"])
    if not pending_groups(project["root"]):
        return {"message": "No Tasks Yet"}
    existing = next(
        (
            run
            for run in runtime.list_runs(project["root"])
            if run["skill"] == "orchestrator" and run["status"] in runtime.ACTIVE
        ),
        None,
    )
    run = existing or runtime.start(
        project["root"],
        "orchestrator",
        provider=op["payload"]["provider"],
        claims=_CLAIMS,
        instruction="Execute or resume this project's pending implementation groups using its local orchestrator skill. Read README, context and supporting context_agents before project work.",
    )
    run = _wait_run(identity, project["root"], run["run_id"])
    return {
        "run": {
            "root": project["root"],
            "project_id": project["project_id"],
            "run_id": run["run_id"],
        },
        "message": "Orchestrator execution finished; apply its isolated changes explicitly.",
    }


def perform(identity):
    op = _read_op(identity)
    _change_op(identity, status="RUNNING", phase="Preparing")
    try:
        _check_cancel(identity)
        if op["kind"] in {"create", "import"}:
            result = _creation_work(identity, op)
        elif op["kind"] == "conversation":
            result = _conversation_work(identity, op)
        elif op["kind"] == "execute":
            result = _task_work(identity, op)
        elif op["kind"] == "action":
            project, payload = _project(op["project"]), op["payload"]
            if payload["action"] == "apply":
                applied = runtime.integrate(project["root"], payload["run_id"])
                result = {
                    "run_id": payload["run_id"],
                    "applied_count": len(applied["integrated_paths"]),
                    "integrated_paths": [name[:256] for name in applied["integrated_paths"][:20]],
                }
            elif payload["action"] == "discard":
                result = runtime.discard(
                    project["root"], payload["run_id"], summary=payload["summary"]
                )
                _forget_conversation_run(project, payload["run_id"])
            else:
                runtime.cancel(project["root"], payload["run_id"])
                while runtime.get(project["root"], payload["run_id"])["status"] in runtime.ACTIVE:
                    time.sleep(0.1)
                result = {"cancelled": payload["run_id"]}
        else:
            raise IntegrityError("Unknown GUI operation.")
        _change_op(
            identity,
            status="SUCCEEDED",
            result=result,
            project=result.get("project", op.get("project")),
            message=result.get("message", "Operation completed."),
            phase="Complete",
        )
    except Exception as error:
        status = "CANCELLED" if isinstance(error, Cancelled) else "FAILED"
        message = (
            "Cancelled; no project was published."
            if isinstance(error, Cancelled)
            else str(error)[:2048]
        )
        if op["kind"] == "conversation":
            _update_message(
                op["payload"]["conversation_id"], identity, status=status, error=message
            )
        if op["kind"] in {"create", "import"}:
            try:
                _remove_stage(identity)
            except (IntegrityError, ConflictError, OSError) as cleanup:
                message += f" Owned staging was preserved: {cleanup}"
        _change_op(identity, status=status, error=message, phase="Stopped")
