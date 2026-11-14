# SPDX-License-Identifier: MPL-2.0
"""Verify the public release mapping and local diagnostic boundary."""

import json
import tomllib
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import corinthos
from corinthos.application import cli, session_cli, sessions, sidecar
from corinthos.contracts.store import ContractStore


def test_desktop_release_mapping_matches_backend():
    root = Path(__file__).resolve().parents[1]
    desktop = root / "apps" / "desktop"
    package = json.loads((desktop / "package.json").read_text(encoding="utf-8"))
    npm = json.loads((desktop / "package-lock.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((desktop / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    tauri = json.loads((desktop / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert {
        corinthos.__version__,
        package["version"],
        npm["version"],
        npm["packages"][""]["version"],
        cargo["package"]["version"],
        tauri["version"],
    } == {"0.1.0"}
    assert tauri["identifier"] == "org.corinthos.desktop"
    assert ContractStore.current_schema_version() == 9
    assert sidecar.SUPPORTED_PROTOCOLS == (1, 2)


def test_application_cli_redacts_classified_values_in_errors(capsys, monkeypatch):
    def fail():
        raise ValueError("Check the input. API_KEY=test-value")

    class SessionApplication:
        list = staticmethod(fail)
        supervisor = SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(sessions, "SessionApplication", SessionApplication)
    assert cli._workspace(Namespace(arguments=(), operation=fail)) == 1
    arguments = Namespace(command="session", session_action="list", handler=session_cli._run)
    assert session_cli._run(arguments) == 1
    errors = [json.loads(line)["error"] for line in capsys.readouterr().err.splitlines()]
    assert errors == ["Check the input. [redacted sensitive assignment]"] * 2
