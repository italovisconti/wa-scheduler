<div align="center">

<img src="./logo.svg" width="96" alt="wa-scheduler logo" />

# wa-scheduler

*Self-hosted personal WhatsApp message scheduling built on top of [`wacli`](https://github.com/steipete/wacli/).*

![Python](https://img.shields.io/badge/Python-3.12+-3c78d8?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-0.135.3-009688?style=flat-square)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0.49-d71f00?style=flat-square)
![wacli](https://img.shields.io/badge/wacli-v0.2.0-555?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ed?style=flat-square)

[Overview](#overview) • [Features](#features) • [Quick start](#quick-start-with-docker) • [How it works](#how-it-works) • [Local development](#local-development)

</div>

`wa-scheduler` is a small web app for a very specific problem: scheduling WhatsApp messages in a self-hosted, single-user setup.

It is built for the personal use case:

- one WhatsApp account per installation
- one operator
- one worker process
- simple deployment with Docker Compose

Instead of integrating directly with WhatsApp internals, the project uses [`wacli`](https://github.com/steipete/wacli/) as the execution engine and wraps it with a FastAPI web UI, a SQLite-backed job queue, and a serial worker.

> [!IMPORTANT]
> This project relies on `wacli`, which in turn relies on a non-official WhatsApp client stack. Treat it as a personal utility, not a guaranteed business-critical messaging platform.

## Overview

The current MVP is designed around a simple constraint: `wacli` uses an exclusive lock on its store, so concurrent access is a bad fit.

That constraint shapes the architecture:

- **FastAPI + Jinja2** for the UI
- **SQLAlchemy + SQLite** for persistence
- **one serial worker** for all `wacli` commands
- **Docker Compose** for the default self-hosted deployment

This gives you a low-friction stack that is easy to understand, easy to run, and well aligned with a personal single-account setup.

## Features

### Available now

- schedule **one-time** messages
- schedule **daily**, **weekly**, and **monthly** recurring messages
- send a **message now** without creating a schedule
- attach a file to an immediate send or a scheduled message
- import **contacts**, **chats**, and **groups** from `wacli`
- save reusable **message templates**
- send **attachments**
- inspect **run history**, errors, and retries
- perform **manual retries** from the UI
- run **health checks** against `wacli`
- deploy as **web + worker** with Docker Compose

### Current scope

- single-user
- single-account
- no Redis
- no Celery
- no multi-tenant complexity

## Quick Start With Docker

For the intended use case, Docker Compose is the easiest way to run the project.

### 1. Create your environment file

```bash
cp .env.docker.example .env
```

### 2. Build the image

```bash
docker compose build
```

### 3. Authenticate WhatsApp inside the container environment

```bash
./scripts/docker-auth.sh
```

This is important: the authentication must happen against the same persistent `/wacli-store` volume that the worker will use later.

### 4. Start the app

```bash
docker compose up -d
```

Open `http://127.0.0.1:8000`.

### 5. Enqueue an initial sync

```bash
docker compose exec web uv run wa-scheduler enqueue-sync
```

The worker will consume those jobs and import data into the local database.

> [!TIP]
> If you prefer short commands, the project includes a `Makefile`:
>
> ```bash
> make docker-build
> make docker-auth
> make docker-up
> make docker-logs
> ```

## How It Works

There is no Redis queue in this project.

Instead, the queue lives in the database:

- `Schedule`: the rule you define in the UI
- `ScheduledRun`: a concrete occurrence derived from that rule
- `OutboundJob`: the executable job consumed by the worker

Runtime flow:

```text
Schedule -> ScheduledRun -> OutboundJob -> Worker -> wacli -> result stored in DB
```

### Why a worker?

The worker is a long-lived process that:

1. materializes due runs
2. reads pending jobs from the database
3. executes the corresponding `wacli` command
4. stores stdout, stderr, status, and retry state back in the database

This design is intentional.

> [!IMPORTANT]
> The worker should be the only long-lived process calling `wacli` for a given account.
> Running a separate `wacli sync --follow` against the same store can cause lock conflicts and failed jobs.

### One-time schedules

Yes, one-time scheduling is supported.

For example, you can create a schedule for **today at 8:00 PM**, send it once, and never repeat it. After a successful send, the schedule is left without a next run and is marked inactive.

## What The UI Covers

The web UI currently includes:

- **Dashboard**: health, counts, recent runs
- **Contacts**: imported contacts with editable alias, tags, and search filters
- **Chats**: imported chats and groups
- **Templates**: create, edit, delete message templates
- **Schedules**: create, edit, pause, delete schedules
- **Runs**: inspect executions and retry failures

Use **Send Now** from the dashboard, or open the dedicated page from a contact or chat to send a test message immediately.

Attachments can be provided in two ways:

- upload a file from the browser
- provide a manual file path already available on disk

Uploaded files are copied into `data/attachments/` so the worker can still access them later.

## Useful Commands

### Docker Compose

```bash
docker compose logs -f web
docker compose logs -f worker
docker compose exec web uv run wa-scheduler enqueue-sync
docker compose exec web uv run wa-scheduler materialize
docker compose exec web wacli --json doctor --store /wacli-store
```

### Local CLI

```bash
uv run wa-scheduler init-db
uv run wa-scheduler sync-now
uv run wa-scheduler enqueue-sync
uv run wa-scheduler materialize
uv run wa-scheduler worker
uv run wa-scheduler worker --once
```

### Makefile shortcuts

```bash
make init
make dev
make worker
make sync
make enqueue
make materialize
make docker-build
make docker-auth
make docker-up
make docker-down
make docker-logs
```

## Local Development

If you want to run the app without Docker:

### Requirements

- Python `3.12+`
- [`uv`](https://github.com/astral-sh/uv)
- `wacli` installed locally and already authenticated

### Setup

```bash
cp .env.example .env
uv sync
uv run wa-scheduler init-db
```

### Run the web app

```bash
uv run uvicorn wa_scheduler.main:app --reload
```

### Run the worker

```bash
uv run wa-scheduler worker
```

### Run a one-off sync

```bash
uv run wa-scheduler sync-now
```

## Data And Persistence

Default local layout:

- database: `./data/wa-scheduler.db`
- uploaded attachments: `./data/attachments`
- default `wacli` store: `~/.wacli`

Docker volumes:

- `wa_scheduler_data` for the app database and attachments
- `wacli_store` for the WhatsApp linked-device session used by `wacli`

## Project Structure

```text
wa_scheduler/
  routes/       FastAPI routes and UI handlers
  services/     wacli adapter, sync, scheduler, worker, health
  templates/    Jinja2 templates
  static/       CSS assets
Dockerfile
docker-compose.yml
Makefile
```

## Notes And Limitations

> [!NOTE]
> This project is intentionally optimized for a small personal deployment, not for high concurrency.

Known constraints:

- one account per installation
- one worker process by design
- SQLite-backed queue instead of Redis/Celery
- delivery timing depends on the worker being up at the right time
- `wacli` and WhatsApp behavior may change over time

## Roadmap

Near-term improvements that still fit the project shape:

- better filtering and search in the UI
- clearer validation and preview flows
- schedule duplication
- stronger operational visibility
- safer handling for file lifecycle and cleanup

If the project proves valuable beyond the personal use case, the next step would be revisiting the execution layer, not adding more complexity around the current single-worker model.
