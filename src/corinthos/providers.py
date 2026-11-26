# SPDX-License-Identifier: MPL-2.0
"""Explicit native command vectors; skills own the development process."""

import os
import shutil
from pathlib import Path

from corinthos.errors import IntegrityError
from corinthos.user_state import read_state, update_state

_DEFAULT = {"version": 1, "commands": {}}


def validate_command(command):
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 64
        or any(not isinstance(arg, str) or len(arg) > 8192 or "\0" in arg for arg in command)
        or not command[0]
    ):
        raise IntegrityError("Provide an executable and bounded argument vector.")
    executable = shutil.which(command[0])
    if not executable:
        raise IntegrityError(
            "Provider executable is unavailable. Configure an installed executable."
        )
    executable = str(Path(executable).absolute())
    if os.name == "nt" and Path(executable).suffix.casefold() != ".exe":
        raise IntegrityError("Use a native .exe; shell command wrappers are unsupported.")
    return [executable, *command[1:]]


def _commands():
    value = read_state("providers.json", _DEFAULT)
    if value.keys() - {"version", "commands", "providers"} or not isinstance(
        value.get("commands", {}), dict
    ):
        raise IntegrityError("Unsupported provider registry; existing choices are preserved.")
    return value.get("commands", {})


def configure_provider(provider, command):
    if (
        not isinstance(provider, str)
        or not provider
        or len(provider) > 64
        or not all(c.isalnum() or c in "-_" for c in provider)
    ):
        raise IntegrityError(
            "Use a short provider name containing letters, digits, dash or underscore."
        )
    command = validate_command(command)

    def transform(value):
        value.setdefault("commands", {})[provider] = command
        if len(value["commands"]) > 32:
            raise IntegrityError("At most 32 configured providers are supported.")
        return value

    return update_state("providers.json", _DEFAULT, transform)


def command_for(provider):
    command = _commands().get(provider)
    if command is None:
        raise IntegrityError("Configure this provider's executable and arguments first.")
    return validate_command(command)


def show_providers(root=None):
    return [
        {"provider": name, "command": command, "available": bool(shutil.which(command[0]))}
        for name, command in _commands().items()
    ]
