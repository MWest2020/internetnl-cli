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
    """An error that maps directly onto a v2-shaped HTTP reply.

    `headers` (round-3 fix): an optional dict of extra response headers the
    raising site wants attached — e.g. `Retry-After` on a 503 `overloaded`
    (see `netnl.auth._overloaded`). `None` by default; the exception
    handler in `api.py` merges these with the header(s) it already adds
    itself (e.g. `WWW-Authenticate` for a 401), so a raising site never
    needs to know about that logic.
    """

    def __init__(
        self, status: int, label: str, msg: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(f"{status} {label}: {msg}")
        self.status = status
        self.label = label
        self.msg = msg
        self.headers = headers
