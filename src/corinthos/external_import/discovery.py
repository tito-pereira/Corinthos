# SPDX-License-Identifier: MPL-2.0
"""Read bounded local Git facts without executing repository configuration."""

import ctypes
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
import unicodedata
from collections import Counter
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache
from hashlib import sha1, sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Thread

from corinthos.adapters.process_control import start_process
from corinthos.bootstrap import _root
from corinthos.context.redaction import sanitize_text, secret_path
from corinthos.contracts import IntegrityError
from corinthos.contracts.artifacts import is_link
from corinthos.contracts.model import canonical_json

POLICY_VERSION = 1
MAX_TRACKED_PATHS = 2048
MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
_MAX_SUMMARY = 16
_MAX_FACTS = 16
_MAX_PATH_BYTES = 512
_MAX_WALK_ENTRIES = 8192
_MAX_GIT_PAYLOAD = 2 * 1024 * 1024
_MAX_INDEX = 8 * 1024 * 1024
_MAX_GIT_OBJECTS = 65_536
_MAX_GIT_OBJECT_BYTES = 512 * 1024 * 1024
_MAX_GIT_ALLOCATION = 64 * 1024 * 1024
_GIT_TIMEOUT = 8
_PRIVATE = {".git", ".corinthos", ".nukytera", ".agentforge"}
_REGULAR = {"100644", "100755"}
_MODES = _REGULAR | {"120000", "160000"}
_OID = re.compile(r"[0-9a-f]{40}")
_UNSAFE_PATH = re.compile(r'[\\:<>|"?*\x00-\x1f\x7f]')
_SAFE_NAME = re.compile(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_SAFE_DEPENDENCY = re.compile(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9][A-Za-z0-9_./-]{0,95}")
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?=$|[\s\[<>=!~;])")
_DEBUG = re.compile(
    rb"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n"
    rb"  dev: (\d+)\tino: (\d+)\n  uid: (\d+)\tgid: (\d+)\n"
    rb"  size: (\d+)\tflags: ([0-9a-f]+)\n"
)
_GITLINK_MARKER = "SUBMODULE"
_MANIFEST_NAMES = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "package.json": "node",
    "package-lock.json": "node",
    "pnpm-lock.yaml": "node",
    "yarn.lock": "node",
    "cargo.toml": "rust",
    "cargo.lock": "rust",
    "go.mod": "go",
    "go.sum": "go",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "cmakelists.txt": "cmake",
    "makefile": "make",
    "dockerfile": "container",
    "compose.yaml": "container",
    "docker-compose.yml": "container",
    "docker-compose.yaml": "container",
    "compose.yml": "container",
}
_LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".rb": "Ruby",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".php": "PHP",
    ".swift": "Swift",
    ".vue": "Vue",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
    ".lua": "Lua",
}
_CONFIG = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml"}
_TEXT = {".txt", ".csv", ".rst", ".md", ".markdown", ".adoc", ".log", ".patch", ".diff"}
_BINARY = {
    ".bin",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyd",
    ".so",
    ".sqlite",
    ".webp",
    ".whl",
    ".zip",
}


class ImportDiscoveryError(IntegrityError):
    """The selected root or a repository observation cannot be established safely."""


def _fail(message):
    raise ImportDiscoveryError(message) from None


def _portable(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
        or _UNSAFE_PATH.search(value)
    ):
        _fail("A Git path exceeds the supported portable path format.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or value == "."
        or any(
            part in {".", ".."} or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
    ):
        _fail("A Git path exceeds the supported portable path format.")
    return path.parts


def _private(relative):
    return any(part.casefold() in _PRIVATE for part in PurePosixPath(relative).parts)


def _managed_state_path(relative):
    return relative.split("/", 1)[0].casefold() == ".corinthos"


def _sensitive(relative):
    return _private(relative) or secret_path(relative) or sanitize_text(relative) != relative


def _ordinary_info(path, expected):
    info = path.lstat()
    if is_link(info) or not expected(info.st_mode):
        _fail("A repository metadata path is redirected or has an unexpected file type.")
    if expected is stat.S_ISREG and info.st_nlink != 1:
        _fail("Git metadata and indexed content require ordinary unlinked files.")
    return info


def _components(root, relative, *, worktree=False):
    parts = _portable(relative)
    parent = root
    _ordinary_info(parent, stat.S_ISDIR)
    for part in parts[:-1]:
        parent = parent / part
        _ordinary_info(parent, stat.S_ISDIR)
        if worktree and os.path.lexists(parent / ".git"):
            _fail("Tracked files cannot be read through a nested Git worktree.")
    return parent / parts[-1]


def _signature(info, *, compare_handle=False):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        None if compare_handle and os.name == "nt" else info.st_ctime_ns,
    )


def _read(root, relative, limit, *, missing_ok=False, worktree=False):
    try:
        path = _components(root, relative, worktree=worktree)
        info = _ordinary_info(path, stat.S_ISREG)
        if info.st_size > limit:
            _fail("A repository metadata or content read exceeds its size limit.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if _signature(info, compare_handle=True) != _signature(before, compare_handle=True):
                _fail("A repository file changed while discovery opened it.")
            raw = stream.read(info.st_size + 1)
            if (
                len(raw) != info.st_size
                or _signature(before) != _signature(os.fstat(stream.fileno()))
                or _signature(info)
                != _signature(_components(root, relative, worktree=worktree).lstat())
            ):
                _fail("A repository file changed while discovery read it.")
        return raw, info
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail("Required Git metadata is missing.")


def _text(data):
    if re.search(rb"[\x00-\x08\x0b\x0e-\x1f\x7f]", data):
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def read_indexed_text(root, relative, *, max_bytes=MAX_FILE_BYTES):
    """Read ordinary bounded UTF-8 content by an admitted relative path."""
    try:
        if (
            _sensitive(relative)
            or PurePosixPath(relative).suffix.casefold() in _BINARY
            or type(max_bytes) is not int
            or not 0 <= max_bytes <= MAX_FILE_BYTES
        ):
            return None
        path = _root(root)
        raw, _ = _read(path, relative, max_bytes, worktree=True)
        text = _text(raw)
        return None if text is None else (text, sha256(raw).hexdigest())
    except (IntegrityError, OSError, TypeError, ValueError, RuntimeError):
        return None


def _root_git(root):
    if Path(root).drive.startswith("\\\\"):
        _fail("The selected Git worktree must be on a local filesystem.")
    try:
        root = _root(root)
        gitdir = root / ".git"
        _ordinary_info(gitdir, stat.S_ISDIR)
    except (ImportDiscoveryError, FileNotFoundError):
        _fail(
            "Select the exact root of an ordinary local Git worktree. Linked or bare Git roots are not supported."
        )
    if any(os.path.lexists(gitdir / name) for name in ("commondir", "gitdir")):
        _fail("Linked or redirected Git metadata is not supported.")
    return root, gitdir


def _read_ref(gitdir):
    raw, _ = _read(gitdir, "HEAD", 4096)
    head = raw.decode("utf-8").strip()
    if _OID.fullmatch(head):
        return head, None
    if not head.startswith("ref: refs/heads/"):
        _fail("Git HEAD does not identify a supported branch or detached commit.")
    ref = head[len("ref: ") :]
    _portable(ref)
    branch = ref[len("refs/heads/") :]
    loose = _read(gitdir, ref, 1024, missing_ok=True)
    if loose is not None:
        oid = loose[0].decode("ascii").strip()
        if not _OID.fullmatch(oid):
            _fail("Git HEAD points to an unsupported local reference.")
        return oid, branch
    packed = _read(gitdir, "packed-refs", _MAX_GIT_PAYLOAD, missing_ok=True)
    if packed is not None:
        exact = (" " + ref).encode("utf-8")
        matches = [line[: -len(exact)] for line in packed[0].splitlines() if line.endswith(exact)]
        if len(matches) > 1:
            _fail("Git HEAD identifies more than one packed reference.")
        if matches:
            oid = matches[0].decode("ascii")
            if not _OID.fullmatch(oid):
                _fail("Git HEAD points to an unsupported packed reference.")
            return oid, branch
    if any(os.path.lexists(gitdir / name) for name in ("reftable", "logs/HEAD")):
        _fail("The working branch does not have a readable current Git reference.")
    return None, branch


def _object_store(gitdir):
    """Check native tree reads cannot rely on alternate, linked or lazy-fetched objects."""
    objects = gitdir / "objects"
    _ordinary_info(objects, stat.S_ISDIR)
    count = 0
    total = 0
    pending = [(objects, 0)]
    while pending:
        directory, depth = pending.pop()
        _ordinary_info(directory, stat.S_ISDIR)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_GIT_OBJECTS:
                    _fail("The Git object inventory exceeds the discovery limit.")
                info = Path(entry.path).lstat()
                name = entry.name.casefold()
                if is_link(info) or (depth and name in {"alternates", "http-alternates"}):
                    _fail("Linked or alternate Git object storage is not supported.")
                if name.endswith(".promisor"):
                    _fail("Partial clone Git objects are not supported by offline discovery.")
                if stat.S_ISDIR(info.st_mode) and not depth:
                    if entry.name not in {"pack", "info"} and not re.fullmatch(
                        r"[0-9a-f]{2}", entry.name
                    ):
                        _fail("The Git object directory layout is unsupported.")
                    pending.append((Path(entry.path), 1))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    _fail("Git objects require ordinary unlinked storage.")
                else:
                    total += info.st_size
                    if total > _MAX_GIT_OBJECT_BYTES:
                        _fail("The Git object storage exceeds the offline discovery limit.")
    return objects


def _outside(root, candidates):
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute() or path.drive.startswith("\\\\"):
            continue
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            continue
        if not path.is_relative_to(root) and not resolved.is_relative_to(root):
            yield resolved


def _git_runtime(root):
    filename = "git.exe" if os.name == "nt" else "git"
    search = tuple(_outside(root, os.environ.get("PATH", "").split(os.pathsep)))
    paths = (directory / filename for directory in search)
    for candidate in _outside(root, paths):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate, os.pathsep.join(str(path) for path in search)
    _fail(
        "A native Git executable outside the selected worktree is required on the configured PATH."
    )


def _git_command(runtime, box, objects, *args):
    exe, path = runtime
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        PATH=path,
        TMPDIR=str(box),
        TEMP=str(box),
        TMP=str(box),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_PROTOCOL_FROM_USER="0",
        GIT_ALLOW_PROTOCOL="",
        GIT_ALLOC_LIMIT=str(_MAX_GIT_ALLOCATION),
        GIT_MMAP_LIMIT=str(_MAX_GIT_OBJECT_BYTES),
    )
    if objects is not None:
        env["GIT_OBJECT_DIRECTORY"] = str(objects)
    command = [str(exe), "--no-pager", "--no-optional-locks", "--git-dir", str(box), *args]
    managed = start_process(
        command,
        cwd=box,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    process = managed.process
    raw = bytearray()

    def read():
        try:
            while len(raw) <= _MAX_GIT_PAYLOAD:
                data = process.stdout.read1(min(32 * 1024, _MAX_GIT_PAYLOAD + 1 - len(raw)))
                if not data:
                    return
                raw.extend(data)
            process.kill()
        except (OSError, ValueError):
            return

    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        process.wait(timeout=_GIT_TIMEOUT)
        reader.join(timeout=1)
    except subprocess.TimeoutExpired:
        _fail("Git could not finish bounded offline discovery.")
    finally:
        managed.finish()
        process.stdout.close()
        reader.join(timeout=1)
    if reader.is_alive() or process.returncode or len(raw) > _MAX_GIT_PAYLOAD:
        _fail("Git could not return a bounded, verifiable index or tree.")
    return bytes(raw)


@contextmanager
def _isolated_index(root, gitdir):
    candidates = [os.environ.get(key, "") for key in ("TMPDIR", "TEMP", "TMP")]
    if os.name == "nt":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Temp")
    else:
        candidates.extend(("/tmp", "/var/tmp"))
    outside = next(
        (path for path in _outside(root, candidates) if path.is_dir() and os.access(path, os.W_OK)),
        None,
    )
    if outside is None:
        _fail("Offline Git discovery requires a temporary directory outside the selected worktree.")
    record = _read(gitdir, "index", _MAX_INDEX, missing_ok=True)
    with tempfile.TemporaryDirectory(dir=outside, prefix="corinthos-git-read-") as temporary:
        box = Path(temporary)
        (box / "refs").mkdir()
        (box / "objects").mkdir()
        (box / "HEAD").write_bytes(b"ref: refs/heads/offline-observer\n")
        (box / "config").write_bytes(b"[core]\n bare = true\n fsmonitor = false\n")
        if record is not None:
            (box / "index").write_bytes(record[0])
        yield box, record
        after = _read(gitdir, "index", _MAX_INDEX, missing_ok=True)
        if (record is None) != (after is None) or (record is not None and after[0] != record[0]):
            _fail("The exact Git index changed during discovery.")


def _entries(raw, *, tree=False):
    entries, spellings = {}, {}
    index = 0
    while index < len(raw):
        end = raw.find(b"\0", index)
        if end < 0:
            _fail("Git did not return a complete portable path entry.")
        metadata, name = raw[index:end].split(b"\t", 1)
        mode, second, third = metadata.decode("ascii").split()
        oid = third if tree else second
        relative = name.decode("utf-8")
        parts = _portable(relative)
        if len(entries) >= MAX_TRACKED_PATHS:
            _fail("The tracked Git inventory exceeds the discovery limit.")
        if relative in entries:
            _fail("The Git inventory contains conflicting path spellings or unresolved stages.")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold()
            if spellings.setdefault(key, prefix) != prefix:
                _fail("The Git inventory contains conflicting path spellings or unresolved stages.")
        if not tree:
            details = _DEBUG.match(raw, end + 1)
            if details is None:
                _fail("Git did not return supported index stat metadata.")
            index = details.end()
            if third != "0" or int(details[10], 16) & 0x40004000 or mode not in _MODES:
                _fail("Sparse or unresolved Git index entries are not supported.")
            facts = tuple(int(value) for value in details.groups()[:-1])
        else:
            index = end + 1
            facts = None
            if mode not in _MODES:
                _fail("The head tree contains an unsupported tracked entry.")
        if not _OID.fullmatch(oid):
            _fail("Git returned an unsupported tracked object identity.")
        entries[relative] = (mode, oid, facts)
    return entries


def _index(runtime, box):
    return _entries(
        _git_command(runtime, box, None, "ls-files", "--stage", "--debug", "--sparse", "-z", "--")
    )


def tracked_paths(root):
    """Read the current stage-zero regular nonprivate paths without project configuration."""
    try:
        root, gitdir = _root_git(root)
        runtime = _git_runtime(root)
        with _isolated_index(root, gitdir) as (box, _):
            return frozenset(
                path
                for path, (mode, _, _) in _index(runtime, box).items()
                if mode in _REGULAR and not _sensitive(path)
            )
    except (OSError, ValueError, UnicodeError, RuntimeError) as error:
        if isinstance(error, ImportDiscoveryError):
            raise
        _fail("The tracked paths could not be inspected safely.")


def discovery_digest(observation):
    """Hash semantic repository facts with no local root or managed-state marker."""
    semantic = {
        key: value
        for key, value in observation.items()
        if key not in {"existing_state", "discovery_digest"}
    }
    semantic["ambiguities"] = [
        entry for entry in semantic["ambiguities"] if entry["code"] != "EXISTING_STATE"
    ]
    return sha256(canonical_json(semantic).encode("utf-8")).hexdigest()


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation", ctypes.c_int64),
        ("access", ctypes.c_int64),
        ("write", ctypes.c_int64),
        ("change", ctypes.c_int64),
        ("attributes", wintypes.DWORD),
    ]


@lru_cache(maxsize=1)
def _windows_file_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(_WindowsFileBasicInfo),
        wintypes.DWORD,
    ]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _windows_change_ns(path):
    api = _windows_file_api()
    # Query file attributes only. Opening the reparse point itself prevents a
    # replaced leaf from redirecting this metadata lookup to its target.
    read_attributes, share_all, open_existing, open_reparse = 0x80, 7, 3, 0x00200000
    handle = api.CreateFileW(
        str(path), read_attributes, share_all, None, open_existing, open_reparse, None
    )
    if handle == wintypes.HANDLE(-1).value:
        return None
    try:
        basic_info = _WindowsFileBasicInfo()
        if not api.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(basic_info), ctypes.sizeof(basic_info)
        ):
            return None
        if not basic_info.change or basic_info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return None
        return (basic_info.change - 116_444_736_000_000_000) * 100
    finally:
        api.CloseHandle(handle)


def _stat_matches(path, facts, info, index_info, mode, *, symlink=False):
    cs, cn, ms, mn, device, inode, uid, gid, size = facts
    if (
        size != info.st_size
        or not ms * 1_000_000_000 + mn == info.st_mtime_ns < index_info.st_mtime_ns
    ):
        return False
    cached_ctime = cs * 1_000_000_000 + cn
    if os.name == "nt":
        # Git for Windows stores creation time and omits identity. A change
        # since the index snapshot makes a metadata-only match inconclusive.
        changed = _windows_change_ns(path)
        return (
            cached_ctime == info.st_birthtime_ns
            and changed is not None
            and changed < index_info.st_mtime_ns
            and _signature(info) == _signature(path.lstat())
        )
    return (
        cached_ctime == info.st_ctime_ns
        and device == info.st_dev & 0xFFFFFFFF
        and inode == info.st_ino & 0xFFFFFFFF
        and uid == info.st_uid
        and gid == info.st_gid
        and (symlink or bool(info.st_mode & stat.S_IXUSR) == (mode == "100755"))
    )


def _ecosystem(relative):
    name = PurePosixPath(relative).name.casefold()
    if name.startswith("requirements") and name.endswith(".txt"):
        return "python"
    if name.endswith((".csproj", ".sln")):
        return "dotnet"
    return _MANIFEST_NAMES.get(name)


def _kind(relative):
    path = PurePosixPath(relative)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    components = {part.casefold() for part in path.parts[:-1]}
    if _ecosystem(relative):
        return "MANIFEST"
    if (
        name.startswith(("readme", "contributing", "changelog"))
        or components.intersection({"docs", "doc", "adr", "architecture"})
        and suffix in _TEXT
        or path.stem.casefold() in {"architecture", "design"}
        and suffix in _TEXT
    ):
        return "DOCUMENTATION"
    if (
        components.intersection({"tests", "test", "spec", "specs", "__tests__"})
        or name.startswith(("test_", "test."))
        or any(path.stem.casefold().endswith(ending) for ending in ("_test", ".test", ".spec"))
    ):
        return "TEST"
    if suffix in _LANGUAGES:
        return "SOURCE"
    if suffix in _CONFIG or name in {
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".editorconfig",
    }:
        return "CONFIGURATION"
    return "OTHER_TEXT"


def _file(root, relative, mode, oid, facts, index_info, remaining):
    sensitive = _sensitive(relative)
    observation = {
        "path": None if sensitive else relative,
        "kind": "EXCLUDED" if sensitive else _kind(relative),
        "index_oid": None if sensitive else oid,
        "index_mode": mode,
        "worktree_sha256": None,
        "state": "CLEAN",
        "size": None,
        "content_excluded": sensitive,
    }
    if mode == "160000":
        observation.update(
            kind=_GITLINK_MARKER if not sensitive else "EXCLUDED", content_excluded=True
        )
        return observation, None, 0, "SUBMODULE"
    try:
        path = _components(root, relative, worktree=True)
        info = path.lstat()
    except FileNotFoundError:
        info = None
    except ImportDiscoveryError:
        observation.update(state="UNVERIFIED", content_excluded=True)
        return observation, None, 0, "REDIRECTED_PATH"
    if mode == "120000":
        observation.update(kind="SYMLINK" if not sensitive else "EXCLUDED", content_excluded=True)
        if info is None:
            observation["state"] = "MISSING"
        elif not _stat_matches(path, facts, info, index_info, mode, symlink=True):
            observation["state"] = "UNVERIFIED"
        return observation, None, 0, "SENSITIVE_PATH" if sensitive else "SYMLINK"
    if info is None:
        observation.update(state="MISSING", content_excluded=True)
        return observation, None, 0, "SENSITIVE_PATH" if sensitive else "MISSING"
    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        observation.update(state="UNVERIFIED", content_excluded=True)
        return observation, None, 0, "REDIRECTED_PATH"
    if not sensitive:
        observation["size"] = info.st_size
    binary = PurePosixPath(relative).suffix.casefold() in _BINARY
    if sensitive or binary or info.st_size > min(MAX_FILE_BYTES, remaining):
        observation["content_excluded"] = True
        if not _stat_matches(path, facts, info, index_info, mode):
            observation["state"] = "UNVERIFIED"
        if sensitive:
            return observation, None, 0, "SENSITIVE_PATH"
        if binary:
            observation["kind"] = "BINARY"
            return observation, None, 0, "BINARY"
        return observation, None, 0, "CONTENT_BUDGET"

    try:
        raw, current = _read(root, relative, MAX_FILE_BYTES, worktree=True)
    except ImportDiscoveryError:
        observation.update(state="UNVERIFIED", content_excluded=True)
        return observation, None, 0, "REDIRECTED_PATH"
    if _signature(current) != _signature(info) or len(raw) > remaining:
        _fail("An indexed file changed during the bounded inventory.")
    # Compare raw Git blob identity without evaluating attributes, filters or textconv.
    header = f"blob {len(raw)}\0".encode("ascii")
    raw_oid = sha1(header + raw, usedforsecurity=False).hexdigest()
    clean = raw_oid == oid
    if os.name != "nt" and bool(info.st_mode & stat.S_IXUSR) != (mode == "100755"):
        clean = False
    observation.update(
        worktree_sha256=sha256(raw).hexdigest(),
        state="CLEAN" if clean else "MODIFIED",
    )
    text = _text(raw)
    if text is None:
        observation.update(kind="BINARY", content_excluded=True)
        return observation, None, len(raw), "BINARY"
    return observation, text, len(raw), None


def _walk_untracked(root, indexed):
    """Summarize untracked paths, including ignored entries, without reading their contents."""
    tracked_prefixes = {
        parent.as_posix()
        for path in indexed
        for parent in PurePosixPath(path).parents
        if parent != PurePosixPath(".")
    }
    pending = [(root, "")]
    paths, nested = [], 0
    count = 0
    while pending:
        directory, parent = pending.pop()
        _ordinary_info(directory, stat.S_ISDIR)
        if parent and os.path.lexists(directory / ".git"):
            nested += 1
            paths.append([parent, "NESTED_GIT"])
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name.casefold() in _PRIVATE:
                    continue
                count += 1
                if count > _MAX_WALK_ENTRIES:
                    _fail("The worktree exceeds the bounded path discovery limit.")
                relative = parent + "/" + name if parent else name
                if relative in indexed:
                    continue
                _portable(relative)
                info = entry.stat(follow_symlinks=False)
                if _sensitive(relative):
                    paths.append([relative, "EXCLUDED"])
                    continue
                if stat.S_ISDIR(info.st_mode) and not is_link(info):
                    pending.append((Path(entry.path), relative))
                elif relative not in tracked_prefixes:
                    paths.append([relative, "LINK" if is_link(info) else "OTHER"])
    return len(paths), sha256(canonical_json(sorted(paths)).encode("utf-8")).hexdigest(), nested


def _member(value):
    if not isinstance(value, str):
        return None
    try:
        _portable(value)
    except ImportDiscoveryError:
        return None
    if _sensitive(value) or value.startswith("-"):
        return None
    return value


def _identifier(value, pattern=_SAFE_NAME):
    return (
        value
        if isinstance(value, str) and pattern.fullmatch(value) and not _sensitive(value)
        else None
    )


def _dependency_names(value):
    if isinstance(value, dict):
        return value.keys()
    if isinstance(value, list):
        return (
            found.group(0)
            for item in value
            if isinstance(item, str) and (found := _REQUIREMENT_NAME.match(item))
        )
    return ()


def _facts(value, fields):
    names = (name for field in fields for name in _dependency_names(value.get(field)))
    return sorted({clean for name in names if (clean := _identifier(name, _SAFE_DEPENDENCY))})[
        :_MAX_FACTS
    ]


def _manifest(relative, content):
    path = PurePosixPath(relative)
    ecosystem = _ecosystem(relative)
    record = {
        "path": relative,
        "ecosystem": ecosystem,
        "name": None,
        "dependencies": [],
        "scripts": [],
        "members": [],
        "parsed": False,
    }
    if content is None:
        return record
    name = path.name.casefold()
    try:
        if name == "package.json":
            package = json.loads(content)
            if not isinstance(package, dict):
                return record
            workspace = package.get("workspaces", [])
            if isinstance(workspace, dict):
                workspace = workspace.get("packages", [])
            scripts = package.get("scripts", {})
            record.update(
                parsed=True,
                name=_identifier(package.get("name")),
                dependencies=_facts(
                    package,
                    ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"),
                ),
                scripts=sorted(clean for item in scripts if (clean := _identifier(item)))[
                    :_MAX_FACTS
                ]
                if isinstance(scripts, dict)
                else [],
                members=sorted({clean for item in workspace if (clean := _member(item))})[
                    :_MAX_FACTS
                ]
                if isinstance(workspace, list)
                else [],
            )
        elif name in {"pyproject.toml", "cargo.toml"}:
            value = tomllib.loads(content)
            project = value.get("project" if ecosystem == "python" else "package", {})
            workspace = value.get("workspace", {})
            members = workspace.get("members", []) if isinstance(workspace, dict) else []
            if not isinstance(project, dict):
                return record
            if ecosystem == "python":
                scripts = project.get("scripts", {})
                dependencies = _facts(project, ("dependencies",))
            else:
                scripts = {}
                dependencies = _facts(
                    value, ("dependencies", "dev-dependencies", "build-dependencies")
                )
            record.update(
                parsed=True,
                name=_identifier(project.get("name")),
                dependencies=dependencies,
                scripts=sorted(clean for item in scripts if (clean := _identifier(item)))[
                    :_MAX_FACTS
                ]
                if isinstance(scripts, dict)
                else [],
                members=sorted({clean for item in members if (clean := _member(item))})[:_MAX_FACTS]
                if isinstance(members, list)
                else [],
            )
        elif name.startswith("requirements") and name.endswith(".txt"):
            declarations = [
                line.strip()
                for line in content.splitlines()
                if re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:[<>=!~][A-Za-z0-9_.<>=!~,-]+)?", line.strip()
                )
            ]
            record.update(parsed=True, dependencies=_facts({"deps": declarations}, ("deps",)))
        elif name == "go.mod":
            for line in content.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[0] == "module":
                    record.update(parsed=True, name=_identifier(fields[1], _SAFE_DEPENDENCY))
                    break
    except (ValueError, TypeError, RecursionError, OverflowError):
        return record
    return record


def _ambiguity(code, explanation, *, required=False):
    return {
        "code": code,
        "explanation": explanation,
        "required": required,
        "options": ["CONFIRM"] if required else [],
    }


def _identity(indexed):
    return sha256(
        canonical_json(
            [[path, mode, oid] for path, (mode, oid, _) in sorted(indexed.items())]
        ).encode("utf-8")
    ).hexdigest()


def scan(root):
    """Return one bounded semantic observation of an ordinary local Git worktree."""
    try:
        root, gitdir = _root_git(root)
        runtime = _git_runtime(root)
        head, branch = _read_ref(gitdir)
        with _isolated_index(root, gitdir) as (box, recorded):
            indexed = _index(runtime, box)
            if head is None:
                tree = {}
            else:
                objects = _object_store(gitdir)
                tree = _entries(
                    _git_command(runtime, box, objects, "ls-tree", "--full-tree", "-r", "-z", head),
                    tree=True,
                )
                _object_store(gitdir)
            if (head, branch) != _read_ref(gitdir):
                _fail("Git HEAD changed during discovery.")
            remaining = MAX_TEXT_BYTES
            files, exclusions, manifests, languages = [], set(), [], Counter()
            for relative, (mode, oid, facts) in sorted(indexed.items()):
                observation, data, consumed, exclusion = _file(
                    root, relative, mode, oid, facts, recorded[1], remaining
                )
                remaining -= consumed
                files.append(observation)
                if exclusion is not None:
                    exclusions.add(exclusion)
                if observation["kind"] == "MANIFEST" and len(manifests) < _MAX_SUMMARY:
                    manifests.append(_manifest(relative, data))
                filename = PurePosixPath(relative)
                language = _LANGUAGES.get(filename.suffix.casefold())
                if language is not None and observation["kind"] in {"SOURCE", "TEST"}:
                    languages[language] += 1
            untracked_count, untracked_digest, nested = _walk_untracked(root, indexed)
            if (head, branch) != _read_ref(gitdir):
                _fail("Git HEAD changed during discovery.")
        existing_state = os.path.lexists(root / ".corinthos")
        index_digest = _identity(indexed)
        staged = {
            path
            for path in indexed.keys() | tree.keys()
            if indexed.get(path, (None, None))[:2] != tree.get(path, (None, None))[:2]
        }
        dirty_tracked = bool(staged) or any(row["state"] != "CLEAN" for row in files)
        tracked_private_state = any(
            _managed_state_path(path) for path in indexed.keys() | tree.keys()
        )
        unverified = any(row["state"] == "UNVERIFIED" for row in files)
        commit_allowed = not (tracked_private_state or unverified)
        candidates = set()
        for manifest in manifests:
            parent = PurePosixPath(manifest["path"]).parent
            if parent != PurePosixPath(".") or manifest["name"] is not None:
                candidates.add((parent.as_posix(), manifest["ecosystem"]))
            for member in manifest["members"]:
                candidate = (parent / member).as_posix()
                if any(path == candidate or path.startswith(candidate + "/") for path in indexed):
                    candidates.add((candidate, manifest["ecosystem"]))
        ambiguities = []
        if dirty_tracked:
            ambiguities.append(
                _ambiguity(
                    "DIRTY_TRACKED",
                    "The index or tracked working tree differs from Git HEAD.",
                    required=True,
                )
            )
        if tracked_private_state:
            ambiguities.append(
                _ambiguity(
                    "TRACKED_PRIVATE_STATE",
                    "Git tracks the reserved root directory for Corinthos private state.",
                )
            )
        if unverified:
            ambiguities.append(
                _ambiguity(
                    "UNVERIFIED_TRACKED", "At least one tracked file cannot be identified safely."
                )
            )
        if untracked_count:
            ambiguities.append(
                _ambiguity(
                    "UNTRACKED_EXCLUDED",
                    "Untracked and ignored paths are excluded from source context.",
                )
            )
        if nested:
            ambiguities.append(
                _ambiguity(
                    "NESTED_GIT",
                    "Nested Git worktrees are identified without traversal.",
                    required=True,
                )
            )
        if "SUBMODULE" in exclusions:
            ambiguities.append(
                _ambiguity(
                    "SUBMODULES",
                    "Submodules are recorded as Git links without traversal.",
                    required=True,
                )
            )
        if "SENSITIVE_PATH" in exclusions:
            ambiguities.append(
                _ambiguity(
                    "SENSITIVE_EXCLUDED",
                    "Tracked sensitive paths are retained as excluded metadata.",
                )
            )
        if "CONTENT_BUDGET" in exclusions:
            ambiguities.append(
                _ambiguity(
                    "CONTENT_LIMIT",
                    "At least one tracked file exceeds the bounded content inventory.",
                )
            )
        if len(files) > _MAX_SUMMARY and (
            sum(row["kind"] == "MANIFEST" for row in files) > _MAX_SUMMARY
            or sum(row["kind"] == "DOCUMENTATION" for row in files) > _MAX_SUMMARY
            or sum(row["kind"] == "TEST" for row in files) > _MAX_SUMMARY
            or len(candidates) > _MAX_SUMMARY
        ):
            ambiguities.append(
                _ambiguity(
                    "SUMMARY_LIMIT", "At least one discovery summary reaches its item limit."
                )
            )
        if sum(manifest["name"] is not None for manifest in manifests) > 1:
            ambiguities.append(
                _ambiguity(
                    "MULTIPLE_MANIFESTS",
                    "Multiple named package manifests are visible.",
                    required=True,
                )
            )
        if not any(manifest["name"] for manifest in manifests):
            ambiguities.append(
                _ambiguity(
                    "PROJECT_NAME_UNKNOWN", "No supported static manifest provides a project name."
                )
            )
        if len(candidates) > 1:
            ambiguities.append(
                _ambiguity(
                    "MULTIPLE_MODULE_ROOTS",
                    "Multiple candidate module roots are visible.",
                    required=True,
                )
            )
        if any(os.path.lexists(root / filename) for filename in ("AGENTS.md", "CLAUDE.md")):
            ambiguities.append(
                _ambiguity(
                    "EXISTING_INSTRUCTIONS", "Existing provider instruction files are retained."
                )
            )
        if existing_state:
            ambiguities.append(
                _ambiguity(
                    "EXISTING_STATE", "Existing Corinthos private state requires status inspection."
                )
            )
        observation = {
            "policy_version": POLICY_VERSION,
            "head": head,
            "branch": None if branch is not None and _sensitive(branch) else branch,
            "branch_digest": None if branch is None else sha256(branch.encode("utf-8")).hexdigest(),
            "dirty": dirty_tracked or bool(untracked_count),
            "tracked_count": len(files),
            "staged_count": len(staged),
            "index_digest": index_digest,
            "untracked_count": untracked_count,
            "untracked_digest": untracked_digest,
            "files": files,
            "manifests": manifests,
            "docs": [row["path"] for row in files if row["kind"] == "DOCUMENTATION"][:_MAX_SUMMARY],
            "tests": [row["path"] for row in files if row["kind"] == "TEST"][:_MAX_SUMMARY],
            "languages": dict(sorted(languages.items())),
            "candidates": [
                {"path": path, "ecosystem": ecosystem}
                for path, ecosystem in sorted(candidates)[:_MAX_SUMMARY]
            ],
            "exclusions": sorted(exclusions),
            "ambiguities": sorted(ambiguities, key=lambda item: item["code"]),
            "commit_allowed": commit_allowed,
            "existing_state": existing_state,
        }
        observation["discovery_digest"] = discovery_digest(observation)
        return root, observation
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        if isinstance(error, ImportDiscoveryError):
            raise
        _fail("The local Git worktree could not be inspected safely.")
