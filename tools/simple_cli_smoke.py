"""Manually smoke-test the simple module command-line interface."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]


def main() -> None:
    """Verify CLI generation, schema validation, determinism, and safe output.

    Raises:
        AssertionError: If the command-line contract leaks data, accepts bad
            configuration, or produces non-deterministic output.
    """
    repository_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        config_path = _write_provider_config(repository_root, temporary_root)
        output_path = temporary_root / "output" / "providers.jsonl"

        first_result = _run_cli(config_path, repository_root)
        first_bytes = output_path.read_bytes()
        records = [json.loads(line) for line in first_bytes.splitlines()]
        assert len(records) == 3
        _validate_records(records, repository_root)
        _assert_safe_stdout(first_result.stdout, records)

        _assert_invalid_config_error(config_path, repository_root)
        _assert_missing_schema_error(config_path, repository_root)
        _assert_import_error(config_path, repository_root, records)
        _assert_invalid_schema_error(config_path, repository_root, records)

        second_result = _run_cli(config_path, repository_root)
        assert output_path.read_bytes() == first_bytes
        _assert_safe_stdout(second_result.stdout, records)
        _assert_selected_set_output(repository_root, temporary_root)
        _assert_all_entities_output(repository_root, temporary_root)
        _assert_minimal_config_and_stale_cleanup(repository_root, temporary_root)


def _write_provider_config(repository_root: Path, temporary_root: Path) -> Path:
    """Create a three-record provider configuration under a temporary root.

    Args:
        repository_root: Project root containing the base configuration.
        temporary_root: Temporary root that will own the derived config.

    Returns:
        Path to the generated smoke configuration.
    """
    config = _load_json(repository_root / "generator.config.json")
    provider = config["entities"]["provider"]
    provider["count"] = 3
    provider["scenarios"] = {"new": 1}
    provider["schema"] = str(repository_root / "schemas/provider/provider.schema.json")
    for entity_name in ("member", "claim"):
        config["entities"][entity_name]["enabled"] = False
        config["entities"][entity_name]["count"] = 0
    config["output_directory"] = "./output"
    config_path = temporary_root / "generator.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _assert_selected_set_output(repository_root: Path, temporary_root: Path) -> None:
    """Prove a selected provider/member set writes exact records and no extras."""
    config = _load_json(repository_root / "generator.config.json")
    for entity_name in ("provider", "member"):
        config["entities"][entity_name]["count"] = 1
        config["entities"][entity_name]["schema"] = str(
            repository_root / "schemas" / entity_name / f"{entity_name}.schema.json"
        )
        config["entities"][entity_name]["scenarios"] = {"new": 1}
    config["entities"]["claim"]["enabled"] = False
    config["entities"]["claim"]["count"] = 0
    config["output_directory"] = "./selected-output"
    config_path = temporary_root / "selected-set.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = _run_cli(config_path, repository_root)
    output_directory = temporary_root / "selected-output"
    _assert_output_files(output_directory, {"providers.jsonl", "members.jsonl"})
    assert _line_count(output_directory / "providers.jsonl") == 1
    assert _line_count(output_directory / "members.jsonl") == 1
    assert "provider: 1 records" in result.stdout
    assert "member: 1 records" in result.stdout


def _assert_all_entities_output(repository_root: Path, temporary_root: Path) -> None:
    """Prove all configured entities honour profiles, counts, and JSONL-only output."""
    config = _load_json(repository_root / "generator.config.json")
    for entity_name, count in {"provider": 2, "member": 2, "claim": 4}.items():
        config["entities"][entity_name]["count"] = count
        config["entities"][entity_name]["schema"] = str(
            repository_root / "schemas" / entity_name / f"{entity_name}.schema.json"
        )
    config["entities"]["provider"]["scenarios"] = {"new": 1}
    config["entities"]["member"]["scenarios"] = {"changed": 1}
    claim = config["entities"]["claim"]
    claim["profile"] = "claim-institutional"
    claim["scenarios"] = {"replacement": 1, "void": 1, "orphan_payment": 1}
    config["output_directory"] = "./all-output"
    config_path = temporary_root / "all-entities.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    first_result = _run_cli(config_path, repository_root)
    output_directory = temporary_root / "all-output"
    expected_files = {"providers.jsonl", "members.jsonl", "claims.jsonl"}
    _assert_output_files(output_directory, expected_files)
    for filename, count in (("providers.jsonl", 2), ("members.jsonl", 2), ("claims.jsonl", 4)):
        assert _line_count(output_directory / filename) == count
    claim_records = [
        json.loads(line) for line in (output_directory / "claims.jsonl").read_bytes().splitlines()
    ]
    _validate_records(claim_records, repository_root, "claim")
    for record in claim_records:
        assert "CLAIM_DETAIL" in record
        assert record["CH_CLIENT_CLAIM_UNIQUE_ID"]
        assert record["CLAIM_DETAIL"][0]["CD_CLAIM_LINE_NUMBER"] >= 1
        assert record["CH_TYPE_OF_BILL_CODE"]
        assert "CH_PLACE_OF_SERVICE_CODE" not in record
        assert not any("scenario" in key.lower() for key in record)
    first_contents = {path.name: path.read_bytes() for path in output_directory.iterdir()}
    second_result = _run_cli(config_path, repository_root)
    assert {path.name: path.read_bytes() for path in output_directory.iterdir()} == first_contents
    assert "claim: 4 records" in first_result.stdout
    assert second_result.stdout == first_result.stdout


def _assert_minimal_config_and_stale_cleanup(repository_root: Path, temporary_root: Path) -> None:
    """Prove short configs support one entity and clean stale known JSONLs.

    Args:
        repository_root: Project root used for child CLI processes.
        temporary_root: Temporary directory that owns source and output files.
    """
    detailed = _load_json(repository_root / "generator.config.json")
    for entity_name in ("provider", "member", "claim"):
        detailed["entities"][entity_name]["count"] = 1
        detailed["entities"][entity_name]["scenarios"] = {"new": 1}
        detailed["entities"][entity_name]["schema"] = str(
            repository_root / "schemas" / entity_name / f"{entity_name}.schema.json"
        )
    detailed["output_directory"] = "./reused-output"
    detailed_path = temporary_root / "detailed-all.config.json"
    detailed_path.write_text(json.dumps(detailed), encoding="utf-8")
    _run_cli(detailed_path, repository_root)

    output_directory = temporary_root / "reused-output"
    preserved = output_directory / "keep.txt"
    preserved.write_text("do not remove", encoding="utf-8")
    minimal = {
        "output_directory": "./reused-output",
        "provider": {"count": 2, "scenarios": {"new": 1}},
    }
    minimal_path = temporary_root / "minimal-provider.config.json"
    minimal_path.write_text(json.dumps(minimal), encoding="utf-8")
    result = _run_cli(minimal_path, repository_root)

    assert {path.name for path in output_directory.glob("*.jsonl")} == {"providers.jsonl"}
    assert _line_count(output_directory / "providers.jsonl") == 2
    assert preserved.read_text(encoding="utf-8") == "do not remove"
    assert "provider: 2 records" in result.stdout

    claim_only = {
        "output_directory": "./claim-only-output",
        "claim": {"count": 2, "scenarios": {"new": 1}},
    }
    claim_path = temporary_root / "minimal-claim.config.json"
    claim_path.write_text(json.dumps(claim_only), encoding="utf-8")
    claim_result = _run_cli(claim_path, repository_root)
    claim_output = temporary_root / "claim-only-output"
    _assert_output_files(claim_output, {"claims.jsonl"})
    assert _line_count(claim_output / "claims.jsonl") == 2
    assert "claim: 2 records" in claim_result.stdout


def _assert_output_files(output_directory: Path, expected: set[str]) -> None:
    """Assert an output run produced precisely the configured JSONL files."""
    assert {path.name for path in output_directory.iterdir()} == expected


def _line_count(path: Path) -> int:
    """Return the number of JSONL records in one generated file."""
    return len(path.read_bytes().splitlines())


def _run_cli(config_path: Path, repository_root: Path) -> subprocess.CompletedProcess[str]:
    """Execute the module CLI through a real child process.

    Args:
        config_path: Configuration to pass to the CLI.
        repository_root: Working directory for the child process.

    Returns:
        Completed successful CLI process.

    Raises:
        AssertionError: If generation exits unsuccessfully.
    """
    result = subprocess.run(
        [sys.executable, "-m", "healthcare_test_data", "generate", "--config", str(config_path)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def _validate_records(
    records: list[object], repository_root: Path, entity_name: str = "provider"
) -> None:
    """Validate every JSONL record against the flat provider schema.

    Args:
        records: Parsed generated JSONL records.
        repository_root: Project root containing the provider schema.
        entity_name: Entity schema name used to validate the records.

    Raises:
        AssertionError: If any record violates the schema.
    """
    schema = _load_json(repository_root / "schemas" / entity_name / f"{entity_name}.schema.json")
    validator = Draft202012Validator(schema)
    for record in records:
        errors = sorted(validator.iter_errors(record), key=str)
        assert not errors, errors


def _assert_invalid_schema_error(
    config_path: Path, repository_root: Path, records: list[object]
) -> None:
    """Prove entity/schema failures return safe, context-only process output.

    Args:
        config_path: Valid baseline configuration to mutate.
        repository_root: Project root used by the CLI child process.
        records: Generated records whose values must remain private.

    Raises:
        AssertionError: If the error contract is unsafe or incomplete.
    """
    invalid_schema_path = config_path.parent / "invalid-provider.schema.json"
    invalid_schema = _load_json(repository_root / "schemas/provider/provider.schema.json")
    invalid_schema["properties"]["CP_PROVIDER_NPI"] = {"type": "integer"}
    invalid_schema_path.write_text(json.dumps(invalid_schema), encoding="utf-8")

    invalid_config = _load_json(config_path)
    invalid_config["entities"]["provider"]["schema"] = str(invalid_schema_path)
    invalid_config_path = config_path.parent / "invalid-provider.config.json"
    invalid_config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

    result = _run_cli_failure(invalid_config_path, repository_root)
    expected_error = f"Generation failed for entity 'provider' using schema {invalid_schema_path}"
    assert expected_error in result.stderr
    assert "schema validation at $.CP_PROVIDER_NPI: expected type integer" in result.stderr
    assert "ValidationError" not in result.stderr
    for value in _scalar_texts(records):
        assert value not in result.stderr


def _assert_invalid_config_error(config_path: Path, repository_root: Path) -> None:
    """Prove config validation output preserves the correction but not its value.

    Args:
        config_path: Valid baseline configuration to mutate.
        repository_root: Project root used by the CLI child process.

    Raises:
        AssertionError: If invalid configuration leaks its private value.
    """
    secret_value = "PRIVATE_CONFIG_VALUE_MUST_NOT_LEAK"
    invalid_config = _load_json(config_path)
    invalid_config["entities"]["provider"]["count"] = secret_value
    invalid_config_path = config_path.parent / "invalid-count.config.json"
    invalid_config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

    result = _run_cli_failure(invalid_config_path, repository_root)
    assert f"Configuration failed for {invalid_config_path.resolve()}" in result.stderr
    assert "$.entities.provider.count: expected type integer" in result.stderr
    assert secret_value not in result.stderr
    assert "ValidationError" not in result.stderr


def _assert_missing_schema_error(config_path: Path, repository_root: Path) -> None:
    """Prove a missing schema path remains visible as an actionable reason.

    Args:
        config_path: Valid baseline configuration to mutate.
        repository_root: Project root used by the CLI child process.

    Raises:
        AssertionError: If the missing schema error lacks useful context.
    """
    missing_schema_path = config_path.parent / "missing-provider.schema.json"
    invalid_config = _load_json(config_path)
    invalid_config["entities"]["provider"]["schema"] = str(missing_schema_path)
    invalid_config_path = config_path.parent / "missing-schema.config.json"
    invalid_config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

    result = _run_cli_failure(invalid_config_path, repository_root)
    assert f"Configuration failed for {invalid_config_path.resolve()}" in result.stderr
    assert f"references missing schema file {missing_schema_path}" in result.stderr


def _assert_import_error(config_path: Path, repository_root: Path, records: list[object]) -> None:
    """Prove module import failures preserve the missing module without record data.

    Args:
        config_path: Valid baseline configuration to mutate.
        repository_root: Project root used by the CLI child process.
        records: Generated records whose values must remain private.

    Raises:
        AssertionError: If a module failure leaks generated data.
    """
    missing_module = "healthcare_test_data.entities.missing_provider_smoke"
    invalid_config = _load_json(config_path)
    invalid_config["entities"]["provider"]["module"] = missing_module
    invalid_config_path = config_path.parent / "missing-module.config.json"
    invalid_config_path.write_text(json.dumps(invalid_config), encoding="utf-8")

    result = _run_cli_failure(invalid_config_path, repository_root)
    assert f"Could not import entity module {missing_module!r}" in result.stderr
    assert f"missing module {missing_module!r}" in result.stderr
    for value in _scalar_texts(records):
        assert value not in result.stderr


def _run_cli_failure(config_path: Path, repository_root: Path) -> subprocess.CompletedProcess[str]:
    """Run one expected CLI failure and assert its process-level contract.

    Args:
        config_path: Invalid configuration to pass to the CLI.
        repository_root: Project root used by the CLI child process.

    Returns:
        Completed failed CLI process.

    Raises:
        AssertionError: If the CLI exits with the wrong process contract.
    """
    result = subprocess.run(
        [sys.executable, "-m", "healthcare_test_data", "generate", "--config", str(config_path)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    return result


def _assert_safe_stdout(stdout: str, records: list[object]) -> None:
    """Ensure the CLI summary exposes no generated provider values or rows.

    Args:
        stdout: CLI standard output to inspect.
        records: Generated records whose values must not appear in output.

    Raises:
        AssertionError: If the summary leaks record values or omits its core
            context.
    """
    assert stdout.count("provider:") == 1
    assert "3 records" in stdout
    assert "providers.jsonl" in stdout
    for value in _scalar_texts(records):
        assert value not in stdout


def _scalar_texts(value: object) -> list[str]:
    """Collect printable nonempty scalar values from nested generated JSON data.

    Args:
        value: JSON-compatible value to traverse.

    Returns:
        Nonempty printable scalar values found recursively.
    """
    if isinstance(value, str):
        return [value] if len(value) > 1 else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        return [text for item in value for text in _scalar_texts(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _scalar_texts(item)]
    return []


def _load_json(path: Path) -> dict[str, Any]:
    """Read one JSON object for the smoke fixture or provider schema.

    Args:
        path: JSON file to read.

    Returns:
        Parsed JSON object.

    Raises:
        AssertionError: If the JSON root is not an object.
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"Expected {path} to contain a JSON object")
    return value


if __name__ == "__main__":
    main()
