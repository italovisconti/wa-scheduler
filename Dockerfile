FROM golang:1.25-bookworm AS wacli-builder

ARG WACLI_VERSION=v0.2.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch ${WACLI_VERSION} https://github.com/steipete/wacli.git /src/wacli

WORKDIR /src/wacli

RUN go build -tags sqlite_fts5 -ldflags "-X main.version=${WACLI_VERSION}" -o /out/wacli ./cmd/wacli


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/usr/local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /uvx /usr/local/bin/
COPY --from=wacli-builder /out/wacli /usr/local/bin/wacli

WORKDIR /app

COPY pyproject.toml README.md ./
COPY wa_scheduler ./wa_scheduler
COPY .env.example ./.env.example
COPY PLAN.md ./PLAN.md

RUN uv sync --no-dev

RUN mkdir -p /app/data/attachments /wacli-store

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "wa_scheduler.main:app", "--host", "0.0.0.0", "--port", "8000"]
