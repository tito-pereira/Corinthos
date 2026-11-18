# SPDX-License-Identifier: MPL-2.0
"""Shared, passive read operations for the CLI and desktop sidecar."""

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from corinthos import __version__
from corinthos.bootstrap import show_adapters
from corinthos.contracts import ContractError, ContractStore
from corinthos.project_status import MAX_ROOT_CHARS, _absolute_root, _inspect_path
from corinthos.project_status import ProjectStatus as ProjectStatus
from corinthos.project_status import project_status as project_status
from corinthos.providers import show_providers

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")


@dataclass(frozen=True)
class VersionInfo:
    version: str


@dataclass(frozen=True)
class KnownCapabilities:
    version: str | None
    available: bool
    json_events: bool
    stdin_prompt: bool
    native_resume: bool
    read_only: bool


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    supported: bool
    configuration: Literal["operational", "initial", "global", "default"]
    executable: str | None
    observation: Literal["OBSERVED", "NOT_OBSERVED", "UNKNOWN"]
    capabilities: KnownCapabilities | None


def version() -> VersionInfo:
    return VersionInfo(__version__)


def diagnostics() -> dict:
    """Describe this running interpreter and supported storage without opening state."""
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "supported_schema_version": ContractStore.current_schema_version(),
    }


def _known_capabilities(row: dict, executable: str | None) -> KnownCapabilities | None:
    cached = row["observed"]
    if cached is None or not row["supported"]:
        return None
    observed_path = cached["executable"]
    if executable is None:
        matches = row["executable"] is None and cached["discovery"] != "explicit"
    else:
        matches = observed_path is not None and os.path.normcase(observed_path) == os.path.normcase(
            executable
        )
    if not matches:
        return None
    observed_version = cached["version"]
    return KnownCapabilities(
        version=(
            observed_version
            if observed_version
            and len(observed_version) <= 128
            and _VERSION.fullmatch(observed_version)
            else None
        ),
        available=cached["available"],
        json_events=cached["json_events"],
        stdin_prompt=cached["stdin_prompt"],
        native_resume=cached["native_resume"],
        read_only=cached["read_only"],
    )


def providers_status(root: str | Path | None = None) -> list[ProviderStatus]:
    """Read saved choices and historical observations without discovery or probing."""
    choices = {}
    if root is not None:
        path, _, is_directory = _inspect_path(_absolute_root(root))
        if is_directory:
            choices = {row["provider"]: row for row in show_adapters(path)}
    results = []
    for row in show_providers():
        choice = choices.get(row["provider"])
        executable = row["executable"] if choice is None else choice["executable"]
        if executable is not None and len(executable) > MAX_ROOT_CHARS:
            raise ContractError("The configured executable path cannot be represented.")
        configuration = (
            ("global" if executable else "default") if choice is None else choice["configuration"]
        )
        capabilities = _known_capabilities(row, executable)
        observation = (
            "OBSERVED" if capabilities else "NOT_OBSERVED" if row["observed"] is None else "UNKNOWN"
        )
        results.append(
            ProviderStatus(
                row["provider"],
                row["supported"],
                configuration,
                executable,
                observation,
                capabilities,
            )
        )
    return results
