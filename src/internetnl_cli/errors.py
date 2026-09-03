"""Exit-code contract for internetnl-cli.

This is the single place exit codes live. See design.md for the pinned
meaning of each code.
"""

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_API = 2
EXIT_GATE = 3
EXIT_POLL_TIMEOUT = 4


class InternetnlError(Exception):
    """Base class for all errors the CLI knows how to report cleanly."""

    exit_code = EXIT_API


class ConfigError(InternetnlError):
    """Configuration is missing, unreadable, or invalid."""

    exit_code = EXIT_CONFIG


class TransportError(InternetnlError):
    """The endpoint could not be reached at all (no HTTP reply)."""

    exit_code = EXIT_API


class ApiError(InternetnlError):
    """The endpoint replied, but with an error or a malformed body.

    `status` carries the raw HTTP status of the reply that caused this
    error, when one exists (`None` for a purely local failure raised
    before any HTTP call was made, e.g. an invalid request id). A caller
    that needs the status to decide how to map the error onward (the
    facade does, in `netnl.api._translate_api_error`) reads it from here
    rather than out-of-band — see that module for why an out-of-band
    channel (`threading.local`) used to exist and was removed.
    """

    exit_code = EXIT_API

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RunFailed(InternetnlError):
    """The batch run itself ended as `error` or `cancelled`."""

    exit_code = EXIT_API


class GateTripped(InternetnlError):
    """`--fail-on-scored` found a non-allowlisted failed test."""

    exit_code = EXIT_GATE


class PollTimeout(InternetnlError):
    """`INTERNETNL_POLL_MAX` was exceeded while the run was unfinished."""

    exit_code = EXIT_POLL_TIMEOUT
