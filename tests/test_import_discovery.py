# SPDX-License-Identifier: MPL-2.0
"""Exercise discovery against a real isolated Git and irregular worktrees."""

import json
import os
import subprocess
from hashlib import sha256

import pytest

from corinthos.external_import import discovery
from corinthos.external_import.service import ExternalProjectImportService


def _git(root, *args, data=None):
    return subprocess.run(
        ["git", *args], cwd=root, input=data, capture_output=True, check=True
    ).stdout


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _commit(root):
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic import fixture")


@pytest.fixture(params=("sha1", "sha256"))
def worktree(tmp_path, request):
    root = tmp_path / "worktree"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main", f"--object-format={request.param}")
    _git(root, "config", "user.name", "Synthetic import fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, "README.md", "# Shipping fixture\n")
    _write(root, "src/café.py", "load = 1\n")
    _write(root, "tests/test_shipping.py", "def test_fixture(): pass\n")
    _write(root, ".gitignore", "ignored/\n")
    _write(root, ".env", "PASSWORD=private_fixture_marker\n")
    _write(root, "image.png", b"\x89PNG\0fixture_image")
    _write(root, "large.txt", b"x" * (discovery.MAX_FILE_BYTES + 1))
    _write(
        root,
        "package.json",
        json.dumps(
            {
                "name": "shipping",
                "workspaces": ["tools/reader", "../private", "tools/*"],
                "dependencies": {"small-lib": "file:private_dependency_marker"},
                "scripts": {"test": "echo private_script_marker"},
            }
        ),
    )
    _write(root, "tools/reader/package.json", '{"name":"reader"}')
    _write(root, ".gitattributes", "*.py filter=fixture\n")
    _git(root, "add", "--all")
    _commit(root)
    return root


def _codes(inventory):
    return {item["code"] for item in inventory["ambiguities"]}


def _fingerprint(root):
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_real_git_manifest_facts_and_read_only_inventory(worktree):
    before = _fingerprint(worktree)
    root, observed = discovery.scan(worktree)
    assert root == worktree.resolve()
    assert observed["head"] == _git(root, "rev-parse", "HEAD").decode().strip()
    assert (
        observed["object_format"]
        == _git(root, "rev-parse", "--show-object-format").decode().strip()
    )
    assert len(observed["head"]) == {"sha1": 40, "sha256": 64}[observed["object_format"]]
    assert observed["branch"] == "main"
    assert (observed["tracked_count"], observed["staged_count"]) == (10, 0)
    assert observed["commit_allowed"]
    assert not observed["dirty"]
    assert observed["docs"] == ["README.md"]
    assert observed["tests"] == ["tests/test_shipping.py"]
    assert observed["languages"] == {"Python": 2}
    assert observed["manifests"][0] == {
        "path": "package.json",
        "ecosystem": "node",
        "name": "shipping",
        "dependencies": ["small-lib"],
        "scripts": ["test"],
        "members": ["tools/reader"],
        "parsed": True,
    }
    assert observed["exclusions"] == ["BINARY", "CONTENT_BUDGET", "SENSITIVE_PATH"]
    source = next(row for row in observed["files"] if row["path"] == "src/café.py")
    assert source["index_oid"] == _git(root, "rev-parse", ":src/café.py").decode().strip()
    assert len(source["index_oid"]) == len(observed["head"])
    assert source["worktree_sha256"] == sha256(b"load = 1\n").hexdigest()
    assert source["state"] == "CLEAN"
    assert {"MULTIPLE_MANIFESTS", "MULTIPLE_MODULE_ROOTS", "SENSITIVE_EXCLUDED"} <= _codes(observed)
    serialized = json.dumps(observed)
    for value in (
        ".env",
        "private_fixture_marker",
        "private_dependency_marker",
        "private_script_marker",
    ):
        assert value not in serialized
    assert str(worktree) not in serialized
    assert discovery.tracked_paths(root) == {
        ".gitattributes",
        ".gitignore",
        "README.md",
        "image.png",
        "large.txt",
        "package.json",
        "src/café.py",
        "tests/test_shipping.py",
        "tools/reader/package.json",
    }
    assert _fingerprint(worktree) == before
    assert discovery.discovery_digest(observed) == observed["discovery_digest"]
    assert discovery.scan(worktree)[1] == observed


def test_configuration_environment_and_executable_paths_do_not_run_project_code(
    worktree, monkeypatch
):
    hook = _write(worktree, "fixture-hook.sh", "#!/bin/sh\necho invoked > invoked.marker\n")
    hook.chmod(0o700)
    fake_git = "git.exe" if os.name == "nt" else "git"
    local_bin = _write(worktree, f"fake-bin/{fake_git}", "#!/bin/sh\nexit 91\n")
    local_bin.chmod(0o700)
    outside_config = _write(worktree.parent, "excluded.config", "[invalid\n")
    expected_format = _git(worktree, "rev-parse", "--show-object-format").decode().strip()
    _git(worktree, "config", "core.hooksPath", str(worktree))
    _git(worktree, "config", "core.fsmonitor", "./fixture-hook.sh")
    _git(worktree, "config", "filter.fixture.clean", "./fixture-hook.sh")
    _git(worktree, "config", "filter.fixture.process", "./fixture-hook.sh")
    _git(worktree, "config", "filter.fixture.smudge", "./fixture-hook.sh")
    _git(worktree, "config", "include.path", str(outside_config))
    monkeypatch.setenv("GIT_DIR", str(worktree / "nonexistent.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(worktree / "other"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "bogus")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(outside_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(outside_config))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256" if expected_format == "sha1" else "sha1")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(worktree / "objects"))
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(local_bin.parent), "fake-bin", os.environ["PATH"]))
    )
    for variable in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(variable, str(worktree))
    before = _fingerprint(worktree)
    _, observed = discovery.scan(worktree)
    assert observed["head"] is not None
    assert observed["object_format"] == expected_format
    assert observed["untracked_count"] == 2
    assert not (worktree / "invoked.marker").exists()
    assert _fingerprint(worktree) == before


def test_discovery_identity_changes_for_worktree_index_and_untracked_paths(worktree):
    _, before = discovery.scan(worktree)
    _write(worktree, ".corinthos/private.txt", "unpublished managed state")
    _, managed = discovery.scan(worktree)
    assert managed["existing_state"]
    assert managed["discovery_digest"] == before["discovery_digest"]
    _write(worktree, "src/café.py", "load = 2\n")
    _, modified = discovery.scan(worktree)
    assert modified["discovery_digest"] != before["discovery_digest"]
    assert "DIRTY_TRACKED" in _codes(modified)
    source = next(row for row in modified["files"] if row["path"] == "src/café.py")
    assert source["state"] == "MODIFIED"
    assert source["worktree_sha256"] == sha256(b"load = 2\n").hexdigest()
    assert discovery.read_indexed_text(worktree, "src/café.py") == (
        "load = 2\n",
        sha256(b"load = 2\n").hexdigest(),
    )
    _git(worktree, "add", "--", "src/café.py")
    _, staged = discovery.scan(worktree)
    assert staged["staged_count"] == 1
    assert staged["discovery_digest"] != modified["discovery_digest"]
    assert next(row for row in staged["files"] if row["path"] == "src/café.py")["state"] == "CLEAN"
    _write(worktree, "ignored/secret.tmp", "untracked contents excluded")
    _, untracked = discovery.scan(worktree)
    assert untracked["untracked_count"] == 1
    assert "UNTRACKED_EXCLUDED" in _codes(untracked)
    assert untracked["discovery_digest"] != staged["discovery_digest"]
    _write(worktree, "ignored/secret.tmp", "untracked contents remain excluded")
    assert discovery.scan(worktree)[1] == untracked


def test_private_large_and_redirected_content_cannot_enter_discovery(worktree):
    assert discovery.read_indexed_text(worktree, "../unrelated") is None
    assert discovery.read_indexed_text(worktree, ".env") is None
    assert discovery.read_indexed_text(worktree, "image.png") is None
    assert discovery.read_indexed_text(worktree, "large.txt") is None
    assert discovery.read_indexed_text(worktree, "README.md", max_bytes=1) is None
    _write(worktree, ".env", "PASSWORD=long_modified_private_fixture_marker\n")
    _, excluded = discovery.scan(worktree)
    assert not excluded["commit_allowed"]
    assert "UNVERIFIED_TRACKED" in _codes(excluded)
    assert "long_modified_private_fixture_marker" not in json.dumps(excluded)
    _write(worktree, "src/.git/HEAD", "boundary marker that must never be read")
    _, nested = discovery.scan(worktree)
    assert "NESTED_GIT" in _codes(nested)
    source = next(row for row in nested["files"] if row["path"] == "src/café.py")
    assert source["state"] == "UNVERIFIED"
    assert discovery.read_indexed_text(worktree, "src/café.py") is None


def test_metadata_only_dirty_file_is_blocked_when_edit_restores_mtime(worktree):
    path = worktree / "image.png"
    before = path.stat()
    original = path.read_bytes()
    path.write_bytes(b"!" + original[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    _, observed = discovery.scan(worktree)
    file = next(row for row in observed["files"] if row["path"] == "image.png")
    assert file["state"] == "UNVERIFIED"
    assert file["worktree_sha256"] is None
    assert not observed["commit_allowed"]


def test_missing_parent_and_gitlink_are_metadata_only(worktree):
    (worktree / "src/café.py").unlink()
    (worktree / "src").rmdir()
    head = _git(worktree, "rev-parse", "HEAD").decode().strip()
    _git(worktree, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/linked")
    _write(worktree, "vendor/linked/README.md", "outside module contents")
    _, observed = discovery.scan(worktree)
    files = {row["path"]: row for row in observed["files"]}
    assert files["src/café.py"]["state"] == "MISSING"
    assert files["vendor/linked"]["kind"] == "SUBMODULE"
    assert files["vendor/linked"]["content_excluded"]
    assert observed["untracked_count"] == 0
    assert "SUBMODULES" in _codes(observed)
    assert "vendor/linked" not in discovery.tracked_paths(worktree)


@pytest.mark.parametrize(
    ("relative", "stage_deletion"), [(".corinthos/state.db", False), (".CORINTHOS", True)]
)
def test_tracked_private_state_blocks_import_before_any_target_write(
    worktree, relative, stage_deletion
):
    reserved = _write(worktree, relative, "repository-owned fixture content")
    _git(worktree, "add", "--", relative)
    _commit(worktree)
    reserved.unlink()
    if reserved.parent != worktree:
        reserved.parent.rmdir()
    if stage_deletion:
        _git(worktree, "add", "-u", "--", relative)

    importer = ExternalProjectImportService()
    before = _fingerprint(worktree)
    observed = importer.inspect(worktree)
    assert observed["dirty"]
    assert not observed["commit_allowed"]
    assert "TRACKED_PRIVATE_STATE" in _codes(observed)
    assert relative not in json.dumps(observed)
    confirmations = [item["code"] for item in observed["ambiguities"] if item["required"]]
    outcome = importer.commit(worktree, observed["discovery_digest"], confirmations=confirmations)
    assert outcome["status"] == "REJECTED"
    assert outcome["error"] == "UNSAFE_REPOSITORY_STATE"
    assert not (worktree / ".corinthos").exists()
    assert not (worktree / ".CORINTHOS").exists()
    assert _fingerprint(worktree) == before


def test_refuses_linked_metadata_alternates_and_inventory_over_budget(worktree, monkeypatch):
    with pytest.raises(discovery.ImportDiscoveryError, match="exact root"):
        discovery.scan(worktree / "src")
    alternate = _write(worktree, ".git/objects/info/alternates", "/unloaded/local/object/path\n")
    with pytest.raises(discovery.ImportDiscoveryError, match="alternate"):
        discovery.scan(worktree)
    alternate.unlink()
    monkeypatch.setattr(discovery, "MAX_TRACKED_PATHS", 1)
    with pytest.raises(discovery.ImportDiscoveryError, match="inventory exceeds"):
        discovery.scan(worktree)


def test_packed_objects_packed_refs_and_detached_head(worktree):
    _, original = discovery.scan(worktree)
    _git(worktree, "pack-refs", "--all")
    _git(worktree, "gc", "--quiet")
    assert not (worktree / ".git/refs/heads/main").exists()
    assert list((worktree / ".git/objects/pack").glob("*.pack"))
    _, packed = discovery.scan(worktree)
    assert packed == original
    _git(worktree, "switch", "--quiet", "--detach", original["head"])
    _, detached = discovery.scan(worktree)
    assert detached["head"] == original["head"]
    assert detached["branch"] is None
    assert detached["files"] == original["files"]
    assert detached["discovery_digest"] != original["discovery_digest"]
    assert discovery.scan(worktree)[1] == detached


def test_unborn_object_format_is_explicit_before_any_oid(worktree):
    object_format = _git(worktree, "rev-parse", "--show-object-format").decode().strip()
    unborn = worktree.parent / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q", "-b", "main", f"--object-format={object_format}")
    _, observed = discovery.scan(unborn)
    assert observed["object_format"] == object_format
    assert observed["head"] is None
    assert observed["files"] == []
    assert discovery.scan(unborn)[1] == observed


def test_format_rejects_unknown_inconsistent_ambiguous_and_oversized_config(worktree):
    config = worktree / ".git/config"
    before = config.read_bytes()
    for source in (
        b"[core]\n repositoryformatversion=1\n[extensions]\n objectformat=sha-invalid\n",
        b"[core]\n repositoryformatversion=0\n[extensions]\n objectformat=sha256\n",
        b"[core]\n repositoryformatversion=1\n[extensions]\n objectformat=sha256\n compatobjectformat=sha1\n",
        b"[core]\n repositoryformatversion=0\n repositoryFormatVersion=0\n",
        b"[core]\n repositoryformatversion=\n",
        b"[core\n",
        b"[core]\n repositoryformatversion=0\0\n",
    ):
        config.write_bytes(source)
        with pytest.raises(discovery.ImportDiscoveryError):
            discovery.scan(worktree)
    config.write_bytes(before + b"#" * (discovery._MAX_GIT_CONFIG + 1))
    with pytest.raises(discovery.ImportDiscoveryError, match="size limit"):
        discovery.scan(worktree)


def test_rejects_head_ref_of_other_object_format(worktree):
    oid = _git(worktree, "rev-parse", "HEAD").decode().strip()
    wrong_width = b"a" * (64 if len(oid) == 40 else 40) + b"\n"
    ref = worktree / ".git/refs/heads/main"
    ref.write_bytes(wrong_width)
    with pytest.raises(discovery.ImportDiscoveryError, match="unsupported local reference"):
        discovery.scan(worktree)
    ref.unlink()
    (worktree / ".git/packed-refs").write_bytes(wrong_width.rstrip() + b" refs/heads/main\n")
    with pytest.raises(discovery.ImportDiscoveryError, match="unsupported packed reference"):
        discovery.scan(worktree)
    (worktree / ".git/HEAD").write_bytes(wrong_width)
    with pytest.raises(discovery.ImportDiscoveryError, match="detached commit"):
        discovery.scan(worktree)
