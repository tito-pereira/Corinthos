# SPDX-License-Identifier: MPL-2.0
"""Current signed candidates, shared durable operations and readonly health admission."""

import io
import json
import os
import stat
import sys
import time
import zipfile
from threading import Event, Thread

import pytest
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_sidecar import _call, _transcript
from test_sidecar import wire as wire

from corinthos import cli, updater, user_state
from corinthos.application import sidecar
from corinthos.update_manifest import (
    UpdateError,
    extract_payload,
    hash_file,
    relative_path,
    strict_json,
    verify_manifest,
    verify_payload,
)
from corinthos.update_release import create_bundle
from corinthos.update_release import main as release_main


@pytest.fixture
def signed_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in (
        "bin/corinthos.exe",
        "bin/corinthos-desktop.exe",
        "runtime/python.exe",
        "app/corinthos/__init__.py",
    ):
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Current-code payload fixture: " + name.encode())
    release = {
        "product_version": "0.1.1",
        "python_package_version": "0.1.1",
        "workspace_format": 1,
        "rpc_protocol": 2,
        "desktop_identifier": "org.corinthos.desktop",
        "build_platform": "windows",
        "architecture": "x86_64-pc-windows-msvc",
        "python_runtime": "3.14.10",
    }
    (candidate / "release-manifest.json").write_text(json.dumps(release))
    entries = [
        ("directory:" if path.is_dir() else "file:")
        + path.relative_to(candidate).as_posix().replace("/", "\\")
        for path in candidate.rglob("*")
    ]
    entries.append("file:installation.json")
    (candidate / "installation.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "owner": "corinthos-core",
                "state": "staged",
                "version": "0.1.1",
                "entries": sorted(entries),
            }
        )
    )
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        "corinthos.update_manifest.public_key", lambda: key.public_key().public_bytes_raw()
    )
    output = tmp_path / "release"
    manifest = create_bundle(candidate, key, output)
    data = (output / "corinthos-update.json").read_bytes()
    signature = (output / "corinthos-update.sig").read_bytes()
    return candidate, output, manifest, data, signature, key


def test_signed_complete_bundle_and_exact_bytes(signed_candidate, tmp_path):
    candidate, output, manifest, data, signature, _ = signed_candidate
    assert verify_manifest(data, signature, "0.1.0") == manifest
    destination = tmp_path / "extracted"
    extract_payload(output / manifest["artifact"]["name"], destination, manifest)
    assert {
        p.relative_to(candidate).as_posix(): hash_file(p)
        for p in candidate.rglob("*")
        if p.is_file()
    } == {
        p.relative_to(destination).as_posix(): hash_file(p)
        for p in destination.rglob("*")
        if p.is_file()
    }
    for changed in (data + b" ", data[:-1] + b"x", b"{}"):
        with pytest.raises(UpdateError, match="VERIFICATION_FAILED"):
            verify_manifest(changed, signature, "0.1.0")


def test_unavailable_ed25519_provider_rejects_as_verification_failure(
    signed_candidate, monkeypatch
):
    _, _, _, data, signature, _ = signed_candidate

    def unavailable(_key):
        raise UnsupportedAlgorithm("Unavailable verifier fixture")

    monkeypatch.setattr("corinthos.update_manifest.Ed25519PublicKey.from_public_bytes", unavailable)
    with pytest.raises(UpdateError, match="VERIFICATION_FAILED"):
        verify_manifest(data, signature, "0.1.0")


def test_release_tool_refuses_secret_in_payload_and_existing_key_before_acl(
    signed_candidate, tmp_path, monkeypatch, capsys
):
    candidate, _, _, _, _, key = signed_candidate
    with pytest.raises(ValueError, match="outside"):
        create_bundle(candidate, key, candidate / "release-output")
    private = candidate / "never-ship.key"
    private.write_bytes(b"Nonsecret test marker")
    assert (
        release_main(
            [
                "bundle",
                "--candidate",
                str(candidate),
                "--private-key",
                str(private),
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 1
    )
    assert "outside candidate" in capsys.readouterr().err
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)

    def forbidden(*_args, **_kw):
        pytest.fail("Existing-key rejection attempted an ACL change")

    monkeypatch.setattr("corinthos.update_release.subprocess.run", forbidden)
    assert (
        release_main(
            [
                "generate-key",
                "--private-key",
                str(private),
                "--public-key",
                str(tmp_path / "public.json"),
            ]
        )
        == 1
    )
    assert private.read_bytes() == b"Nonsecret test marker"


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda value: value.update(workspace_format=10), "INCOMPATIBLE_SCHEMA"),
        (lambda value: value.update(version="0.1.0"), "INVALID_RELEASE"),
        (lambda value: value.update(version="0.0.9"), "INVALID_RELEASE"),
        (lambda value: value.update(platform="linux-x64"), "INVALID_RELEASE"),
        (lambda value: value.update(schema=True), "INVALID_RELEASE"),
        (lambda value: value["artifact"].update(name="../payload.zip"), "INVALID_RELEASE"),
        (lambda value: value["payload"].append(dict(value["payload"][0])), "INVALID_RELEASE"),
    ],
)
def test_authenticated_invalid_manifests_are_rejected(signed_candidate, change, code):
    _, _, manifest, _, _, key = signed_candidate
    change(manifest)
    data = json.dumps(manifest).encode()
    with pytest.raises(UpdateError, match=code):
        verify_manifest(data, key.sign(data), "0.1.0")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "a\\b",
        "C:/file",
        "a:stream",
        "a/CON.txt",
        "a/.CORINTHOS/state.db",
        "a/dir./x",
        "a//x",
    ],
)
def test_windows_payload_paths_are_not_executable_escape_hatches(name):
    with pytest.raises(UpdateError):
        relative_path(name)


def test_duplicate_json_and_case_alias_payload_are_rejected(signed_candidate):
    with pytest.raises(UpdateError):
        strict_json(b'{"a":1,"a":2}')
    _, _, manifest, _, _, key = signed_candidate
    duplicate = dict(manifest["payload"][0])
    duplicate["path"] = duplicate["path"].upper()
    manifest["payload"].append(duplicate)
    data = json.dumps(manifest).encode()
    with pytest.raises(UpdateError):
        verify_manifest(data, key.sign(data), "0.1.0")


def test_tampered_zip_and_unlisted_files_never_replace_owner_data(signed_candidate, tmp_path):
    candidate, output, manifest, _, _, _ = signed_candidate
    owner = tmp_path / "installation"
    owner.mkdir()
    (owner / "keep.bin").write_bytes(b"Exact owner bytes\x00\xff")
    archive = output / manifest["artifact"]["name"]
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(UpdateError):
        extract_payload(archive, tmp_path / "bad-stage", manifest)
    assert not (tmp_path / "bad-stage").exists()
    (candidate / "extra.txt").write_bytes(b"Not signed")
    with pytest.raises(UpdateError):
        verify_payload(candidate, manifest)
    assert (owner / "keep.bin").read_bytes() == b"Exact owner bytes\x00\xff"


def test_zip_symlink_is_rejected_even_when_archive_hash_matches(signed_candidate, tmp_path):
    _, _, manifest, _, _, _ = signed_candidate
    archive = tmp_path / "link.zip"
    entry = manifest["payload"][0]
    with zipfile.ZipFile(archive, "w") as bundle:
        for item in manifest["payload"]:
            info = zipfile.ZipInfo(item["path"])
            info.external_attr = ((stat.S_IFLNK if item is entry else stat.S_IFREG) | 0o600) << 16
            bundle.writestr(info, b"x" * item["bytes"])
    manifest["artifact"].update(bytes=archive.stat().st_size, sha256=hash_file(archive))
    with pytest.raises(UpdateError):
        extract_payload(archive, tmp_path / "unsafe", manifest)
    assert not (tmp_path / "unsafe").exists()


@pytest.fixture
def service(tmp_path, monkeypatch, signed_candidate):
    monkeypatch.setattr(user_state, "state_root", lambda: tmp_path / "user-state")
    monkeypatch.setattr(updater, "_installed", lambda: tmp_path / "installed")
    monkeypatch.setattr(updater, "installation_root", lambda: tmp_path / "installed")
    launches = []
    monkeypatch.setattr(
        updater, "_spawn", lambda action, identity, **_kw: launches.append((action, identity))
    )
    _, output, manifest, data, signature, _ = signed_candidate
    names = ("corinthos-update.json", "corinthos-update.sig", manifest["artifact"]["name"])
    contents = {names[0]: data, names[1]: signature, names[2]: (output / names[2]).read_bytes()}
    release = json.dumps(
        {
            "draft": False,
            "prerelease": False,
            "tag_name": "v0.1.1",
            "assets": [
                {
                    "name": name,
                    "size": len(contents[name]),
                    "browser_download_url": f"{updater._DOWNLOAD}/v0.1.1/{name}",
                }
                for name in names
            ],
        }
    ).encode()

    def receive(url, maximum, *, target=None):
        content = release if url == updater._API else contents[url.rsplit("/", 1)[1]]
        assert len(content) <= maximum
        if target:
            target.write_bytes(content)
        return content

    monkeypatch.setattr(updater, "_receive", receive)
    return launches


def test_one_operation_survives_check_start_status_and_duplicate_start(service, monkeypatch, wire):
    checked = updater.update_check()
    identity = checked["operation_id"]
    assert checked["phase"] == "CHECKING" and service == [("_check", identity)]
    assert updater.update_check()["operation_id"] == identity
    updater._check(identity)
    available = updater.update_status(identity)
    assert available["phase"] == "AVAILABLE" and available["can_start"]
    assert updater.update_start(identity)["phase"] == "STAGING"
    assert updater.update_start(identity)["error"]["code"] == "BUSY"
    assert service == [("_check", identity), ("_stage", identity)]
    root = updater._op_root(identity)
    monkeypatch.setattr(sys, "executable", str(root / "host/runtime/python.exe"))
    updater._stage(identity)
    ready = updater.update_status()
    assert ready["operation_id"] == identity and ready["phase"] == "READY" and ready["can_finish"]
    assert updater.helper_plan(identity, verify=True)["candidate_version"] == "0.1.1"
    wire("update-result", ready)
    assert updater.update_check()["operation_id"] == identity


def test_slow_check_does_not_occupy_rpc_or_state_lock(service, monkeypatch):
    entered, release = Event(), Event()
    threads = []

    def spawn(_action, identity, **_kw):
        def run():
            entered.set()
            release.wait(5)
            updater.set_phase(identity, "NO_UPDATE")

        thread = Thread(target=run)
        threads.append(thread)
        thread.start()

    monkeypatch.setattr(updater, "_spawn", spawn)
    before = time.monotonic()
    result = updater.update_check()
    assert entered.wait(1)
    assert time.monotonic() - before < 1
    assert updater.update_status(result["operation_id"])["phase"] == "CHECKING"
    release.set()
    for thread in threads:
        thread.join(2)
    assert updater.update_status()["phase"] == "NO_UPDATE"


def test_stale_available_operation_cannot_start_beside_a_new_check(service):
    old = updater.update_check()["operation_id"]
    updater._check(old)
    current = updater.update_check()["operation_id"]
    assert current != old
    assert updater.update_start(old)["error"]["code"] == "BUSY"
    assert updater.update_status(old)["phase"] == "AVAILABLE"
    assert updater.update_status()["operation_id"] == current
    assert ("_stage", old) not in service


def test_busy_operation_survives_conflicting_check_and_finishes_with_same_id(
    service, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    identity = updater.update_check()["operation_id"]
    updater._change(identity, lambda record: record.update(candidate_version="0.1.1"))
    updater.set_phase(identity, "BUSY", "BUSY")
    checked = updater.update_check()
    assert checked["operation_id"] == identity and checked["phase"] == "BUSY"
    assert checked["can_finish"] and updater._state()["current"] == identity
    assert service == [("_check", identity)]

    root = updater._op_root(identity)
    for name in ("helper/update.ps1", "host/runtime/python.exe"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Owned claim-only fixture")
    powershell = tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"Fixed system executable fixture")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(updater.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    handoffs = []
    monkeypatch.setattr(
        updater.subprocess,
        "Popen",
        lambda command, **_kw: handoffs.append(command) or SimpleNamespace(pid=123),
    )
    finished = updater.update_finish(identity)
    assert finished["operation_id"] == identity and finished["phase"] == "WAITING_FOR_EXIT"
    assert finished["error"] is None and updater._state()["current"] == identity
    assert len(handoffs) == 1 and service == [("_check", identity)]


def test_no_live_release_and_failed_source_have_explicit_terminal_outcomes(service, monkeypatch):
    identity = updater.update_check()["operation_id"]
    monkeypatch.setattr(updater, "_receive", lambda *_args, **_kw: None)
    updater._check(identity)
    assert updater.update_status(identity)["phase"] == "NO_UPDATE"
    newer = updater.update_check()["operation_id"]

    def unavailable(*_args, **_kw):
        raise UpdateError("SOURCE_UNAVAILABLE")

    monkeypatch.setattr(updater, "_receive", unavailable)
    monkeypatch.setattr(sys, "argv", ["corinthos.updater", "_check", newer])
    assert updater.main() == 1
    result = updater.update_status(newer)
    assert result["phase"] == "FAILED" and result["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_finish_refuses_to_close_an_owned_active_worker(service, monkeypatch):
    identity = updater.update_check()["operation_id"]
    updater.set_phase(identity, "READY")
    monkeypatch.setattr("corinthos.runtime.has_active", lambda: True)
    messages = _transcript(
        [
            _call("system.handshake", 1, protocol_version=2),
            _call("update.finish", 2, operation_id=identity, restart_gui=True),
        ]
    )
    assert messages[2]["result"]["error"]["code"] == "BUSY"
    assert updater.update_status(identity)["phase"] == "READY"


def test_interruption_requires_explicit_recovery_and_preserves_identity(service, monkeypatch):
    identity = updater.update_check()["operation_id"]
    updater._change(
        identity, lambda record: record.update(phase="INSTALLING", worker_pid=321, updated_at=0)
    )
    monkeypatch.setattr(updater, "_alive", lambda _pid: False)
    monkeypatch.setattr(updater.time, "time", lambda: 10_000_000_000)
    status = updater.update_status(identity)
    assert status["phase"] == "RECOVERY_REQUIRED" and status["can_finish"]
    assert updater.update_check()["operation_id"] == identity
    updater.set_phase(identity, "BUSY", "BUSY")
    assert updater.update_status(identity)["can_finish"]
    assert updater.update_status("f" * 32)["error"]["code"] == "OPERATION_NOT_FOUND"
    assert updater._state()["current"] == identity


def test_passive_status_is_readonly_and_does_not_construct_project_store(tmp_path, monkeypatch):
    monkeypatch.setattr(user_state, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(updater, "installation_root", lambda: None)

    def forbidden(*_args, **_kw):
        pytest.fail("Passive status attempted a mutation or provider operation")

    monkeypatch.setattr("corinthos.workspace.initialize", forbidden)
    monkeypatch.setattr(updater, "_receive", forbidden)
    monkeypatch.setattr(updater, "_spawn", forbidden)
    assert updater.update_status()["phase"] == "UNSUPPORTED"
    assert not (tmp_path / "state").exists()


def test_update_rpc_requires_identified_explicit_actions_and_strict_inputs(service, wire):
    requests = [_call("system.handshake", 1, protocol_version=2), _call("update.check", 2)]
    messages = _transcript(requests)
    for method in ("update.check", "update.start", "update.status", "update.finish"):
        assert method in messages[1]["result"]["methods"]
    result = messages[2]["result"]
    wire("update-result", result)
    identity = result["operation_id"]
    bad = _transcript(
        [
            _call("system.handshake", 1, protocol_version=2),
            {"jsonrpc": "2.0", "method": "update.check", "params": {}},
            _call("update.status", 3, url="https://elsewhere.invalid"),
            _call("update.start", 4, operation_id="../unsafe"),
        ]
    )
    # JSON-RPC notifications never receive a response, including rejected mutations.
    assert [message["error"]["code"] for message in bad[2:]] == [-32602, -32602]
    assert updater._state()["current"] == identity


def test_health_peer_cannot_invoke_project_provider_or_update_operations(monkeypatch):
    def forbidden():
        pytest.fail("Readonly health mode entered the ordinary runtime lease")

    monkeypatch.setattr("corinthos.runtime_gate.client_runtime", forbidden)
    requests = [_call("system.handshake", 1, protocol_version=2), _call("system.version", 2)]
    requests += [
        _call(method, index, root="/never")
        for index, method in enumerate(
            ("project.status", "providers.status", "update.check", "work.create"), start=3
        )
    ]
    requests.append(_call("system.shutdown", 7))
    source = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
    output = io.BytesIO()
    sidecar.serve(source, output, io.StringIO(), health=True)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[1]["result"]["methods"] == [
        "system.handshake",
        "system.version",
        "system.shutdown",
    ]
    assert [message["error"]["code"] for message in messages[3:7]] == [-32601] * 4
    assert messages[7]["result"] == {"stopping": True}


def test_fixed_system_powershell_may_be_hardlinked_but_owned_helpers_may_not(
    service, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from corinthos.runtime_gate import plain_path

    identity = updater.update_check()["operation_id"]
    updater.set_phase(identity, "READY")
    root = updater._op_root(identity)
    for name in ("helper/update.ps1", "host/runtime/python.exe"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Owned ordinary fixture")
    system_root = tmp_path / "Windows"
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    system_binary = tmp_path / "system-hardlink-source"
    system_binary.write_bytes(b"Windows executable fixture")
    os.link(system_binary, powershell)
    assert powershell.stat().st_nlink == 2
    with pytest.raises(RuntimeError):
        plain_path(powershell, file=True)
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(updater.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(updater.subprocess, "DETACHED_PROCESS", 0, raising=False)
    launches = []
    monkeypatch.setattr(
        updater.subprocess,
        "Popen",
        lambda command, **options: launches.append((command, options)) or SimpleNamespace(pid=123),
    )
    result = updater.update_finish(identity)
    assert result["phase"] == "WAITING_FOR_EXIT" and result["error"] is None
    assert launches[0][0][0] == str(powershell)
    assert launches[0][1]["cwd"] == root
    os.link(root / "helper/update.ps1", tmp_path / "owned-helper-alias")
    with pytest.raises(RuntimeError):
        plain_path(root / "helper/update.ps1", file=True)


def test_cli_uses_the_same_shared_operation_and_status(service, capsys):
    assert cli.main(["update", "check"]) == 0
    checked = json.loads(capsys.readouterr().out)
    identity = checked["operation_id"]
    assert cli.main(["update", "status", "--operation-id", identity]) == 0
    assert json.loads(capsys.readouterr().out)["operation_id"] == identity
    assert cli.main(["update", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["operation_id"] == identity


@pytest.mark.skipif(
    os.name != "nt", reason="Windows installation admission uses CreateFile sharing"
)
def test_installed_client_handle_excludes_replacement_and_new_clients(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    from corinthos import runtime_gate

    root = tmp_path / "Programs/Corinthos"
    root.mkdir(parents=True)
    (root / "installation.json").write_text(
        json.dumps(
            {"owner": "corinthos-core", "schema": 1, "state": "installed", "version": "0.1.0"}
        )
    )
    monkeypatch.setattr(runtime_gate, "installation_root", lambda: root)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path = str(root.parent / ".Corinthos-runtime.lock")
    with runtime_gate.client_runtime(), runtime_gate.client_runtime():
        assert (
            kernel.CreateFileW(path, 0xC0000000, 0, None, 4, 0x80, None)
            == wintypes.HANDLE(-1).value
        )
    handle = kernel.CreateFileW(path, 0xC0000000, 0, None, 4, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(RuntimeError, match="being updated"):
            with runtime_gate.client_runtime():
                pytest.fail("A new client entered replacement")
    finally:
        kernel.CloseHandle(handle)
