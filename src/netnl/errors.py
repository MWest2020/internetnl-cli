"""Exception hierarchy for the netnl facade.

Kept separate from `internetnl_cli.errors`: the facade's HTTP-facing errors
carry a v2 label and status, not an exit code.
"""

from __future__ import annotations


class NetnlError(Exception):
    """Base class for all errors the facade knows how to report cleanly."""


class SettingsError(NetnlError):
    """Configuration read from the environment is missing or invalid."""


class NetnlHTTPError(NetnlError):
    """An error that maps directly onto a v2-shaped HTTP reply."""

    def __init__(self, status: int, label: str, msg: str) -> None:
        super().__init__(f"{status} {label}: {msg}")
        self.status = status
        self.label = label
        self.msg = msg
