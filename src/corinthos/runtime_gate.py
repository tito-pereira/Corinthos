# SPDX-License-Identifier: MPL-2.0
"""Exclude installation replacement while an installed client owns runtime state."""

import ctypes
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from corinthos import __version__
from corinthos.contracts.artifacts import is_link


def plain_path(path: Path, *, file: bool = False) -> None:
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        expected = stat.S_ISREG if file and current == path else stat.S_ISDIR
        if (
            is_link(info)
            or not expected(info.st_mode)
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
        ):
            raise RuntimeError("Corinthos requires ordinary, unredirected local paths.")


def installation_root() -> Path | None:
    if os.name != "nt":
        return None
    value = os.environ.get("LOCALAPPDATA", "")
    if not value or not Path(value).is_absolute() or Path(value).drive.startswith("\\\\"):
        raise RuntimeError("LOCALAPPDATA must identify a local user directory.")
    root = Path(value) / "Programs" / "Corinthos"
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(
        str(root / "runtime/python.exe")
    ):
        return None
    plain_path(root)
    return root


@contextmanager
def client_runtime():
    """Keep a shared Windows handle; the updater must acquire this file exclusively."""
    root = installation_root()
    if root is None:
        yield
        return
    path = root.parent / ".Corinthos-runtime.lock"
    plain_path(path, file=True)
    from ctypes import wintypes

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
    handle = kernel.CreateFileW(str(path), 0xC0000000, 3, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise RuntimeError("Corinthos is being updated. Wait for the update and reopen it.")
    try:
        plain_path(path, file=True)
        marker = root / "installation.json"
        plain_path(marker, file=True)
        if marker.stat().st_size > 256 * 1024:
            raise RuntimeError("The Corinthos installation marker is invalid.")
        value = json.loads(marker.read_bytes())
        if (
            value.get("owner") != "corinthos-core"
            or value.get("schema") != 1
            or value.get("state") != "installed"
            or value.get("version") != __version__
        ):
            raise RuntimeError(
                "The installed runtime changed. Reopen Corinthos after setup finishes."
            )
        yield
    finally:
        kernel.CloseHandle(handle)
