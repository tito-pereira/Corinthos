# SPDX-License-Identifier: MPL-2.0
"""Project-bound local processes with isolated outputs and explicit integration."""

import codecs
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from corinthos.errors import ConflictError, IntegrityError
from corinthos.filesystem import atomic_json, file_lock, plain_path, read_json
from corinthos.providers import command_for, validate_command
from corinthos.skills import build_prompt
from corinthos.user_state import state_root
from corinthos.workspace import open_project

ACTIVE = {"STARTING", "RUNNING"}
_SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
_ID = re.compile(r"[a-f0-9]{32}")


def _directory(root):
    return Path(open_project(root)["root"]) / "context_agents/corinthos"


def _read_runs(root):
    project = open_project(root)
    directory = Path(project["root"]) / "context_agents/corinthos"
    value = read_json(directory / "runs.json", {})
    if not isinstance(value, dict) or len(value) > 64:
        raise IntegrityError("Unsupported run index.")
    for identity, run in value.items():
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or not isinstance(run, dict)
            or run.get("run_id") != identity
            or run.get("project_id") != project["project_id"]
            or run.get("root") != project["root"]
            or run.get("status") not in ACTIVE | {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
            or run.get("workspace") != str(directory / "runs" / identity / "workspace")
            or type(run.get("created_at")) not in {int, float}
            or type(run.get("integrated")) is not bool
        ):
            raise IntegrityError("Run metadata is invalid or belongs to a different project.")
        _claims(run.get("claims"))
    return value


def _run_directory(root, run_id):
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise IntegrityError("Supply a valid run identifier.")
    return _directory(root) / "runs" / run_id


def _change(root, run_id, **values):
    with file_lock(_directory(root) / "project.lock"):
        runs = _read_runs(root)
        if run_id not in runs:
            raise IntegrityError("Run not found in this project.")
        runs[run_id].update(values, updated_at=time.time())
        atomic_json(_directory(root) / "runs.json", runs)
        return runs[run_id]


def _alive(root, run):
    path = _owner_path(run["run_id"])
    if not path.exists():
        return run["status"] == "STARTING" and time.time() - run["created_at"] < 15
    try:
        with file_lock(path, blocking=False):
            return run["status"] == "STARTING" and time.time() - run["created_at"] < 15
    except ConflictError:
        return True


def list_runs(root, *, blocking=True):
    target = _directory(root)
    with file_lock(target / "project.lock", blocking=blocking):
        runs = _read_runs(root)
        changed = False
        for run in runs.values():
            if run["status"] in ACTIVE and not _alive(root, run):
                run.update(
                    status="INTERRUPTED",
                    error="Worker exited; inspect isolated outputs before resuming.",
                    updated_at=time.time(),
                )
                changed = True
        if changed:
            atomic_json(target / "runs.json", runs)
        return list(runs.values())


def get(root, run_id, *, blocking=True):
    target = _run_directory(root, run_id)
    run = next((run for run in list_runs(root, blocking=blocking) if run["run_id"] == run_id), None)
    if run is None:
        raise IntegrityError("Run not found in this project.")
    output = target / "output.txt"
    plain_path(output, file=True)
    if output.exists():
        with output.open("rb") as source:
            source.seek(max(0, output.stat().st_size - 4096))
            run = {**run, "output_tail": source.read(4096).decode("utf-8", errors="replace")}
    return run


def output(root, run_id, cursor=0):
    """Read only a selected run's bounded UTF-8 output, with a byte cursor."""
    run = get(root, run_id)
    if type(cursor) is not int or not 0 <= cursor <= 1024 * 1024:
        raise IntegrityError("Supply a bounded output cursor.")
    path = _run_directory(root, run_id) / "output.txt"
    plain_path(path, file=True)
    size = path.stat().st_size if path.exists() else 0
    if cursor > size:
        raise IntegrityError("Output cursor exceeds this run's output.")
    data = b""
    if size:
        with path.open("rb") as source:
            source.seek(cursor)
            data = source.read(4096)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data, final=run["status"] not in ACTIVE and cursor + len(data) >= size)
    next_cursor = cursor + len(data) - len(decoder.getstate()[0])
    return {
        "text": text,
        "cursor": next_cursor,
        "complete": run["status"] not in ACTIVE and next_cursor >= size,
    }


def _owner_path(run_id):
    return state_root() / "worker-owners" / (run_id + ".lock")


def _owners():
    value = read_json(state_root() / "worker-owners.json", {})
    if not isinstance(value, dict) or len(value) > 8192:
        raise IntegrityError("Unsupported worker ownership registry.")
    for identity, owner in value.items():
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or not isinstance(owner, dict)
            or set(owner) != {"root", "created_at"}
            or not isinstance(owner["root"], str)
            or not Path(owner["root"]).is_absolute()
            or type(owner["created_at"]) not in {int, float}
        ):
            raise IntegrityError("Unsupported worker ownership registry.")
    return value


def _register(root, run_id):
    path = state_root() / "worker-owners.json"
    with file_lock(path.with_suffix(".lock")):
        owners = _owners()
        if len(owners) >= 8192:
            raise IntegrityError(
                "Worker ownership registry is full; stop or recover stale workers."
            )
        owners[run_id] = {"root": str(root), "created_at": time.time()}
        atomic_json(path, owners)


@contextmanager
def worker_ownership(root, run_id):
    """Keep ownership outside movable project data, including descendant cleanup."""
    path = state_root() / "worker-owners.json"
    lease = file_lock(_owner_path(run_id), blocking=False)
    with file_lock(path.with_suffix(".lock")):
        owner = _owners().get(run_id)
        if owner is None or os.path.normcase(owner["root"]) != os.path.normcase(str(root)):
            raise IntegrityError("Worker admission expired or belongs to a different project.")
        stream = lease.__enter__()
    try:
        yield stream
    finally:
        lease.__exit__(None, None, None)
        with file_lock(path.with_suffix(".lock")):
            owners = _owners()
            owners.pop(run_id, None)
            atomic_json(path, owners)
            _owner_path(run_id).unlink(missing_ok=True)


def has_active():
    """Check ownership locks across clients/projects, never a remembered PID alone."""
    path = state_root() / "worker-owners.json"
    try:
        with file_lock(path.with_suffix(".lock")):
            owners = _owners()
            changed = active = False
            for run_id, owner in list(owners.items()):
                try:
                    with file_lock(_owner_path(run_id), blocking=False):
                        if time.time() - owner["created_at"] < 15:
                            active = True
                            continue
                except ConflictError:
                    active = True
                    continue
                del owners[run_id]
                _owner_path(run_id).unlink(missing_ok=True)
                changed = True
            if changed:
                atomic_json(path, owners)
        return active
    except (ConflictError, IntegrityError, OSError):
        # Unconfirmed ownership must not permit installation replacement.
        return True


def _claims(values):
    if not isinstance(values, list) or len(values) > 64:
        raise IntegrityError("Supply at most 64 relative write scopes.")
    result = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
        ):
            raise IntegrityError("Write scopes must be plain project-relative paths.")
        if (
            value.split("/")[0].casefold()
            not in {"repo", "task", "task_agents", "context", "context_agents", ".agents"}
            or (
                value.casefold().startswith("context_agents/corinthos")
                and value.casefold() != "context_agents/corinthos/continuity.json"
            )
            or any(part.casefold() == ".git" for part in value.split("/"))
        ):
            raise IntegrityError("Write scopes cannot target runtime state or Git metadata.")
        result.append(value.rstrip("/"))
    return sorted(set(result))


def _within(name, claims):
    name = name.casefold()
    return any(
        name == scope.casefold() or name.startswith(scope.casefold() + "/") for scope in claims
    )


def _files(root):
    result, size = {}, 0
    root = Path(root)
    for parent, directories, names in os.walk(root):
        relative = Path(parent).relative_to(root)
        directories[:] = [
            name
            for name in directories
            if name not in _SKIP
            and not (relative == Path("context_agents") and name == "corinthos")
        ]
        for name in directories:
            plain_path(Path(parent) / name)
        for name in names:
            if name in {".git"}:
                continue
            path = Path(parent) / name
            plain_path(path, file=True)
            size += path.stat().st_size
            if len(result) >= 10000 or size > 128 * 1024 * 1024:
                raise IntegrityError(
                    "Isolated workspace exceeds 10000 files or 128 MiB; remove generated artifacts first."
                )
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    continuity_path = root / "context_agents/corinthos/continuity.json"
    if continuity_path.exists():
        plain_path(continuity_path, file=True)
        if continuity_path.stat().st_size > 262144:
            raise IntegrityError("Continuity document exceeds its size limit.")
        result["context_agents/corinthos/continuity.json"] = hashlib.sha256(
            continuity_path.read_bytes()
        ).hexdigest()
    return result


def _snapshot(root, destination):
    repo = root / "repo"
    git = repo / ".git"
    if git.exists():

        def call(*args):
            result = subprocess.run(
                ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return result.stdout.strip()

        if call("status", "--porcelain"):
            raise ConflictError(
                "Commit or separately preserve repository changes before starting an isolated source run."
            )
        commit = call("rev-parse", "HEAD")
        subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo.as_posix()}",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--",
                str(repo),
                str(destination / "repo"),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(destination / "repo"), "checkout", "--quiet", "--detach", commit],
            check=True,
            capture_output=True,
            timeout=30,
        )
    for name in _files(root):
        if git.exists() and name.startswith("repo/"):
            continue
        source, target = root / name, destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for name in ("repo", "context", "task", "task_agents", "context_agents", ".agents/skills"):
        (destination / name).mkdir(parents=True, exist_ok=True)
    identity = open_project(root)
    atomic_json(
        destination / "context_agents/corinthos/state.json", {**identity, "root": str(destination)}
    )
    return _files(destination)


def start(
    root,
    skill,
    *,
    provider=None,
    command=None,
    group=None,
    instruction="",
    claims=None,
    timeout=3600,
    prepared_prompt=None,
):
    project = open_project(root)
    root = Path(project["root"])
    command = validate_command(command if command is not None else command_for(provider))
    claims = _claims([] if claims is None else claims)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 86400:
        raise IntegrityError("Timeout must be between 1 and 86400 seconds.")
    prompt = (
        prepared_prompt
        if prepared_prompt is not None
        else build_prompt(root, skill, group=group, instruction=instruction)
    )
    if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 262144:
        raise IntegrityError("Prepared prompt exceeds its bounded UTF-8 limit.")
    run_id = uuid.uuid4().hex
    target = _run_directory(root, run_id)
    with file_lock(_directory(root) / "project.lock"):
        runs = _read_runs(root)
        if len(runs) >= 64:
            raise IntegrityError(
                "Retain results and discard terminal runs before admitting more than 64 runs."
            )
        for run in runs.values():
            if run["status"] in ACTIVE and any(
                _within(scope, run["claims"]) or _within(existing, claims)
                for scope in claims
                for existing in run["claims"]
            ):
                raise ConflictError("Write scopes overlap an active run.")
        target.mkdir(parents=True)
        destination = target / "workspace"
        destination.mkdir()
        (target / "prompt.txt").write_text(
            f"Isolated workspace root: {destination}\nUse this root for local commands; canonical integration is separate.\n"
            + prompt,
            encoding="utf-8",
        )
        atomic_json(target / "request.json", {"command": command, "timeout": timeout})
        now = time.time()
        runs[run_id] = {
            "run_id": run_id,
            "project_id": project["project_id"],
            "root": str(root),
            "skill": skill,
            "group": group,
            "provider": provider or "command",
            "claims": claims,
            "status": "STARTING",
            "created_at": now,
            "updated_at": now,
            "workspace": str(destination),
            "error": None,
            "output": str(target / "output.txt"),
            "integrated": False,
        }
        atomic_json(_directory(root) / "runs.json", runs)
    try:
        _register(root, run_id)
        flags = (
            {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        subprocess.Popen(
            [sys.executable, "-m", "corinthos.worker", "--root", str(root), "--run-id", run_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **flags,
        )
    except (IntegrityError, OSError) as error:
        _change(root, run_id, status="FAILED", error=str(error))
        raise
    return runs[run_id]


def prepare(root, run_id):
    """Prepare the admitted workspace in its owned worker, never the RPC thread."""
    target = _run_directory(root, run_id)
    run = get(root, run_id)
    if (target / "cancel.json").exists():
        return False
    before = _snapshot(Path(run["root"]), Path(run["workspace"]))
    atomic_json(target / "baseline.json", before, max_bytes=2 * 1024 * 1024)
    return not (target / "cancel.json").exists()


def cancel(root, run_id):
    run = get(root, run_id)
    if run["status"] in ACTIVE:
        atomic_json(_run_directory(root, run_id) / "cancel.json", {"cancel": True})
    return run


def integrate(root, run_id):
    """Validate every output and baseline before copying any provider changes."""
    root = Path(open_project(root)["root"])
    with file_lock(_directory(root) / "project.lock"):
        runs = _read_runs(root)
        run = runs.get(run_id)
        if run is None or run["status"] != "SUCCEEDED" or run["integrated"]:
            raise ConflictError("Only a successful, not-yet-integrated run can be integrated.")
        before = read_json(
            _run_directory(root, run_id) / "baseline.json", max_bytes=2 * 1024 * 1024
        )
        workspace = _run_directory(root, run_id) / "workspace"
        after = _files(workspace)
        changed = [
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        ]
        for name in changed:
            if not _within(name, run["claims"]):
                raise ConflictError(
                    "Provider changed a path outside its declared write scopes; outputs remain isolated."
                )
            if any(
                other["run_id"] != run_id
                and other["status"] in ACTIVE
                and _within(name, other["claims"])
                for other in runs.values()
            ):
                raise ConflictError("An active run owns an integration destination.")
            destination = root / name
            plain_path(destination, file=True)
            actual = (
                hashlib.sha256(destination.read_bytes()).hexdigest()
                if destination.exists()
                else None
            )
            if actual != before.get(name):
                raise ConflictError(
                    "Destination changed after the run began; outputs remain isolated."
                )
        # All targets are checked before mutation; an I/O failure is reported with outputs retained.
        for name in changed:
            destination = root / name
            if name in after:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(workspace / name, destination)
            else:
                destination.unlink()
        run.update(integrated=True, updated_at=time.time())
        atomic_json(_directory(root) / "runs.json", runs)
    return {"run_id": run_id, "integrated_paths": sorted(changed)}


def discard(root, run_id, *, summary):
    from corinthos.continuity import consolidate

    run = get(root, run_id)
    if run["status"] in ACTIVE:
        raise ConflictError("Cancel and wait for a worker before discarding it.")
    consolidate(root, summary)
    with file_lock(_directory(root) / "project.lock"):
        runs = _read_runs(root)
        target = _run_directory(root, run_id)
        for path in target.rglob("*"):
            plain_path(path, file=not path.is_dir())
        shutil.rmtree(target)
        del runs[run_id]
        atomic_json(_directory(root) / "runs.json", runs)
    return {"discarded": run_id}
