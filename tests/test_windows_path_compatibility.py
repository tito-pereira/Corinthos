# SPDX-License-Identifier: MPL-2.0
"""Verify Windows component rules on native and fallback Python runtimes."""

import ntpath

import pytest

from corinthos import _paths

_RESERVED = (
    "CON",
    "con.txt",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "LPT9",
    "conin$",
    "CONOUT$",
    "com¹.log",
    "LPT³.log",
    "nul .txt",
    "name.",
    "name ",
    "name:stream",
    'bad"name',
    "bad*name",
    "bad<name",
    "bad>name",
    "bad?name",
    "bad|name",
    "bad\x00name",
    "bad\x1fname",
)
_ORDINARY = ("storage.py", "concord.txt", "COM10", "LPT11", "NULIX", ".env.example", "audit.v1.md")


@pytest.mark.parametrize("fallback", [False, True])
def test_windows_reserved_names(monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(_paths, "_native_isreserved", None)
    for value in (*_RESERVED, "n:stream", "dir/file", r"dir\file"):
        assert _paths.windows_component_is_reserved(value), repr(value)
    for value in _ORDINARY:
        assert not _paths.windows_component_is_reserved(value), repr(value)


@pytest.mark.skipif(not hasattr(ntpath, "isreserved"), reason="Requires Python 3.13 or later.")
def test_windows_fallback_agrees_with_native_component_semantics(monkeypatch):
    native = ntpath.isreserved
    monkeypatch.setattr(_paths, "_native_isreserved", None)
    for value in (*_RESERVED, *_ORDINARY):
        assert _paths.windows_component_is_reserved(value) == native(value), repr(value)
