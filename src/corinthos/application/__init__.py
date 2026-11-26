# SPDX-License-Identifier: MPL-2.0
"""Shared local application boundary for the CLI and native JSON-RPC peer."""

from dataclasses import dataclass

from corinthos import __version__


@dataclass(frozen=True)
class VersionInfo:
    version: str


def version():
    return VersionInfo(__version__)
