# Vite 8 requires Node 20.19+ / 22.12+ — use the Node 22 LTS line (a stale
# cached node:20-slim image can be older than 20.19 and break the build).
FROM node:22-slim AS frontend-builder
WORKDIR /frontend
RUN corepack enable
# pnpm with a BuildKit cache mount for the content-addressable store:
# repeat builds only download what the lockfile diff actually changed.
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --store-dir=/pnpm-store
COPY frontend/ .
RUN pnpm run build

FROM python:3.12-slim
WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies via uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY app/ ./app/
COPY tests/ ./tests/
COPY --from=frontend-builder /app/static ./app/static/

RUN mkdir -p /app/data

EXPOSE 9001

CMD ["uv", "run", "--no-project", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001"]
