.PHONY: test test-all test-integration lint fmt cases eval-rules results up down ps sandbox-image

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

# Services (compose.yaml). Secrets come from backend/.env (copy backend/.env.example).
COMPOSE = docker compose --env-file backend/.env

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

ps:
	$(COMPOSE) ps

# Sandbox for generated transforms. polars is pinned (with hashes) from backend/uv.lock.
SANDBOX_IMAGE = sluice-sandbox:dev

sandbox-image:
	cd backend && PYTHONPATH=src uv run python -m sluice.sandbox.image uv.lock > sandbox/requirements.txt
	docker build --pull=false -t $(SANDBOX_IMAGE) backend/sandbox
