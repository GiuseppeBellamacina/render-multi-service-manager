# render-service-manager - single manager for multiple scheduled services.
# Docker (not the native runtime) because a managed service may need the
# `ssh` and `scp` binaries at runtime (subprocess), and Render's native
# Python runtime does not guarantee them.
FROM python:3.12-slim

# ssh/scp required at runtime by the cluster driver service (t2g): ssh tick
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv resolves and installs the dependencies from pyproject.toml + uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependencies BEFORE code: the daily fetch downloads only the user scripts
# (fastapi/requests/httpx/upstash/dotenv stay fixed in the image).
# If a source script adds a new dependency -> add it to pyproject.toml, run
# `uv lock` and redeploy the manager.
#
# --frozen installs EXACTLY what uv.lock pins and fails if the lock is out of
# date, so a deploy can never silently resolve different versions than the
# ones tested locally. --no-dev keeps ruff/black/isort out of the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# uv installs into /app/.venv: put it first on PATH so `uvicorn` and any
# `python` subprocess resolve to the locked environment.
ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

# The SECRET manifest never enters the image: it arrives via
# MANIFEST_CONTENT or MANIFEST_REPO at runtime.
ENV PORT=10000
EXPOSE 10000

# Render injects $PORT; locally: docker run -p 8000:10000 ...
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]