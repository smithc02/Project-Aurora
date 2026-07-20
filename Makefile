.PHONY: setup run lint format typecheck test check

setup:
	uv sync --all-groups

run:
	uv run aurora --check

lint:
	uv run ruff check .

format:
	uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest --cov=aurora_core --cov-report=term-missing

check:
	./scripts/check.sh
