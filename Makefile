.PHONY: lint format-check typecheck test generate verify

lint:
	uv run ruff check src

format-check:
	uv run ruff format --check src

typecheck:
	uv run mypy

test:
	uv run python -m unittest discover -s tests -v

generate:
	uv run generate-data

verify: lint format-check typecheck test
