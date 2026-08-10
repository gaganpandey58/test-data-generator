"""Manually validate member and medical-claim schemas, links, and payment amounts."""

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from healthcare_test_data.config import SurvivorshipPolicy, load_config
from healthcare_test_data.engine import run_entity
from healthcare_test_data.entities.claim import generate_record as generate_claim
from healthcare_test_data.entities.member import generate_record as generate_member
from healthcare_test_data.entities.provider import generate_record as generate_provider
from healthcare_test_data.layouts import LayoutField, load_layout
from healthcare_test_data.scenarios import Scenario
from healthcare_test_data.survivorship import ExpectedAction, evaluate

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Validate deterministic member/claim generation and cross-entity linkage.

    Raises:
        AssertionError: If schemas, identifiers, dates, links, or payment
            amounts do not meet the generator contract.
    """
    member_validator = Draft202012Validator(_load_schema("member"))
    claim_validator = Draft202012Validator(_load_schema("claim"))
    members = [generate_member(20260805, index) for index in range(10)]
    providers = [generate_provider(20260805, index) for index in range(10)]
    claims = [generate_claim(20260805, index) for index in range(20)]
    assert members == [generate_member(20260805, index) for index in range(10)]
    assert claims == [generate_claim(20260805, index) for index in range(20)]
    _validate_records(member_validator, members)
    _validate_records(claim_validator, claims)
    _assert_claim_links(members, providers, claims)
    _assert_configurable_links()
    _assert_cross_file_scenario_links()
    _assert_member_source_shape_and_variations(members[0])
    _assert_claim_source_shape_and_variations(claims[0])
    print(
        "members=10 claims=20 schema_validation=passed links=passed "
        "payments=reconciled source_shape=passed scenarios=passed"
    )


def _load_schema(entity: str) -> dict[str, Any]:
    """Load one flat entity schema from the repository.

    Args:
        entity: Entity directory and schema filename stem.

    Returns:
        Parsed entity JSON Schema object.

    Raises:
        AssertionError: If the schema root is not an object.
    """
    value: Any = json.loads(
        (ROOT / "schemas" / entity / f"{entity}.schema.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def _validate_records(
    validator: Draft202012Validator, records: Iterable[Mapping[str, object]]
) -> None:
    """Assert that every generated record satisfies its entity schema.

    Args:
        validator: Compiled JSON Schema validator for an entity.
        records: Entity records to validate.

    Raises:
        AssertionError: If any generated record is invalid.
    """
    for record in records:
        errors = sorted(validator.iter_errors(record), key=str)
        assert not errors, [error.message for error in errors]


def _assert_claim_links(
    members: list[dict[str, object]],
    providers: list[dict[str, object]],
    claims: list[dict[str, object]],
) -> None:
    """Assert claims preserve member/provider identity and payment relationships.

    Args:
        members: Generated members indexed by deterministic source position.
        providers: Generated providers indexed by deterministic source position.
        claims: Generated medical claims to inspect.

    Raises:
        AssertionError: If a claim link or header/line payment reconciliation
            fails.
    """
    for index, claim in enumerate(claims):
        member = members[index % len(members)]
        provider = providers[index % len(providers)]
        enrollments = member["CM_MEMBER_ENROLLMENTS"]
        assert isinstance(enrollments, list) and enrollments
        enrollment = enrollments[0]
        assert isinstance(enrollment, Mapping)
        assert enrollment["CM_PCP_PROVIDER_CLIENT_ID"] == provider["CP_PROVIDER_CLIENT_ID"]
        assert claim["CH_PATIENT_CLIENT_ID"] == member["CM_MEMBER_CLIENT_ID"]
        assert claim["CH_PATIENT_CLIENT_MASTER_ID"] == member["CM_MEMBER_CLIENT_MASTER_ID"]
        assert claim["CH_SUBSCRIBER_CLIENT_ID"] == member["CM_SUBSCRIBER_CLIENT_ID"]
        assert claim["CH_SUBSCRIBER_CLIENT_MASTER_ID"] == member["CM_SUBSCRIBER_CLIENT_MASTER_ID"]
        assert claim["CH_BILLING_PROVIDER_CLIENT_ID"] == provider["CP_PROVIDER_CLIENT_ID"]
        assert claim["CH_BILLING_PROVIDER_NPI"] == provider["CP_PROVIDER_NPI"]
        assert claim["CH_BILLING_PROVIDER_FEDERAL_TAX_ID"] == provider["CP_PROVIDER_FEDERAL_TAX_ID"]
        if claim["CH_CLAIM_FREQUENCY_CODE"] == "7":
            original_id = claim["CH_CLIENT_ORIGINAL_CLAIM_ID"]
            assert isinstance(original_id, str)
            original = next(
                record for record in claims if record["CH_CLIENT_CLAIM_ID"] == original_id
            )
            assert claim["CH_CLIENT_ROOT_CLAIM_ID"] == original["CH_CLIENT_ROOT_CLAIM_ID"]
        _assert_payment_reconciliation(claim)


def _assert_configurable_links() -> None:
    """Assert relational IDs remain valid when entity counts are reduced.

    Raises:
        AssertionError: If claims reference records outside configured output.
    """
    entity_counts = {"provider": 1, "member": 1, "claim": 20}
    member = generate_member(20260805, 0, entity_counts)
    provider = generate_provider(20260805, 0)
    enrollment = member["CM_MEMBER_ENROLLMENTS"]
    assert isinstance(enrollment, list) and isinstance(enrollment[0], Mapping)
    assert enrollment[0]["CM_PCP_PROVIDER_CLIENT_ID"] == provider["CP_PROVIDER_CLIENT_ID"]
    for index in range(entity_counts["claim"]):
        claim = generate_claim(20260805, index, entity_counts)
        assert claim["CH_PATIENT_CLIENT_ID"] == member["CM_MEMBER_CLIENT_ID"]
        assert claim["CH_BILLING_PROVIDER_CLIENT_ID"] == provider["CP_PROVIDER_CLIENT_ID"]

    claim_only_counts = {"claim": 3}
    for index in range(claim_only_counts["claim"]):
        fallback_claim = generate_claim(20260805, index, claim_only_counts)
        assert fallback_claim["CH_PATIENT_CLIENT_ID"]
        assert fallback_claim["CH_BILLING_PROVIDER_CLIENT_ID"]


def _assert_cross_file_scenario_links() -> None:
    """Assert claims resolve IDs from actual scenario-varied JSONL output.

    This guards against using a raw source index after a related output position
    is replaced by a duplicate, changed, stale, or incomplete variation.

    Raises:
        AssertionError: If a claim member or provider link is absent from its
            corresponding emitted JSONL file.
    """
    with TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        raw_config = json.loads((ROOT / "generator.config.json").read_text(encoding="utf-8"))
        raw_config["output_directory"] = "./output"
        for entity_name, count, scenarios in (
            ("provider", 10, {"changed": 5}),
            ("member", 10, {}),
            ("claim", 10, {}),
        ):
            entity = raw_config["entities"][entity_name]
            entity["enabled"] = True
            entity["count"] = count
            entity["scenarios"] = scenarios
            entity["schema"] = str(ROOT / "schemas" / entity_name / f"{entity_name}.schema.json")
        config_path = temporary_root / "scenario-links.config.json"
        config_path.write_text(json.dumps(raw_config), encoding="utf-8")
        run_config = load_config(config_path)
        entity_counts = {entity.name: entity.count for entity in run_config.entities}
        entity_scenarios = {entity.name: entity.scenarios for entity in run_config.entities}
        for entity in run_config.entities:
            run_entity(
                entity,
                run_config.seed,
                run_config.output_directory,
                entity_counts,
                entity_scenarios,
            )

        output_directory = temporary_root / "output"
        members = _load_jsonl(output_directory / "members.jsonl")
        providers = _load_jsonl(output_directory / "providers.jsonl")
        claims = _load_jsonl(output_directory / "claims.jsonl")
        member_ids = {record["CM_MEMBER_CLIENT_ID"] for record in members}
        provider_ids = {record["CP_PROVIDER_CLIENT_ID"] for record in providers}
        pcp_provider_ids = {
            enrollment["CM_PCP_PROVIDER_CLIENT_ID"]
            for member in members
            for enrollment in member["CM_MEMBER_ENROLLMENTS"]
            if isinstance(enrollment, Mapping)
        }
        assert all(isinstance(provider_id, str) for provider_id in pcp_provider_ids)
        assert pcp_provider_ids <= provider_ids
        assert all(claim["CH_PATIENT_CLIENT_ID"] in member_ids for claim in claims)
        assert all(claim["CH_BILLING_PROVIDER_CLIENT_ID"] in provider_ids for claim in claims)


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    """Read one smoke-generated JSONL file into decoded records.

    Args:
        path: JSONL output file to inspect.

    Returns:
        Decoded object records in output order.
    """
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def _assert_member_source_shape_and_variations(baseline: Mapping[str, object]) -> None:
    """Assert GDF limits, nested arrays, and member scenario identity behavior."""
    assert len(baseline["CM_MEMBER_CLIENT_ID"]) <= 35
    validator = Draft202012Validator(_load_schema("member"))
    overlong_member_id = dict(baseline)
    overlong_member_id["CM_MEMBER_CLIENT_ID"] = "M" * 36
    assert any(
        list(error.absolute_path) == ["CM_MEMBER_CLIENT_ID"]
        for error in validator.iter_errors(overlong_member_id)
    ), "member schema must reject a 36-character client ID"
    _assert_layout_values(baseline, load_layout("member").root)
    addresses = baseline["CM_MEMBER_ADDRESSES"]
    assert isinstance(addresses, list) and isinstance(addresses[0], Mapping)
    _assert_layout_values(addresses[0], load_layout("member").groups["CM_MEMBER_ADDRESSES"])
    scenario_record = generate_member(20260805, 9, scenario=Scenario("changed", 0))
    duplicate = generate_member(20260805, 10, scenario=Scenario("duplicate", 0))
    stale = generate_member(20260805, 11, scenario=Scenario("stale", 0))
    incomplete = generate_member(20260805, 12, scenario=Scenario("incomplete", 0))
    assert scenario_record["CM_MEMBER_SOURCE_UPDATED_AT"] > baseline["CM_MEMBER_SOURCE_UPDATED_AT"]
    assert duplicate == baseline
    assert stale["CM_MEMBER_SOURCE_UPDATED_AT"] < baseline["CM_MEMBER_SOURCE_UPDATED_AT"]
    assert "CM_MEMBER_SSN" not in incomplete
    for key in (
        "CM_PAYER_SHORT",
        "CM_MEMBER_CLIENT_ID",
        "CM_MEMBER_BIRTH_DATE",
        "CM_MEMBER_GENDER",
    ):
        assert scenario_record[key] == baseline[key]
    assert scenario_record["CM_SUBSCRIBER_CLIENT_ID"] == baseline["CM_SUBSCRIBER_CLIENT_ID"]
    baseline_address = baseline["CM_MEMBER_ADDRESSES"][0]
    changed_address = scenario_record["CM_MEMBER_ADDRESSES"][0]
    assert isinstance(baseline_address, Mapping) and isinstance(changed_address, Mapping)
    assert (
        changed_address["CM_MEMBER_STATE"],
        changed_address["CM_MEMBER_CITY"],
        changed_address["CM_MEMBER_ZIP"],
        changed_address["CM_MEMBER_COUNTY"],
    ) != (
        baseline_address["CM_MEMBER_STATE"],
        baseline_address["CM_MEMBER_CITY"],
        baseline_address["CM_MEMBER_ZIP"],
        baseline_address["CM_MEMBER_COUNTY"],
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


def _assert_payment_reconciliation(claim: Mapping[str, object]) -> None:
    """Assert header and line paid amounts reconcile to allowed amounts.

    Args:
        claim: Generated claim containing one or more service lines.

    Raises:
        AssertionError: If member liability plus paid amount differs from the
            allowed amount at either level.
    """
    _assert_amounts(
        claim["CH_ALLOWED_AMOUNT"],
        claim["CH_PATIENT_LIABILITY_AMOUNT"],
        claim["CH_PAID_AMOUNT"],
    )
    lines = claim["CLAIM_DETAIL"]
    assert isinstance(lines, list) and lines
    for line in lines:
        assert isinstance(line, Mapping)
        _assert_amounts(
            line["CD_ALLOWED_AMOUNT"],
            line["CD_PATIENT_LIABILITY_AMOUNT"],
            line["CD_PAID_AMOUNT"],
        )


def _assert_amounts(allowed: object, liability: object, paid: object) -> None:
    """Assert a payment triad reconciles within a cent.

    Args:
        allowed: Allowed amount.
        liability: Member liability amount.
        paid: Payer paid amount.

    Raises:
        AssertionError: If values are not numeric or do not reconcile.
    """
    assert isinstance(allowed, float)
    assert isinstance(liability, float)
    assert isinstance(paid, float)
    assert abs(allowed - liability - paid) < 0.01


def _assert_claim_source_shape_and_variations(claim: Mapping[str, object]) -> None:
    """Assert EIP/GDF claim headers use canonical detail and identity fields."""
    assert "CLAIM_DETAIL" in claim
    assert claim["CH_CLIENT_CLAIM_UNIQUE_ID"]
    lines = claim["CLAIM_DETAIL"]
    assert isinstance(lines, list) and lines
    assert isinstance(lines[0], Mapping)
    assert lines[0]["CD_CLAIM_LINE_NUMBER"] >= 1
    professional = generate_claim(20260805, 0, profile="claim-professional")
    institutional = generate_claim(20260805, 1, profile="claim-institutional")
    assert professional["CH_CLAIM_TYPE"] == "P"
    assert professional["CH_PLACE_OF_SERVICE_CODE"]
    assert "CH_TYPE_OF_BILL_CODE" not in professional
    assert "CH_ADMISSION_DATE" not in professional
    professional_lines = professional["CLAIM_DETAIL"]
    assert isinstance(professional_lines, list) and isinstance(professional_lines[0], Mapping)
    assert "CD_SUBMITTED_REVENUE_CODE" not in professional_lines[0]
    assert institutional["CH_CLAIM_TYPE"] == "I"
    assert institutional["CH_TYPE_OF_BILL_CODE"]
    assert "CH_PLACE_OF_SERVICE_CODE" not in institutional
    assert "CH_RENDERING_PROVIDER_NPI" not in institutional
    institutional_lines = institutional["CLAIM_DETAIL"]
    assert isinstance(institutional_lines, list) and isinstance(institutional_lines[0], Mapping)
    assert "CD_PLACE_OF_SERVICE_CODE" not in institutional_lines[0]
    assert "CD_SUBMITTED_PROCEDURE_MODIFIER_01" not in institutional_lines[0]

    replacement = generate_claim(20260805, 10, scenario=Scenario("replacement", 0))
    assert replacement["CH_CLIENT_ROOT_CLAIM_ID"] == claim["CH_CLIENT_ROOT_CLAIM_ID"]
    assert replacement["CH_CLIENT_ORIGINAL_CLAIM_ID"] == claim["CH_CLIENT_CLAIM_ID"]
    assert replacement["CH_CLIENT_CLAIM_VERSION_NUMBER"] == "2"
    assert replacement["CH_NUMBER_OF_ADJUSTMENTS"] == 1
    assert replacement["CH_CLAIM_FREQUENCY_CODE"] == "7"
    replacement_lines = replacement["CLAIM_DETAIL"]
    assert isinstance(replacement_lines, list) and isinstance(replacement_lines[0], Mapping)
    assert replacement_lines[0]["CD_LINE_ADJUSTMENTS"]

    void = generate_claim(20260805, 11, scenario=Scenario("void", 0))
    assert void["CH_CLAIM_FREQUENCY_CODE"] == "8"
    void_decision = evaluate(
        claim,
        void,
        Scenario("void", 0),
        SurvivorshipPolicy("update", "update", "ignore"),
    )
    assert void_decision.action is ExpectedAction.IGNORE

    orphan = generate_claim(20260805, 12, scenario=Scenario("orphan_payment", 0))
    assert orphan["CH_RECORD_TAG"] == "835 Provisional"
    assert orphan["CH_RECORD_STATUS"] == "Orphan Payment"
    assert orphan["CH_PAYMENT_CLAIM_ID"] not in {
        claim["CH_CLIENT_CLAIM_ID"],
        replacement["CH_CLIENT_CLAIM_ID"],
    }
    assert orphan["CH_PATIENT_ACCOUNT_CONTROL_NUMBER"] != claim["CH_PATIENT_ACCOUNT_CONTROL_NUMBER"]
    assert orphan["CH_CHECK_DATE"] >= orphan["CH_CLAIM_PAID_DATE"]
    orphan_lines = orphan["CLAIM_DETAIL"]
    baseline_lines = claim["CLAIM_DETAIL"]
    assert isinstance(orphan_lines, list) and isinstance(orphan_lines[0], Mapping)
    assert isinstance(baseline_lines, list) and isinstance(baseline_lines[0], Mapping)
    assert orphan_lines[0].get("CD_SUBMITTED_REVENUE_CODE") != baseline_lines[0].get(
        "CD_SUBMITTED_REVENUE_CODE"
    )
    orphan_decision = evaluate(
        claim,
        orphan,
        Scenario("orphan_payment", 0),
        SurvivorshipPolicy("update", "update", "ignore"),
    )
    assert orphan_decision.action is ExpectedAction.CREATE

    professional_orphan = generate_claim(
        20260805,
        13,
        scenario=Scenario("orphan_payment", 0),
        profile="claim-professional",
    )
    assert (
        professional_orphan["CH_PLACE_OF_SERVICE_CODE"] != professional["CH_PLACE_OF_SERVICE_CODE"]
    )
    professional_orphan_decision = evaluate(
        professional,
        professional_orphan,
        Scenario("orphan_payment", 0),
        SurvivorshipPolicy("update", "update", "ignore"),
    )
    assert professional_orphan_decision.action is ExpectedAction.CREATE


if __name__ == "__main__":
    main()
