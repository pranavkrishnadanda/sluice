.PHONY: test test-all lint fmt cases

test:
	cd backend && uv run pytest -q -m "not slow"

test-all:
	cd backend && uv run pytest -q

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
