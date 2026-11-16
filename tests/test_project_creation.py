# SPDX-License-Identifier: MPL-2.0
"""Creation exercises real private initialization, durable retries and passive projections."""

import gc
import json
import subprocess
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_external_import import repository as repository

from corinthos import project_creation as creation
from corinthos.application import command, workspace
from corinthos.bootstrap import initialize_private
from corinthos.contracts import ContractStore
from corinthos.external_import.service import ExternalProjectImportService, _new_intent
from corinthos.project_registry import ProjectRegistryService


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    user = tmp_path / "user"
    monkeypatch.setattr("corinthos.user_state.state_root", lambda: user)
    return user


def _bytes(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _forbidden(*_args, **_kwargs):
    pytest.fail("Creation and passive projections must not execute/discover providers or publish.")


@pytest.mark.parametrize("kind", ["new", "empty", "nonempty"])
def test_private_creation_preserves_files_and_has_no_authority(tmp_path, monkeypatch, kind):
    root = tmp_path / "Projects" / "São João Notes"
    if kind != "new":
        root.mkdir(parents=True)
    if kind == "nonempty":
        for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "user.bin"):
            (root / name).write_bytes(b"Owner bytes\x00\xff\r\n")
    before = _bytes(root)
    for method in ("discover", "probe", "invoke"):
        monkeypatch.setattr(f"corinthos.adapters.service.AdapterManager.{method}", _forbidden)
    monkeypatch.setattr("corinthos.providers.refresh_providers", _forbidden)
    monkeypatch.setattr("corinthos.providers.inspect_providers", _forbidden, raising=False)
    monkeypatch.setattr(ContractStore, "publish", _forbidden)
    result = workspace.project_create(str(root), "  João's research  ")
    assert result["display_name"] == "João's research"
    assert result["initialized"] and result["storage"] == "ABSENT"
    assert result["project_id"] is result["active_project"] is result["active_workflow"] is None
    assert result["validation"] is None
    assert {p: (root / p).read_bytes() for p in before} == before
    assert set(_bytes(root)) - set(before) == {
        Path(".corinthos/bootstrap.json"),
        Path(".corinthos/project-create.json"),
    }
    assert not (root / ".corinthos/state.db").exists()
    saved = _bytes(root)
    workspace.project_open(str(root))
    assert workspace.project_summary(str(root))["display_name"] == "João's research"
    assert ProjectRegistryService().list()[0].display_name == "João's research"
    assert command.command_overview()["projects"][0]["display_name"] == "João's research"
    assert command.command_project(str(root))["display_name"] == "João's research"
    assert _bytes(root) == saved


@pytest.mark.parametrize(
    "name", [None, 1, "", "  ", "x" * 129, "bad\nname", "bad\x00", "bad\u200b", "\ud800"]
)
def test_invalid_names_do_not_mutate_project(tmp_path, name):
    root = tmp_path / "untouched"
    with pytest.raises(ValueError):
        workspace.project_create(str(root), name)
    assert not root.exists()


@pytest.mark.parametrize("root", ["relative", "", "a/../b", "x\x00", 42, None])
def test_invalid_roots_fail_before_initialization(root):
    with pytest.raises(ValueError):
        workspace.project_create(root, "Name")


@pytest.mark.parametrize(
    "state", ["unknown", "corrupt", "unsupported", "legacy", "incomplete-import", "bad-intent"]
)
def test_unverified_private_state_is_preserved(tmp_path, state):
    root = tmp_path / "project"
    private = root / (".nukytera" if state == "legacy" else ".corinthos")
    private.mkdir(parents=True)
    if state == "unknown":
        (private / "owner.bin").write_bytes(b"owner")
    elif state == "corrupt":
        (private / "state.db").write_bytes(b"not sqlite")
    elif state == "unsupported":
        initialize_private(root)
        profile = private / "bootstrap.json"
        value = json.loads(profile.read_bytes())
        value["version"] = 999
        profile.write_text(json.dumps(value))
    elif state == "incomplete-import":
        (private / "external-import-intent.json").write_bytes(b"{}")
    elif state == "bad-intent":
        initialize_private(root)
        (private / "project-create.json").write_bytes(b"invalid")
    before = _bytes(root)
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Name")
    assert error.value.creation_code == "STATE_UNAVAILABLE"
    assert _bytes(root) == before


@pytest.mark.parametrize("database", [False, True])
@pytest.mark.parametrize("state", ["INCOMPLETE", "INVALID"])
def test_real_interrupted_import_cannot_be_adopted_by_creation(tmp_path, database, state):
    root = tmp_path / "interrupted-import"
    root.mkdir()
    _new_intent(root, "a" * 64, [])
    initialize_private(root)
    if database:
        ContractStore(root)
    if state == "INVALID":
        (root / ".corinthos/external-import-intent.json").write_bytes(b'{"version":999}')
    assert ExternalProjectImportService().status(root)["status"] == state
    before = _bytes(root)
    assert ProjectRegistryService().paths() == []
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Keep import ownership")
    assert error.value.creation_code == "STATE_UNAVAILABLE"
    assert ExternalProjectImportService().status(root)["status"] == state
    assert _bytes(root) == before and ProjectRegistryService().paths() == []


def test_completed_import_remains_supported_and_preserves_owned_bytes(repository):
    importer = ExternalProjectImportService()
    observation = importer.inspect(repository)
    confirmations = [row["code"] for row in observation["ambiguities"] if row["required"]]
    imported = importer.commit(repository, observation["discovery_digest"], confirmations)
    assert imported["status"] == "IMPORTED"
    before = _bytes(repository)
    result = workspace.project_create(str(repository), "Existing imported project")
    assert result["initialized"] and result["project_id"] == imported["project_id"]
    assert importer.status(repository) == imported
    assert {path: (repository / path).read_bytes() for path in before} == before
    assert set(_bytes(repository)) - set(before) == {Path(".corinthos/project-create.json")}
    assert ProjectRegistryService().list()[0].display_name == "Existing imported project"


@pytest.mark.parametrize("part", ["stage", "private"])
def test_replaced_staging_sources_are_preserved_without_publication(tmp_path, monkeypatch, part):
    root = tmp_path / "selected"
    root.mkdir()
    (root / "owner.bin").write_bytes(b"Owner bytes")
    original = creation.initialize_private
    changed = {}

    def replace(stage):
        original(stage)
        target = stage if part == "stage" else stage / ".corinthos"
        moved = target.with_name(target.name + "-moved")
        target.rename(moved)
        if part == "stage":
            stage.mkdir()
        original(stage)
        (stage / ".corinthos/foreign.bin").write_bytes(b"Foreign bytes")
        changed.update(
            target=target, moved=moved, target_bytes=_bytes(target), moved_bytes=_bytes(moved)
        )

    monkeypatch.setattr(creation, "initialize_private", replace)
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Intended name")
    assert error.value.creation_code == "STATE_UNAVAILABLE"
    assert not (root / ".corinthos").exists()
    assert (root / "owner.bin").read_bytes() == b"Owner bytes"
    assert _bytes(changed["target"]) == changed["target_bytes"]
    assert _bytes(changed["moved"]) == changed["moved_bytes"]
    assert ProjectRegistryService().paths() == []


@pytest.mark.parametrize("change", ["missing", "changed"])
def test_creation_requires_matching_final_intent_before_registration(tmp_path, monkeypatch, change):
    root = tmp_path / "postcondition"
    original = creation._stage

    def tamper(*args):
        result = original(*args)
        intent = root / ".corinthos/project-create.json"
        if change == "missing":
            intent.unlink()
        else:
            intent.write_bytes(b'{"display_name":"Other name","version":1}')
        return result

    monkeypatch.setattr(creation, "_stage", tamper)
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Intended name")
    assert error.value.creation_code == "STATE_UNAVAILABLE"
    assert (root / ".corinthos/bootstrap.json").is_file()
    assert ProjectRegistryService().paths() == []


@pytest.mark.parametrize("replacement", ["private", "sibling-file"])
def test_postpublication_cleanup_preserves_unexpected_stage_data_after_gc(
    tmp_path, monkeypatch, replacement
):
    root = tmp_path / "published"
    root.mkdir()
    (root / "owner.bin").write_bytes(b"Owner bytes")
    promote = creation._promote
    allocate = creation.TemporaryDirectory
    allocated = []
    changed = {}

    def retain_temporary(*args, **kwargs):
        temporary = allocate(*args, **kwargs)
        allocated.append(temporary)
        return temporary

    def add_foreign_after_publication(source, destination):
        promote(source, destination)
        if replacement == "private":
            source.mkdir()
            foreign = source / "foreign.bin"
        else:
            foreign = source.parent / "foreign.bin"
        foreign.write_bytes(b"Foreign bytes")
        changed["foreign"] = foreign

    monkeypatch.setattr(creation, "TemporaryDirectory", retain_temporary)
    monkeypatch.setattr(creation, "_promote", add_foreign_after_publication)
    result = workspace.project_create(str(root), "Published name")
    assert result["initialized"] and result["display_name"] == "Published name"
    assert changed["foreign"].read_bytes() == b"Foreign bytes"
    assert (root / "owner.bin").read_bytes() == b"Owner bytes"
    assert not (root / ".corinthos/foreign.bin").exists()
    assert json.loads((root / ".corinthos/project-create.json").read_bytes()) == {
        "version": 1,
        "display_name": "Published name",
    }
    assert len(ProjectRegistryService().list()) == 1
    references = [weakref.ref(temporary) for temporary in allocated]
    allocated.clear()
    gc.collect()
    assert all(reference() is None for reference in references)
    assert changed["foreign"].read_bytes() == b"Foreign bytes"


def test_bootstrap_and_current_contract_bytes_survive_creation(tmp_path):
    from test_validation_repository import _load

    from corinthos.contracts import PublishContext, approval_request

    root = tmp_path / "current"
    root.mkdir()
    initialize_private(root)
    store = ContractStore(root)
    document = _load("project")
    proof = approval_request(document)
    origin = document["metadata"]["provenance"]
    store.publish(
        document,
        context=PublishContext(origin["engine"], origin["actor_id"], lambda value: value == proof),
    )
    before = _bytes(root)
    result = workspace.project_create(str(root), "Current project")
    assert result["active_project"] is not None
    assert {path: (root / path).read_bytes() for path in before} == before
    saved = _bytes(root)
    workspace.project_create(str(root), "Current project")
    assert _bytes(root) == saved


def test_registration_failure_recovers_after_restart_without_new_identity(tmp_path, monkeypatch):
    root = tmp_path / "recover"
    original = ProjectRegistryService.remember
    monkeypatch.setattr(
        ProjectRegistryService,
        "remember",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Durable name")
    assert error.value.creation_code == "REGISTRY_INCOMPLETE"
    before = _bytes(root)
    monkeypatch.setattr(ProjectRegistryService, "remember", original)
    with pytest.raises(creation.ProjectCreationError) as conflicting:
        workspace.project_create(str(root), "Different name")
    assert conflicting.value.creation_code == "CONFLICT"
    assert _bytes(root) == before
    user = tmp_path / "user"
    script = "from pathlib import Path; from corinthos import user_state; from corinthos.application.workspace import project_create; import sys; user_state.state_root=lambda:Path(sys.argv[2]); print(project_create(sys.argv[1], 'Durable name')['display_name'])"
    recovered = subprocess.run(
        [sys.executable, "-c", script, str(root), str(user)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert recovered.stdout.strip() == "Durable name"
    assert _bytes(root) == before and len(ProjectRegistryService().list()) == 1


@pytest.mark.parametrize("names", [("One name", "One name"), ("One name", "Another name")])
def test_same_root_processes_serialize_matching_and_conflicting_names(tmp_path, names):
    root, user = tmp_path / "concurrent", tmp_path / "user"
    script = "from pathlib import Path; from corinthos import user_state; from corinthos.application.workspace import project_create; import sys; user_state.state_root=lambda:Path(sys.argv[2]); print(project_create(sys.argv[1], sys.argv[3])['display_name'])"

    def run(name):
        return subprocess.run(
            [sys.executable, "-c", script, str(root), str(user), name],
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, names))
    assert sum(r.returncode == 0 for r in results) == (2 if names[0] == names[1] else 1), [
        r.stderr for r in results
    ]
    before = _bytes(root)
    assert run("Conflict").returncode != 0
    assert _bytes(root) == before and len(ProjectRegistryService().list()) == 1


def test_interrupted_staging_never_publishes_partial_private_state(tmp_path, monkeypatch):
    root = tmp_path / "interrupted"
    original = creation.initialize_private

    def interrupted(stage):
        original(stage)
        raise OSError("Interrupted before publication")

    monkeypatch.setattr(creation, "initialize_private", interrupted)
    with pytest.raises(creation.ProjectCreationError):
        workspace.project_create(str(root), "Recover")
    assert root.is_dir() and not (root / ".corinthos").exists()
    monkeypatch.setattr(creation, "initialize_private", original)
    assert workspace.project_create(str(root), "Recover")["initialized"]


def test_root_replacement_before_publication_preserves_replacement(tmp_path, monkeypatch):
    root, moved = tmp_path / "selected", tmp_path / "moved"
    root.mkdir()
    (root / "owner.txt").write_bytes(b"original")
    original = creation.initialize_private

    def replace(stage):
        original(stage)
        root.rename(moved)
        root.mkdir()
        (root / "replacement.txt").write_bytes(b"replacement")

    monkeypatch.setattr(creation, "initialize_private", replace)
    with pytest.raises(creation.ProjectCreationError):
        workspace.project_create(str(root), "Name")
    assert _bytes(root) == {Path("replacement.txt"): b"replacement"}
    assert (moved / "owner.txt").read_bytes() == b"original"
    assert not (moved / ".corinthos").exists()
    assert all(
        path.parts[0].startswith(".corinthos-create-") or path == Path("owner.txt")
        for path in _bytes(moved)
    )


def test_writable_root_does_not_require_sibling_staging(tmp_path, monkeypatch):
    root = tmp_path / "writable-root"
    root.mkdir()
    (root / ".gitignore").write_bytes(b"Owner ignore rules\r\n")
    original = creation.TemporaryDirectory

    def deny_parent(*args, **kwargs):
        if Path(kwargs["dir"]) != root:
            raise PermissionError("The selected root's parent cannot allocate sibling staging.")
        return original(*args, **kwargs)

    monkeypatch.setattr(creation, "TemporaryDirectory", deny_parent)
    result = workspace.project_create(str(root), "Local stage")
    assert result["initialized"]
    assert (root / ".gitignore").read_bytes() == b"Owner ignore rules\r\n"
    assert not list(root.glob(".corinthos-create-*"))


def test_access_failure_and_redirected_roots_leave_bytes_untouched(tmp_path, monkeypatch):
    root = tmp_path / "denied"
    root.mkdir()
    (root / "owner").write_bytes(b"owner")
    monkeypatch.setattr(creation, "_stage", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(creation.ProjectCreationError) as error:
        workspace.project_create(str(root), "Name")
    assert error.value.creation_code == "ACCESS_DENIED"
    assert _bytes(root) == {Path("owner"): b"owner"}
    link = tmp_path / "redirect"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable on this Windows account.")
    with pytest.raises(creation.ProjectCreationError):
        workspace.project_create(str(link), "Name")
    assert _bytes(root) == {Path("owner"): b"owner"}


@pytest.mark.parametrize("version", [1, 2])
def test_old_registries_are_passive_and_named_reopen_preserves_other_entries(
    tmp_path, isolated_user, version
):
    root, missing = tmp_path / "directory", tmp_path / "gone"
    root.mkdir()
    initialize_private(root)
    isolated_user.mkdir()
    root_key, date_key = (
        ("path", "last_opened_at") if version == 1 else ("root", "last_accessed_at")
    )
    path = isolated_user / "recent-projects.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "projects": [
                    {root_key: str(p), date_key: "2026-01-01T00:00:00Z"} for p in (missing, root)
                ],
            }
        )
    )
    before = path.read_bytes()
    assert ProjectRegistryService().list()[1].display_name == "directory"
    assert path.read_bytes() == before
    workspace.project_create(str(root), "Explicit name")
    workspace.project_open(str(root))
    entries = ProjectRegistryService().list()
    assert [(e.root, e.display_name) for e in entries] == [
        (str(root), "Explicit name"),
        (str(missing), "gone"),
    ]
    assert len(entries) == 2 and entries[1].availability == "MISSING"
