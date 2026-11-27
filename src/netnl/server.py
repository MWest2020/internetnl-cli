"""Entry point for `netnl-serve`."""

from __future__ import annotations

import copy
import logging
import os
import sys
from typing import Callable, Mapping

from fastapi import FastAPI

from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

# Paths whose requests never reach uvicorn's access log
# (openspec/changes/add-bazibo-unlock): the access line carries the peer
# address, and for `/bazibo/*` the promise is that no address is logged.
_PRIVATE_PATH_PREFIXES = ("/bazibo/",)


class PrivatePathFilter(logging.Filter):
    """Drops uvicorn access-log records for `_PRIVATE_PATH_PREFIXES`.
    uvicorn's access record args are (client, method, path, http, status).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2])
            if path.startswith(_PRIVATE_PATH_PREFIXES):
                return False
        return True


def log_config() -> dict:
    """uvicorn's default logging config plus `PrivatePathFilter` on the
    access handler."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["netnl_private_paths"] = {"()": PrivatePathFilter}
    cfg["handlers"]["access"]["filters"] = ["netnl_private_paths"]
    return cfg


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    run: Callable[[FastAPI], None] | None = None,
) -> int:
    env = env if env is not None else os.environ

    try:
        settings = load(env)
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    app = create_app(settings)

    if run is not None:
        run(app)
        return 0

    import uvicorn

    host = env.get("NETNL_HOST", "127.0.0.1")
    port = int(env.get("NETNL_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_config=log_config())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
