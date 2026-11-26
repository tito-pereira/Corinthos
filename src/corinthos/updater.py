# SPDX-License-Identifier: MPL-2.0
"""One explicit, durable updater shared by installed GUI and CLI clients."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from corinthos import __version__, user_state
from corinthos.filesystem import is_link
from corinthos.runtime_gate import installation_root, plain_path
from corinthos.update_manifest import (
    MAX_ARCHIVE,
    MAX_MANIFEST,
    UpdateError,
    extract_payload,
    read_bounded,
    strict_json,
    verify_manifest,
    verify_payload,
    version_tuple,
)

REPOSITORY = "tito-pereira/Corinthos"
_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
_DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download"
_ID = re.compile(r"[0-9a-f]{32}")
_DEFAULT = {"version": 1, "current": None, "operations": {}}
_ACTIVE = {
    "CHECKING",
    "STAGING",
    "READY",
    "WAITING_FOR_EXIT",
    "INSTALLING",
    "RECOVERY_REQUIRED",
    "BUSY",
}
_WORKING = {"CHECKING", "STAGING", "WAITING_FOR_EXIT", "INSTALLING"}
_PHASES = {
    "IDLE": (0, "Choose Check for updates to contact the official release channel."),
    "UNSUPPORTED": (0, "Updates require the shared per-user Windows installation."),
    "CHECKING": (5, "Checking the official stable release and its signature."),
    "AVAILABLE": (
        10,
        "A verified compatible update is available. Choose Update Corinthos to stage it.",
    ),
    "NO_UPDATE": (100, "No newer signed stable release is available."),
    "STAGING": (
        20,
        "Downloading and verifying the complete update outside the running installation.",
    ),
    "READY": (
        80,
        "The update is verified. Close other Corinthos clients, then finish and restart.",
    ),
    "WAITING_FOR_EXIT": (
        82,
        "Finishing this update requires this client to exit. Close other Corinthos clients.",
    ),
    "INSTALLING": (
        90,
        "Replacing the installation and checking GUI, CLI and private runtime health.",
    ),
    "SUCCEEDED": (100, "Corinthos was updated and its stable entry points passed startup checks."),
    "ROLLED_BACK": (
        0,
        "Startup failed or replacement was interrupted. The previous installation was restored.",
    ),
    "RECOVERY_REQUIRED": (
        80,
        "Close other Corinthos clients and finish this operation to recover the installation.",
    ),
    "FAILED": (0, "The update stopped safely. Check the error before starting another check."),
    "BUSY": (
        80,
        "Close other Corinthos clients and active operations, then finish this same update again.",
    ),
}
_ERRORS = {
    "NOT_INSTALLED": "Use the shared per-user Windows installation to update Corinthos.",
    "SOURCE_UNAVAILABLE": "The official release source could not be reached. Check your connection and try another check.",
    "INVALID_RELEASE": "The release metadata is malformed or inconsistent. No installed files were replaced.",
    "INCOMPATIBLE_SCHEMA": "This release uses a different workspace format and cannot be installed by this updater.",
    "VERIFICATION_FAILED": "The signature or complete release payload did not verify. No unverified candidate may be installed.",
    "ACCESS_DENIED": "The updater could not access its owned staging files. Check local permissions and available disk space.",
    "BUSY": "Another client, active operation or setup owns the installation. Close it, then retry this same operation.",
    "INTERRUPTED": "The update worker stopped before completion. Start a fresh check; the active installation was not replaced.",
    "STARTUP_FAILED": "The candidate failed startup checks; the previous compatible runtime was restored.",
    "RECOVERY_REQUIRED": "Recovery could not finish. Close Corinthos clients and finish this operation again, or rerun setup.",
    "OPERATION_NOT_FOUND": "That update operation is unavailable. Read the current status and start a fresh check.",
    "INVALID_STATE": "The saved updater state is invalid. Preserve it for inspection and repair the per-user installation.",
}


def _operation_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Supply a valid update operation ID.")
    return value


def _state():
    value = user_state.read_state("updates.json", _DEFAULT)
    if (
        set(value) != set(_DEFAULT)
        or not isinstance(value["operations"], dict)
        or len(value["operations"]) > 20
        or (value["current"] is not None and value["current"] not in value["operations"])
    ):
        raise UpdateError("INVALID_STATE")
    for identity, record in value["operations"].items():
        _operation_id(identity)
        if (
            not isinstance(record, dict)
            or record.get("operation_id") != identity
            or record.get("phase") not in _PHASES
            or type(record.get("updated_at")) is not int
            or type(record.get("worker_pid", 0)) is not int
            or not 0 <= record.get("worker_pid", 0) < 2**32
            or type(record.get("restart_gui", False)) is not bool
            or record.get("error") not in {None, *_ERRORS}
        ):
            raise UpdateError("INVALID_STATE")
        version_tuple(record.get("installed_version"))
        if record.get("candidate_version") is not None:
            version_tuple(record["candidate_version"])
    return value


def _record(identity):
    record = _state()["operations"].get(_operation_id(identity))
    if record is None:
        raise UpdateError("OPERATION_NOT_FOUND")
    return record


def _change(identity, transform, *, current_only=False):
    def apply(document):
        record = document["operations"].get(identity)
        if record is None:
            raise UpdateError("OPERATION_NOT_FOUND")
        if current_only and document["current"] != identity:
            return document
        transform(record)
        record["updated_at"] = int(time.time())
        return document

    return user_state.update_state("updates.json", _DEFAULT, apply)["operations"][identity]


def set_phase(identity, phase, code=None, *, worker_pid=None):
    _operation_id(identity)
    if phase not in _PHASES or (code is not None and code not in _ERRORS):
        raise ValueError("Unknown updater outcome.")

    def apply(record):
        record.update(phase=phase, error=code)
        if worker_pid is not None:
            record["worker_pid"] = worker_pid

    return _change(identity, apply)


def _view(record=None, *, phase="IDLE", error=None):
    phase = record["phase"] if record else phase
    code = error if error else record.get("error") if record else None
    installed = record["installed_version"] if record else __version__
    if record and phase == "SUCCEEDED":
        installed = record["candidate_version"]
    return {
        "operation_id": record["operation_id"] if record else None,
        "installed_version": installed,
        "candidate_version": record.get("candidate_version") if record else None,
        "phase": phase,
        "progress": _PHASES[phase][0],
        "message": _PHASES[phase][1],
        "error": {"code": code, "message": _ERRORS[code]} if code else None,
        "can_start": phase == "AVAILABLE" and code is None,
        "can_finish": phase in {"READY", "BUSY", "RECOVERY_REQUIRED"},
    }


def _installed():
    root = installation_root()
    if root is None:
        raise UpdateError("NOT_INSTALLED")
    marker = root / "installation.json"
    plain_path(marker, file=True)
    value = strict_json(read_bounded(marker, 256 * 1024), 256 * 1024)
    if (
        value.get("schema") != 1
        or value.get("owner") != "corinthos-core"
        or value.get("state") != "installed"
        or value.get("version") != __version__
    ):
        raise UpdateError("INVALID_STATE")
    return root


def _op_root(identity, *, create=False):
    path = user_state.state_root() / "updates" / _operation_id(identity)
    plain_path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        plain_path(path)
    return path


def _alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def update_status(operation_id=None):
    document = _state()
    identity = _operation_id(operation_id) if operation_id is not None else document["current"]
    record = document["operations"].get(identity) if identity else None
    if identity and record is None:
        return _view(phase="FAILED", error="OPERATION_NOT_FOUND")
    if (
        record
        and record["phase"] in _WORKING
        and time.time() - record["updated_at"] > 10
        and not _alive(record.get("worker_pid", 0))
    ):
        recovery = record["phase"] in {"WAITING_FOR_EXIT", "INSTALLING"}
        record = set_phase(
            identity,
            "RECOVERY_REQUIRED" if recovery else "FAILED",
            "RECOVERY_REQUIRED" if recovery else "INTERRUPTED",
        )
    if record:
        return _view(record)
    return _view(phase="IDLE" if installation_root() is not None else "UNSUPPORTED")


def busy_status(operation_id):
    return _view(_record(operation_id), error="BUSY")


def _spawn(action, identity, *, executable=None):
    command = [
        str(executable or sys.executable),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "corinthos.updater",
        action,
        identity,
    ]
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        command,
        cwd=_op_root(identity),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )
    _change(identity, lambda record: record.update(worker_pid=process.pid))


def update_check():
    try:
        _installed()
    except UpdateError as error:
        return _view(phase="UNSUPPORTED", error=error.code)
    identity = uuid4().hex
    created = []

    def claim(document):
        current = document["operations"].get(document["current"])
        if current and current["phase"] in _ACTIVE:
            return document
        while len(document["operations"]) >= 20:
            del document["operations"][next(iter(document["operations"]))]
        document["current"] = identity
        document["operations"][identity] = {
            "operation_id": identity,
            "installed_version": __version__,
            "candidate_version": None,
            "phase": "CHECKING",
            "error": None,
            "updated_at": int(time.time()),
            "worker_pid": 0,
            "restart_gui": False,
        }
        created.append(True)
        return document

    document = user_state.update_state("updates.json", _DEFAULT, claim)
    if not created:
        return _view(document["operations"][document["current"]], error="BUSY")
    try:
        _op_root(identity, create=True)
        _spawn("_check", identity)
    except (OSError, RuntimeError):
        set_phase(identity, "FAILED", "ACCESS_DENIED")
    return update_status(identity)


def update_start(operation_id):
    _installed()
    identity = _operation_id(operation_id)
    claimed = []

    def claim(record):
        if record["phase"] == "AVAILABLE":
            record.update(phase="STAGING", error=None, worker_pid=0)
            claimed.append(True)

    record = _change(identity, claim, current_only=True)
    if not claimed:
        return _view(record, error="BUSY")
    try:
        _spawn("_stage", identity)
    except (OSError, RuntimeError):
        set_phase(identity, "FAILED", "ACCESS_DENIED")
    return update_status(identity)


def update_finish(operation_id, *, restart_gui=False):
    from corinthos.runtime import has_active

    if has_active():
        return busy_status(operation_id)
    identity = _operation_id(operation_id)
    if type(restart_gui) is not bool:
        raise ValueError("Invalid restart choice.")
    claimed = []

    def claim(record):
        if record["phase"] in {"READY", "BUSY", "RECOVERY_REQUIRED"}:
            record.update(
                phase="WAITING_FOR_EXIT", error=None, restart_gui=restart_gui, worker_pid=0
            )
            claimed.append(True)

    record = _change(identity, claim, current_only=True)
    if not claimed:
        return _view(record, error="BUSY")
    try:
        root = _op_root(identity)
        helper = root / "helper/update.ps1"
        host = root / "host/runtime/python.exe"
        plain_path(helper, file=True)
        plain_path(host, file=True)
        if not helper.is_file() or not host.is_file():
            raise OSError("The external update host is missing.")
        powershell = (
            Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        plain_path(powershell.parent)
        info = powershell.lstat()
        # Windows system executables may have legitimate WinSxS hardlinks.
        if is_link(info) or not stat.S_ISREG(info.st_mode):
            raise OSError("Windows PowerShell is unavailable.")
        process = subprocess.Popen(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-OperationId",
                identity,
            ],
            stdin=subprocess.DEVNULL,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _change(identity, lambda value: value.update(worker_pid=process.pid))
    except (OSError, RuntimeError, KeyError):
        set_phase(identity, "BUSY", "ACCESS_DENIED")
    return update_status(identity)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.hostname
            not in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
        ):
            raise UpdateError("SOURCE_UNAVAILABLE")
        return super().redirect_request(request, file, code, message, headers, newurl)


def _receive(url, maximum, *, target=None):
    if not (url == _API or url.startswith(_DOWNLOAD + "/")):
        raise UpdateError("SOURCE_UNAVAILABLE")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Corinthos-explicit-updater",
            "Accept": "application/vnd.github+json" if url == _API else "application/octet-stream",
        },
    )
    try:
        with urllib.request.build_opener(_Redirect()).open(request, timeout=15) as source:
            if int(source.headers.get("Content-Length", "0")) > maximum:
                raise UpdateError("INVALID_RELEASE")
            data = bytearray()
            total = 0
            deadline = time.monotonic() + 300
            destination = target.open("xb") if target else None
            try:
                while block := source.read(1024 * 1024):
                    total += len(block)
                    if total > maximum or time.monotonic() > deadline:
                        raise UpdateError("INVALID_RELEASE")
                    if destination:
                        destination.write(block)
                    else:
                        data.extend(block)
            finally:
                if destination:
                    destination.close()
            return bytes(data)
    except urllib.error.HTTPError as error:
        if url == _API and error.code == 404:
            return None
        raise UpdateError("SOURCE_UNAVAILABLE") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise UpdateError("SOURCE_UNAVAILABLE") from error


def _manifest(identity):
    record = _record(identity)
    root = _op_root(identity)
    for name in ("manifest.json", "manifest.sig"):
        plain_path(root / name, file=True)
    return verify_manifest(
        read_bounded(root / "manifest.json"),
        read_bounded(root / "manifest.sig", 64),
        record["installed_version"],
    )


def _check(identity):
    record = _record(identity)
    release_data = _receive(_API, 1024 * 1024)
    if release_data is None:
        set_phase(identity, "NO_UPDATE")
        return
    release = strict_json(release_data, 1024 * 1024)
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("tag_name"), str)
        or not release["tag_name"].startswith("v")
    ):
        raise UpdateError("INVALID_RELEASE")
    candidate = release["tag_name"][1:]
    if version_tuple(candidate) <= version_tuple(record["installed_version"]):
        set_phase(identity, "NO_UPDATE")
        return
    names = (
        "corinthos-update.json",
        "corinthos-update.sig",
        f"Corinthos-{candidate}-windows-x64.zip",
    )
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) > 100:
        raise UpdateError("INVALID_RELEASE")
    selected = {}
    for name, maximum in zip(names, (MAX_MANIFEST, 64, MAX_ARCHIVE), strict=True):
        matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
        if len(matches) != 1:
            raise UpdateError("INVALID_RELEASE")
        item = matches[0]
        expected = f"{_DOWNLOAD}/v{candidate}/{name}"
        if (
            item.get("browser_download_url") != expected
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= maximum
        ):
            raise UpdateError("INVALID_RELEASE")
        selected[name] = item
    data = _receive(selected[names[0]]["browser_download_url"], MAX_MANIFEST)
    signature = _receive(selected[names[1]]["browser_download_url"], 64)
    manifest = verify_manifest(data, signature, record["installed_version"])
    if (
        manifest["version"] != candidate
        or manifest["artifact"]["bytes"] != selected[names[2]]["size"]
    ):
        raise UpdateError("INVALID_RELEASE")
    root = _op_root(identity)
    for name, content in (("manifest.json", data), ("manifest.sig", signature)):
        with (root / name).open("xb") as destination:
            destination.write(content)
    _change(
        identity,
        lambda value: value.update(phase="AVAILABLE", candidate_version=candidate, error=None),
    )


def _copy_host(identity):
    root = _op_root(identity)
    install = Path(os.environ["LOCALAPPDATA"]) / "Programs/Corinthos"
    host = root / "host"
    host.mkdir(mode=0o700)
    total = count = 0
    for name in ("runtime", "app"):
        source = install / name
        plain_path(source)
        for path in source.rglob("*"):
            plain_path(path, file=not path.is_dir())
            if path.is_file():
                total += path.stat().st_size
                count += 1
                if total > 1024 * 1024 * 1024 or count > 20000:
                    raise UpdateError("INVALID_STATE")
        shutil.copytree(source, host / name)
    helper = root / "helper"
    helper.mkdir(mode=0o700)
    for name in ("common.ps1", "distribution.ps1", "release.ps1", "update.ps1"):
        source = install / "platform/windows" / name
        plain_path(source, file=True)
        shutil.copyfile(source, helper / name)
    _spawn("_stage", identity, executable=host / "runtime/python.exe")


def _stage(identity):
    root = _op_root(identity)
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(
        str(root / "host/runtime/python.exe")
    ):
        _copy_host(identity)
        return
    manifest = _manifest(identity)
    archive = root / "bundle.zip"
    _receive(
        f"{_DOWNLOAD}/v{manifest['version']}/{manifest['artifact']['name']}",
        manifest["artifact"]["bytes"],
        target=archive,
    )
    extract_payload(archive, root / "candidate", manifest)
    set_phase(identity, "READY", worker_pid=0)


def helper_plan(identity, *, verify=False):
    record = _record(identity)
    if record["candidate_version"] is None:
        raise UpdateError("INVALID_STATE")
    if verify:
        manifest = _manifest(identity)
        if manifest["version"] != record["candidate_version"]:
            raise UpdateError("VERIFICATION_FAILED")
        verify_payload(_op_root(identity) / "candidate", manifest)
    return {
        key: record[key]
        for key in ("operation_id", "installed_version", "candidate_version", "restart_gui")
    }


def main():
    arguments = sys.argv[1:]
    if len(arguments) not in {2, 3, 4}:
        return 2
    action, identity = arguments[:2]
    _operation_id(identity)
    try:
        if action in {"_plan", "_verify"} and len(arguments) == 2:
            print(json.dumps(helper_plan(identity, verify=action == "_verify")))
        elif action == "_phase" and len(arguments) in {3, 4}:
            set_phase(identity, arguments[2], arguments[3] if len(arguments) == 4 else None)
        elif action in {"_check", "_stage"} and len(arguments) == 2:
            _change(identity, lambda value: value.update(worker_pid=os.getpid()))
            (_check if action == "_check" else _stage)(identity)
        else:
            return 2
        return 0
    except UpdateError as error:
        if action in {"_check", "_stage"}:
            set_phase(identity, "FAILED", error.code)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError):
        if action in {"_check", "_stage"}:
            set_phase(identity, "FAILED", "ACCESS_DENIED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
