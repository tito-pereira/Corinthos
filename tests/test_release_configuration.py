# SPDX-License-Identifier: MPL-2.0
"""Keep retained desktop and backend compatibility metadata aligned."""

import json
import tomllib
from pathlib import Path

import corinthos
from corinthos.application import sidecar
from corinthos.workspace import WORKSPACE_FORMAT


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
    assert WORKSPACE_FORMAT == 1
    assert sidecar.SUPPORTED_PROTOCOLS == (1, 2)
