.PHONY: lint format-check typecheck generate verify

lint:
	uv run ruff check src

format-check:
	uv run ruff format --check src

typecheck:
	uv run mypy

generate:
	uv run python -m healthcare_test_data generate --config generator.config.json

verify: lint format-check typecheck
