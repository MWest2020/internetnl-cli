"""Poll a batch run's status until it is done, times out, or fails."""

from __future__ import annotations

import time

from internetnl_cli.errors import ApiError, PollTimeout, RunFailed

_UNFINISHED = {"registering", "running", "generating"}


def poll_until_done(
    client,
    request_id: str,
    interval: float,
    max_seconds: float,
    *,
    sleep=time.sleep,
    monotonic=time.monotonic,
    progress=None,
) -> dict:
    start = monotonic()
    while True:
        reply = client.status(request_id)
        status = reply["request"]["status"]

        if status == "done":
            return reply

        if status in ("error", "cancelled"):
            raise RunFailed(f"run {request_id} ended as {status}")

        if status in _UNFINISHED:
            if progress is not None:
                progress(status)
            elapsed = monotonic() - start
            if elapsed >= max_seconds:
                raise PollTimeout(
                    f"still {status} after {max_seconds:g}s: raise INTERNETNL_POLL_MAX"
                )
            sleep(min(interval, max_seconds - elapsed))
            continue

        raise ApiError(f"unexpected request status '{status}' from {client.endpoint_host}")
