# Facade image for netnl-serve — see design.md, "Deployment (section 4.1)".
#
# Reproducible install via uv + the committed lockfile (`uv sync --frozen`).
# Runs as a non-root user. Binds only inside the container (0.0.0.0:8000);
# no port is published here — that is the compose file's decision, and this
# image never publishes one on its own.

# Pinned to the python:3.12-slim multi-arch index digest for a fully
# reproducible build. Keep the tag in this comment; bump both together.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# Pin the uv binary's source image by digest (tag 0.7.13) so the install step
# is reproducible independent of the base image's package index state.
COPY --from=ghcr.io/astral-sh/uv:0.7.13@sha256:6c1e19020ec221986a210027040044a5df8de762eb36d5240e382bc41d7a9043 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    NETNL_HOST=0.0.0.0 \
    NETNL_PORT=8000

WORKDIR /app

# Dependencies first (better layer caching); locked and frozen — no
# resolution happens at build time.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --extra api

# Non-root user. /data is a mount point for the SQLite DB (NETNL_DB); it is
# created here so the volume owner is correct from the first start.
RUN groupadd --system netnl \
    && useradd --system --gid netnl --home-dir /app --no-create-home netnl \
    && mkdir -p /data \
    && chown -R netnl:netnl /app /data

USER netnl

CMD ["netnl-serve"]
