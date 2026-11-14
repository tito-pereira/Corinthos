# SPDX-License-Identifier: MPL-2.0
"""Desktop operations over the existing provider settings owners."""

from dataclasses import asdict

from corinthos.application import providers_status
from corinthos.bootstrap import configure_adapter
from corinthos.project_status import _absolute_root
from corinthos.providers import SUPPORTED_PROVIDERS, configure_provider, refresh_providers


def settings_providers(root=None):
    return [asdict(provider) for provider in providers_status(root)]


def providers_refresh():
    refresh_providers()
    return settings_providers()


def _configure(provider, executable, root):
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Select a supported provider.")
    if root is None:
        configure_provider(provider, executable)
    else:
        configure_adapter(_absolute_root(root), provider, executable)
    return settings_providers(root)


def provider_configure(provider, executable, root=None):
    if (
        not isinstance(executable, str)
        or not 1 <= len(executable) <= 4096
        or any(not character.isprintable() for character in executable)
    ):
        raise ValueError("Supply an absolute executable path.")
    return _configure(provider, executable, root)


def provider_reset(provider, root=None):
    return _configure(provider, None, root)
