.PHONY: lint format-check typecheck test generate extract-source verify

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

extract-source:
	uv run python schema/tools/extract-member-provider-claims-source.py \
		Member_Provider_ClaimsKeysAndSurvivorship\(5\).docx \
		--output /tmp/member-provider-claims-source.json \
		--catalog src/test_data_generator/configuration/member-provider-claims-key-survivorship.json \
		--check-catalog

verify: lint format-check typecheck test
