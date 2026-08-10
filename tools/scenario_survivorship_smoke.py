"""Exercise scenario planning and internal survivorship decisions."""

import json
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from healthcare_test_data.config import EntityConfig, SurvivorshipPolicy
from healthcare_test_data.engine import run_entity
from healthcare_test_data.entities.member import generate_record as generate_member
from healthcare_test_data.entities.provider import generate_record as generate_provider
from healthcare_test_data.scenarios import Scenario, plan
from healthcare_test_data.survivorship import ExpectedAction, evaluate


def main() -> None:
    """Verify scenario quantities, published shape, and decision outcomes."""
    repository_root = Path(__file__).resolve().parents[1]
    scenarios = {
        "new": 1,
        "changed": 1,
        "duplicate": 1,
        "stale": 1,
        "incomplete": 1,
    }
    scenario_plan = plan(count=10, scenarios=scenarios, seed=73)
    assert scenario_plan.baseline_indexes == (0, 1, 2, 3, 4)
    variations = [scenario_plan.variation_for(index) for index in range(10)]
    assert variations[:5] == [None] * 5
    assert [variation.name for variation in variations[5:] if variation is not None] == list(
        scenarios
    )
    assert all(
        variation is None or variation.name == "new" or variation.baseline_index in range(5)
        for variation in variations
    )
    cyclic_plan = plan(count=8, scenarios={"changed": 6}, seed=0)
    cyclic_variations = [cyclic_plan.variation_for(index) for index in range(2, 8)]
    assert all(variation is not None for variation in cyclic_variations)
    assert [
        variation.baseline_index for variation in cyclic_variations if variation is not None
    ] == [
        0,
        1,
        0,
        1,
        0,
        1,
    ]

    with TemporaryDirectory() as temporary_directory:
        output = run_entity(
            EntityConfig(
                name="member",
                count=10,
                scenarios=scenarios,
                profile="member",
                schema=repository_root / "schemas/member/member.schema.json",
                module="healthcare_test_data.entities.member",
                filename="member.jsonl",
            ),
            seed=73,
            output_directory=Path(temporary_directory),
        )
        records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 10
    assert all("scenario" not in record for record in records)

    default_policy = SurvivorshipPolicy(
        member_verified_action="update",
        claim_verified_action="update",
        void_action="ignore",
    )
    _assert_matching_variants(default_policy)
    existing = {
        "CM_MEMBER_CLIENT_ID": "M1",
        "CM_PAYER_SHORT": "PAYER",
        "CM_MEMBER_SOURCE_UPDATED_AT": "20260805",
        "CM_MEMBER_SOURCE_RECORD_TAG": "834 Provisional",
    }
    assert (
        _action({}, {"CM_MEMBER_CLIENT_ID": "M2"}, "new", default_policy) is ExpectedAction.CREATE
    )
    assert (
        _action(
            existing,
            {**existing, "CM_MEMBER_SOURCE_UPDATED_AT": "20260806"},
            "changed",
            default_policy,
        )
        is ExpectedAction.UPDATE
    )
    assert _action(existing, dict(existing), "duplicate", default_policy) is ExpectedAction.IGNORE
    assert (
        _action(
            existing,
            {**existing, "CM_MEMBER_SOURCE_UPDATED_AT": "20260804"},
            "stale",
            default_policy,
        )
        is ExpectedAction.IGNORE
    )
    assert (
        _action(existing, {"CM_MEMBER_CLIENT_ID": "M2"}, "incomplete", default_policy)
        is ExpectedAction.CREATE
    )

    claim = _professional_claim()
    assert (
        _action(claim, {**claim, "CH_SOURCE_UPDATED_AT": "20260806"}, "replacement", default_policy)
        is ExpectedAction.UPDATE
    )
    assert (
        _action(claim, {**claim, "CH_SOURCE_UPDATED_AT": "20260806"}, "void", default_policy)
        is ExpectedAction.IGNORE
    )
    assert (
        _action(
            {},
            {"CH_CLIENT_CLAIM_ID": "C2", "CH_RECORD_TAG": "835 Provisional"},
            "orphan_payment",
            default_policy,
        )
        is ExpectedAction.CREATE
    )
    institutional_claim = _institutional_claim()
    assert (
        _action(
            institutional_claim,
            {**institutional_claim, "CH_SOURCE_UPDATED_AT": "20260806"},
            "changed",
            default_policy,
        )
        is ExpectedAction.UPDATE
    )
    legacy_line_claim = {
        **institutional_claim,
        "CLAIM_DETAIL": [{}],
        "CD_CLAIM_LINES": [{"CD_SUBMITTED_REVENUE_CODE": "0510"}],
    }
    assert (
        _action(
            legacy_line_claim,
            {**legacy_line_claim, "CH_SOURCE_UPDATED_AT": "20260806"},
            "changed",
            default_policy,
        )
        is ExpectedAction.UPDATE
    )
    claim_id_only = {"CH_CLIENT_CLAIM_ID": "C1", "CH_SOURCE_UPDATED_AT": "20260805"}
    assert (
        _action(
            claim_id_only,
            {**claim_id_only, "CH_SOURCE_UPDATED_AT": "20260806"},
            "changed",
            default_policy,
        )
        is ExpectedAction.CREATE
    )
    orphan = {**claim, "CH_RECORD_TAG": "835 Provisional", "CH_RECORD_STATUS": "Orphan Payment"}
    assert (
        _action(orphan, {**claim, "CH_SOURCE_UPDATED_AT": "20260806"}, "changed", default_policy)
        is ExpectedAction.LINK_PAYMENT
    )

    keep_both = SurvivorshipPolicy(
        member_verified_action="keep_both",
        claim_verified_action="keep_both",
        void_action="keep_both",
    )
    verified = {**existing, "CM_MEMBER_SOURCE_RECORD_TAG": "MR Verified"}
    assert (
        _action(
            verified, {**existing, "CM_MEMBER_SOURCE_UPDATED_AT": "20260806"}, "changed", keep_both
        )
        is ExpectedAction.KEEP_BOTH
    )
    assert (
        _action(claim, {**claim, "CH_SOURCE_UPDATED_AT": "20260806"}, "void", keep_both)
        is ExpectedAction.KEEP_BOTH
    )


def _action(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
    name: str,
    policy: SurvivorshipPolicy,
) -> ExpectedAction:
    """Evaluate one named scenario and return only its expected action."""
    return evaluate(existing, incoming, Scenario(name=name, baseline_index=0), policy).action


def _assert_matching_variants(policy: SurvivorshipPolicy) -> None:
    """Prove each configured source matching tier selects an update decision."""
    member = generate_member(73, 0)
    provider = generate_provider(73, 0)
    _assert_update_tier(member, member, 1, policy)
    _assert_update_tier(
        _without(member, "CM_PAYER_SHORT"), _without(member, "CM_PAYER_SHORT"), 2, policy
    )
    name_member = _without(member, "CM_PAYER_SHORT", "CM_MEMBER_CLIENT_ID")
    _assert_update_tier(name_member, name_member, 3, policy)
    ssn_member = _without(
        member,
        "CM_PAYER_SHORT",
        "CM_MEMBER_CLIENT_ID",
        "CM_MEMBER_FIRST_NAME",
        "CM_MEMBER_LAST_NAME",
    )
    _assert_update_tier(ssn_member, ssn_member, 4, policy)
    newborn_member = _without(
        member,
        "CM_PAYER_SHORT",
        "CM_MEMBER_CLIENT_ID",
        "CM_MEMBER_FIRST_NAME",
        "CM_MEMBER_LAST_NAME",
        "CM_MEMBER_SSN",
    )
    _assert_update_tier(newborn_member, newborn_member, 5, policy)
    _assert_update_tier(provider, provider, 6, policy)
    npi_provider = _without(provider, "CP_PROVIDER_CLIENT_ID")
    _assert_update_tier(npi_provider, npi_provider, 7, policy)
    organization_provider = {
        **_without(
            provider,
            "CP_PROVIDER_CLIENT_ID",
            "CP_PROVIDER_NPI",
            "CP_PROVIDER_FULL_NAME",
        ),
        "CP_PROVIDER_RECORD_TYPE": "O",
    }
    _assert_update_tier(organization_provider, organization_provider, 8, policy)
    taxonomy_provider = _without(
        provider,
        "CP_PROVIDER_CLIENT_ID",
        "CP_PROVIDER_NPI",
        "CP_PROVIDER_RECORD_TYPE",
        "CP_PROVIDER_FEDERAL_TAX_ID",
    )
    _assert_update_tier(taxonomy_provider, taxonomy_provider, 9, policy)


def _assert_update_tier(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
    tier: int,
    policy: SurvivorshipPolicy,
) -> None:
    """Assert a source-shaped matching variant resolves through one tier."""
    changed = {**incoming, "CM_MEMBER_SOURCE_UPDATED_AT": "20260806"}
    if any(key.startswith("CP_PROVIDER") for key in incoming):
        changed.pop("CM_MEMBER_SOURCE_UPDATED_AT")
        changed["CP_PROVIDER_SOURCE_UPDATED_AT"] = "20260806"
    decision = evaluate(existing, changed, Scenario(name="changed", baseline_index=0), policy)
    assert decision.action is ExpectedAction.UPDATE
    assert decision.match_tier == tier


def _without(record: Mapping[str, object], *keys: str) -> dict[str, object]:
    """Return a source-record view with earlier matching keys omitted."""
    result: dict[str, object] = {}
    omitted = set(keys)
    for key, value in record.items():
        if key in omitted:
            continue
        if isinstance(value, list):
            result[key] = [
                {
                    nested_key: nested_value
                    for nested_key, nested_value in item.items()
                    if nested_key not in omitted
                }
                if isinstance(item, Mapping)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def _professional_claim() -> dict[str, object]:
    """Build a documented professional 837 matching composite for the smoke."""
    return {
        "CH_CLIENT_CLAIM_ID": "C1",
        "CH_CLAIM_TYPE": "P",
        "CH_PATIENT_CLIENT_ID": "M1",
        "CH_CLAIM_SERVICE_FROM_DATE": "20260801",
        "CH_CLAIM_SERVICE_TO_DATE": "20260802",
        "CH_BILLING_PROVIDER_NPI": "1234567890",
        "CH_RENDERING_PROVIDER_NPI": "1234567890",
        "CH_PLACE_OF_SERVICE_CODE": "11",
        "CH_DIAGNOSIS_CODE_01": "I10",
        "CH_SUBSCRIBER_CLIENT_ID": "S1",
        "CH_CLAIM_FREQUENCY_CODE": "1",
        "CH_CHARGE_AMOUNT": "100.00",
        "CH_PATIENT_ACCOUNT_CONTROL_NUMBER": "PAC1",
        "CH_SOURCE_UPDATED_AT": "20260805",
        "CH_RECORD_TAG": "837 Provisional",
    }


def _institutional_claim() -> dict[str, object]:
    """Build a documented institutional 837 composite using the GDF line group."""
    return {
        "CH_CLIENT_CLAIM_ID": "C2",
        "CH_CLAIM_TYPE": "I",
        "CH_PATIENT_CLIENT_ID": "M1",
        "CH_CLAIM_SERVICE_FROM_DATE": "20260801",
        "CH_CLAIM_SERVICE_TO_DATE": "20260802",
        "CH_BILLING_PROVIDER_NPI": "1234567890",
        "CH_ATTENDING_PROVIDER_NPI": "1234567890",
        "CH_DIAGNOSIS_CODE_01": "I10",
        "CH_TYPE_OF_BILL_CODE": "131",
        "CH_SUBSCRIBER_CLIENT_ID": "S1",
        "CH_CLAIM_FREQUENCY_CODE": "1",
        "CH_CHARGE_AMOUNT": "100.00",
        "CH_PATIENT_ACCOUNT_CONTROL_NUMBER": "PAC2",
        "CH_SOURCE_UPDATED_AT": "20260805",
        "CH_RECORD_TAG": "837 Provisional",
        "CLAIM_DETAIL": [{"CD_SUBMITTED_REVENUE_CODE": "0510"}],
    }


if __name__ == "__main__":
    main()
