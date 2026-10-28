.PHONY: test test-all lint fmt

test:
	cd backend && uv run pytest -q -m "not slow"

test-all:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check . && uv run ruff format --check .

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .
