.PHONY: lint format-check typecheck test-inventory test regression-test diff-check generate verify regression

lint:
	uv run ruff check src

format-check:
	uv run ruff format --check src

typecheck:
	uv run mypy

test-inventory:
	uv run python tests/assert_inventory.py

test: test-inventory
	uv run python -m unittest discover -s tests/update -t . -v

regression-test:
	uv run python -m unittest discover -s tests/regression -t . -v

diff-check:
	git diff --check
	git diff --cached --check

generate:
	uv run generate-data

verify: lint format-check typecheck test

regression: verify diff-check regression-test
