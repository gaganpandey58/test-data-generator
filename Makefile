.PHONY: lint format-check typecheck generate verify

lint:
	uv run ruff check src

format-check:
	uv run ruff format --check src

typecheck:
	uv run mypy

generate:
	uv run generate-data

verify: lint format-check typecheck
