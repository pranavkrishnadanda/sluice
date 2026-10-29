.PHONY: test test-all test-integration lint fmt cases eval-rules results up up-obs down ps sandbox-image api

# Default selection (pyproject addopts) excludes integration, live and slow.
test:
	cd backend && uv run python -m pytest -q

test-all:
	cd backend && uv run python -m pytest -q -m "not integration and not live"

# Needs the stack (`make up`) and the sandbox image (`make sandbox-image`).
test-integration:
	cd backend && uv run python -m pytest -q -m integration

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .

# The console script cannot import on macOS + Python 3.14 (hidden editable .pth), so run the CLI
# with PYTHONPATH=src.
SLUICE = cd backend && PYTHONPATH=src uv run sluice

cases:
	$(SLUICE) cases --split dev --n 300
	$(SLUICE) cases --split holdout --n 300
	$(SLUICE) cases --verify

eval-rules:
	$(SLUICE) eval --system rules --split dev --trials 1

results:
	$(SLUICE) results

# API server for the UI on 127.0.0.1:8080 (port 8000 is reserved). One worker: the event hub and
# job worker are in-process.
api:
	cd backend && PYTHONPATH=src uv run uvicorn sluice.api.app:create_app --factory --host 127.0.0.1 --port 8080 --workers 1

# Services (compose.yaml). Secrets come from backend/.env (copy backend/.env.example).
COMPOSE = docker compose --env-file backend/.env

up:
	$(COMPOSE) up -d --wait

# `up` plus Phoenix (traces UI and OTLP collector on 127.0.0.1:56006). Export traces by setting
# OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:56006 in backend/.env (see .env.example).
up-obs:
	$(COMPOSE) --profile observability up -d --wait

down:
	$(COMPOSE) --profile observability down

ps:
	$(COMPOSE) ps

# Sandbox for generated transforms. polars is pinned (with hashes) from backend/uv.lock.
SANDBOX_IMAGE = sluice-sandbox:dev

sandbox-image:
	cd backend && PYTHONPATH=src uv run python -m sluice.sandbox.image uv.lock > sandbox/requirements.txt
	docker build --pull=false -t $(SANDBOX_IMAGE) backend/sandbox
