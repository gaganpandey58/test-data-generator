"""Manually validate generated providers and, optionally, an external JSONL sample."""

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from healthcare_test_data.entities.provider import generate_record
from healthcare_test_data.layouts import LayoutField, load_layout
from healthcare_test_data.scenarios import Scenario

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "provider" / "provider.schema.json"


def main() -> None:
    """Validate generated provider records and an optional external JSONL file.

    Raises:
        AssertionError: If deterministic generation, schema validation, or a
            provider invariant fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, help="Optional external provider JSONL file")
    arguments = parser.parse_args()

    validator = Draft202012Validator(_load_schema())
    first_run = [generate_record(20260805, index) for index in (0, 1)]
    second_run = [generate_record(20260805, index) for index in (0, 1)]
    assert first_run == second_run
    _validate_records(validator, first_run)
    fractional_epoch = dict(first_run[0])
    fractional_epoch["INGESTION_EPOCH"] = 1_785_945_600.5
    fractional_errors = list(validator.iter_errors(fractional_epoch))
    assert any(list(error.absolute_path) == ["INGESTION_EPOCH"] for error in fractional_errors), (
        "provider schema must reject a fractional INGESTION_EPOCH"
    )
    invalid_npi = dict(first_run[0])
    invalid_npi["CP_PROVIDER_NPI"] = "12345678901"
    assert any(
        list(error.absolute_path) == ["CP_PROVIDER_NPI"]
        for error in validator.iter_errors(invalid_npi)
    ), "provider schema must reject an invalid NPI shape"
    for record in first_run:
        _assert_generated_invariants(record)
    _assert_source_shape_and_variations(first_run[0])

    external_count = 0
    if arguments.sample is not None:
        external_records = list(_load_jsonl(arguments.sample))
        assert len(external_records) == 3
        _validate_records(validator, external_records)
        external_count = len(external_records)
    print(
        f"external_records={external_count} generated_records=2 deterministic=same "
        "schema_validation=passed fractional_epoch=rejected source_shape=passed scenarios=passed"
    )


def _load_schema() -> dict[str, Any]:
    """Load the checked-in provider schema.

    Returns:
        The schema JSON object used for all provider validations.

    Raises:
        AssertionError: If the checked-in schema is not a JSON object.
    """
    value: Any = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _load_jsonl(path: Path) -> Iterable[dict[str, object]]:
    """Yield JSON records from an external JSONL source.

    Args:
        path: External JSONL file to inspect without copying it into the repo.

    Yields:
        Each nonempty JSON object in file order.

    Raises:
        AssertionError: If a nonempty line is not a JSON object.
    """
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            value: Any = json.loads(line)
            assert isinstance(value, dict), f"line {line_number} must be an object"
            yield value


def _validate_records(
    validator: Draft202012Validator, records: Iterable[Mapping[str, object]]
) -> None:
    """Assert that every supplied record satisfies the provider schema.

    Args:
        validator: Compiled JSON Schema validator for providers.
        records: Records to validate.

    Raises:
        AssertionError: If any record violates the schema.
    """
    for record in records:
        errors = sorted(validator.iter_errors(record), key=str)
        assert not errors, [error.message for error in errors]


def _assert_generated_invariants(record: Mapping[str, object]) -> None:
    """Assert generated provider values meet key domain invariants.

    Args:
        record: One generated provider record.

    Raises:
        AssertionError: If the NPI, dates, name, address, or network data is
            unusable or inconsistent.
    """
    npi = record["CP_PROVIDER_NPI"]
    assert isinstance(npi, str) and len(npi) == 10 and npi.isdigit()
    assert _is_luhn_valid(f"80840{npi}")
    start_date = record["CP_PROVIDER_RECORD_START_DATE"]
    ingestion_date = record["INGESTION_DATE"]
    assert isinstance(start_date, str) and len(start_date) == 8 and start_date.isdigit()
    assert isinstance(ingestion_date, str) and len(ingestion_date) == 8 and ingestion_date.isdigit()
    assert record["CP_PROVIDER_FULL_NAME"]
    addresses = record["CP_PROVIDER_ADDRESSES"]
    networks = record["CP_PROVIDER_NETWORKS"]
    assert isinstance(addresses, list) and len(addresses) >= 1
    assert isinstance(networks, list) and len(networks) >= 1
    address = addresses[0]
    network = networks[0]
    assert isinstance(address, Mapping) and address["CP_PROVIDER_STATE"]
    assert isinstance(network, Mapping)
    assert network["CP_PROVIDER_NETWORK_EFFECTIVE_DATE"] == start_date
    assert network["CP_PROVIDER_NETWORK_TERMINATION_DATE"] in ("", start_date)


def _assert_source_shape_and_variations(baseline: Mapping[str, object]) -> None:
    """Assert GDF limits, nested source groups, and provider variations."""
    _assert_layout_values(baseline, load_layout("provider").root)
    addresses = baseline["CP_PROVIDER_ADDRESSES"]
    networks = baseline["CP_PROVIDER_NETWORKS"]
    assert isinstance(addresses, list) and isinstance(addresses[0], Mapping)
    assert isinstance(networks, list) and isinstance(networks[0], Mapping)
    _assert_layout_values(addresses[0], load_layout("provider").groups["CP_PROVIDER_ADDRESSES"])
    _assert_layout_values(networks[0], load_layout("provider").groups["CP_PROVIDER_NETWORKS"])
    scenario_record = generate_record(20260805, 9, scenario=Scenario("changed", 0))
    duplicate = generate_record(20260805, 10, scenario=Scenario("duplicate", 0))
    stale = generate_record(20260805, 11, scenario=Scenario("stale", 0))
    incomplete = generate_record(20260805, 12, scenario=Scenario("incomplete", 0))
    assert (
        scenario_record["CP_PROVIDER_SOURCE_UPDATED_AT"] > baseline["CP_PROVIDER_SOURCE_UPDATED_AT"]
    )
    assert duplicate == baseline
    assert stale["CP_PROVIDER_SOURCE_UPDATED_AT"] < baseline["CP_PROVIDER_SOURCE_UPDATED_AT"]
    assert "CP_PROVIDER_DEA_NUMBER" not in incomplete
    assert scenario_record["CP_PROVIDER_CLIENT_ID"] == baseline["CP_PROVIDER_CLIENT_ID"]
    assert scenario_record["CP_PROVIDER_NPI"] == baseline["CP_PROVIDER_NPI"]
    assert scenario_record["CP_PROVIDER_FEDERAL_TAX_ID"] == baseline["CP_PROVIDER_FEDERAL_TAX_ID"]
    baseline_address = baseline["CP_PROVIDER_ADDRESSES"][0]
    changed_address = scenario_record["CP_PROVIDER_ADDRESSES"][0]
    assert isinstance(baseline_address, Mapping) and isinstance(changed_address, Mapping)
    assert (
        changed_address["CP_PROVIDER_STATE"],
        changed_address["CP_PROVIDER_CITY"],
        changed_address["CP_PROVIDER_ZIP"],
        changed_address["CP_PROVIDER_COUNTY"],
        changed_address["CP_PROVIDER_REGION"],
    ) != (
        baseline_address["CP_PROVIDER_STATE"],
        baseline_address["CP_PROVIDER_CITY"],
        baseline_address["CP_PROVIDER_ZIP"],
        baseline_address["CP_PROVIDER_COUNTY"],
        baseline_address["CP_PROVIDER_REGION"],
    )


def _assert_layout_values(record: Mapping[str, object], fields: tuple[LayoutField, ...]) -> None:
    """Require GDF fields to be source-compatible when populated."""
    for field in fields:
        assert field.name in record
        value = record[field.name]
        assert isinstance(value, str)
        assert len(value) <= field.max_length
        if field.type in {"numeric", "date"} and value:
            assert value.isdigit()


def _is_luhn_valid(number: str) -> bool:
    """Check a complete numeric string with the standard Luhn checksum.

    Args:
        number: Numeric identifier including its check digit.

    Returns:
        ``True`` when the identifier has a valid Luhn checksum.
    """
    total = 0
    for position, character in enumerate(reversed(number)):
        digit = int(character)
        if position % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


if __name__ == "__main__":
    main()
