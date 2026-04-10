.PHONY: help init dev worker sync enqueue materialize docker-build docker-up docker-down docker-auth docker-logs docker-shell

help:
	@printf '%s\n' \
	  'make init           - install dependencies and initialize the local DB' \
	  'make dev            - run the FastAPI dev server' \
	  'make worker         - run the serial worker locally' \
	  'make sync           - run an immediate local wacli sync/import' \
	  'make enqueue        - enqueue maintenance jobs locally' \
	  'make materialize    - materialize due runs locally' \
	  'make docker-build   - build the Docker image' \
	  'make docker-auth    - run wacli auth inside the Docker environment' \
	  'make docker-up      - start web and worker with Docker Compose' \
	  'make docker-down    - stop Docker Compose services' \
	  'make docker-logs    - follow Compose logs' \
	  'make docker-shell   - open a shell in the web container'

init:
	uv sync
	uv run wa-scheduler init-db

dev:
	uv run uvicorn wa_scheduler.main:app --reload

worker:
	uv run wa-scheduler worker

sync:
	uv run wa-scheduler sync-now

enqueue:
	uv run wa-scheduler enqueue-sync

materialize:
	uv run wa-scheduler materialize

docker-build:
	docker compose build

docker-auth:
	./scripts/docker-auth.sh

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-shell:
	docker compose exec web sh
