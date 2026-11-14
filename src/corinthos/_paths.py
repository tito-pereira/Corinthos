# SPDX-License-Identifier: MPL-2.0
"""Lexical Windows component checks on every supported Python and platform."""

import ntpath

_native_isreserved = getattr(ntpath, "isreserved", None)
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{device}{number}" for device in ("COM", "LPT") for number in "123456789¹²³"
}


def windows_component_is_reserved(value: str) -> bool:
    """Check one component without letting a colon parse as a drive prefix."""
    if any(char in value for char in "/\\:"):
        return True
    if _native_isreserved is not None:
        return _native_isreserved(value)
    if value.endswith((".", " ")):
        return value not in {".", ".."}
    if any(ord(char) < 32 or char in '"*<>?|' for char in value):
        return True
    return value.partition(".")[0].rstrip(" ").upper() in _DEVICE_NAMES
