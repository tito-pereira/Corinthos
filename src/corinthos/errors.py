# SPDX-License-Identifier: MPL-2.0
"""Ordinary application errors, independent of project governance."""


class CorinthosError(RuntimeError):
    """An actionable local operation failure."""


class IntegrityError(CorinthosError):
    """Invalid input or unsafe local state."""


class ConflictError(CorinthosError):
    """A concurrent operation or changed destination prevents a write."""
