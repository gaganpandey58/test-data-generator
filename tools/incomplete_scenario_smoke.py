"""Validate source-valid incomplete variations across every entity profile."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from healthcare_test_data.entities.claim import generate_record as generate_claim
from healthcare_test_data.entities.member import generate_record as generate_member
from healthcare_test_data.entities.provider import generate_record as generate_provider
from healthcare_test_data.scenarios import Scenario

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Validate every incomplete variation remains schema-valid and distinct.

    Raises:
        AssertionError: If an incomplete record retains its intended matching
            field, is identical to the baseline, or fails its entity schema.
    """
    member = generate_member(73, 0)
    incomplete_member = generate_member(73, 1, scenario=Scenario("incomplete", 0))
    assert "CM_PAYER_SHORT" not in incomplete_member
    assert incomplete_member != member
    _assert_valid("member", incomplete_member)

    provider = generate_provider(73, 0)
    incomplete_provider = generate_provider(73, 1, scenario=Scenario("incomplete", 0))
    provider_address = _first_group(incomplete_provider, "CP_PROVIDER_ADDRESSES")
    assert "CP_PROVIDER_ADDRESS_01" not in provider_address
    assert incomplete_provider != provider
    _assert_valid("provider", incomplete_provider)

    for profile in ("claim-professional", "claim-institutional"):
        claim = generate_claim(73, 0, profile=profile)
        incomplete_claim = generate_claim(
            73, 1, scenario=Scenario("incomplete", 0), profile=profile
        )
        assert "CH_PATIENT_ACCOUNT_CONTROL_NUMBER" not in incomplete_claim
        assert incomplete_claim != claim
        _assert_valid("claim", incomplete_claim)

    print("incomplete_scenarios=passed schema_validation=passed profiles=P,I")


def _assert_valid(entity: str, record: Mapping[str, object]) -> None:
    """Assert a generated entity variation passes its checked-in JSON Schema.

    Args:
        entity: Entity directory and schema filename stem.
        record: Generated source-shaped record to validate.

    Raises:
        AssertionError: If the schema file is malformed or the record is
            invalid for the entity contract.
    """
    schema: Any = json.loads(
        (ROOT / "schemas" / entity / f"{entity}.schema.json").read_text(encoding="utf-8")
    )
    assert isinstance(schema, dict)
    errors = sorted(Draft202012Validator(schema).iter_errors(record), key=str)
    assert not errors, [error.message for error in errors]


def _first_group(record: Mapping[str, object], group: str) -> Mapping[str, object]:
    """Return the first dictionary item from a required source group.

    Args:
        record: Generated source-shaped parent record.
        group: Nested array field that must contain a source row.

    Returns:
        First nested source-row mapping.

    Raises:
        AssertionError: If the group is absent, empty, or not a dictionary.
    """
    values = record[group]
    assert isinstance(values, list) and values
    value = values[0]
    assert isinstance(value, Mapping)
    return value


if __name__ == "__main__":
    main()
