"""Entry point for `netnl-serve`."""

from __future__ import annotations

import os
import sys
from typing import Callable, Mapping

from fastapi import FastAPI

from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load


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
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
