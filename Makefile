.PHONY: lint format-check typecheck simple-config-smoke provider-generator-smoke member-claim-generator-smoke scenario-survivorship-smoke incomplete-scenario-smoke simple-engine-smoke simple-cli-smoke generate verify

lint:
	uv run ruff check src tools

format-check:
	uv run ruff format --check src tools

typecheck:
	uv run mypy

simple-config-smoke:
	uv run python tools/simple_config_smoke.py

provider-generator-smoke:
	uv run python tools/provider_generator_smoke.py

member-claim-generator-smoke:
	uv run python tools/member_claim_generator_smoke.py

scenario-survivorship-smoke:
	uv run python tools/scenario_survivorship_smoke.py

incomplete-scenario-smoke:
	uv run python tools/incomplete_scenario_smoke.py

simple-engine-smoke:
	uv run python tools/simple_engine_smoke.py

simple-cli-smoke:
	uv run python tools/simple_cli_smoke.py

generate:
	uv run python -m healthcare_test_data generate --config generator.config.json

verify: lint format-check typecheck simple-config-smoke provider-generator-smoke member-claim-generator-smoke scenario-survivorship-smoke incomplete-scenario-smoke simple-engine-smoke simple-cli-smoke
