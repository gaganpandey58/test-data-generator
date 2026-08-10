"""Generate source-shaped medical claims with embedded 835 payment data."""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from healthcare_test_data.entities.member import generate_record as generate_member
from healthcare_test_data.entities.provider import generate_record as generate_provider
from healthcare_test_data.layouts import load_layout
from healthcare_test_data.scenarios import Scenario, plan


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int] | None = None,
    *,
    scenario: Scenario | None = None,
    profile: str | None = None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, object]:
    """Generate one GDF claim/payment envelope, optionally varying a baseline.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based claim position.
        entity_counts: Optional enabled-entity counts used for linked records.
        scenario: Optional planned claim variation.
        profile: Optional GDF claim profile override.
        entity_scenarios: Optional scenario quantities for linked member and
            provider output rows.

    Returns:
        Medical claim linked to deterministic member and provider source records.
    """
    if scenario is not None and scenario.baseline_index is not None:
        baseline = generate_record(
            seed,
            scenario.baseline_index,
            entity_counts,
            profile=profile,
            entity_scenarios=entity_scenarios,
        )
        return _mutate(baseline, scenario)

    randomizer = Random(_record_seed(seed, index))
    member = _linked_member(seed, index, entity_counts, entity_scenarios)
    provider = _linked_provider(seed, index, entity_counts, entity_scenarios)
    claim_type = _claim_type(index, profile)
    service_from_date = date(2025, 1, 1) + timedelta(days=randomizer.randrange(500))
    service_from = _compact_date(service_from_date)
    service_to = _compact_date(service_from_date + timedelta(days=randomizer.randrange(1, 4)))
    charge = round(randomizer.uniform(120, 1_200), 2)
    allowed = round(charge * 0.75, 2)
    copay = min(30.0, allowed)
    deductible = round((allowed - copay) * 0.1, 2)
    coinsurance = round((allowed - copay - deductible) * 0.2, 2)
    liability = round(copay + deductible + coinsurance, 2)
    paid = round(allowed - liability, 2)
    claim_id = f"CLM{index + 1:010d}"
    member_addresses = member["CM_MEMBER_ADDRESSES"]
    address = member_addresses[0] if isinstance(member_addresses, list) else {}
    record = _profile_blanks("claim-professional" if claim_type == "P" else "claim-institutional")
    record.update(
        {
            "CH_CLIENT_DATA_PLATFORM": "QNXT",
            "CH_CLIENT_CLAIM_UNIQUE_ID": f"CLU{index + 1:012d}",
            "CH_CLIENT_CLAIM_ID": claim_id,
            "CH_CLIENT_ROOT_CLAIM_ID": f"ROOT{index + 1:08d}",
            "CH_CLIENT_CLAIM_VERSION_NUMBER": "1",
            "CH_CLIENT_ORIGINAL_CLAIM_ID": "",
            "CH_NUMBER_OF_ADJUSTMENTS": 0,
            "CH_BILLING_PROVIDER_CLAIM_ID": f"BPC{index + 1:010d}",
            "CH_CLAIM_TYPE": claim_type,
            "CH_CLAIM_FREQUENCY_CODE": "1",
            "CH_PATIENT_CLIENT_ID": member["CM_MEMBER_CLIENT_ID"],
            "CH_PATIENT_CLIENT_MASTER_ID": member["CM_MEMBER_CLIENT_MASTER_ID"],
            "CH_PATIENT_FIRST_NAME": member["CM_MEMBER_FIRST_NAME"],
            "CH_PATIENT_LAST_NAME": member["CM_MEMBER_LAST_NAME"],
            "CH_PATIENT_BIRTH_DATE": member["CM_MEMBER_BIRTH_DATE"],
            "CH_PATIENT_GENDER": member["CM_MEMBER_GENDER"],
            "CH_SUBSCRIBER_CLIENT_ID": member["CM_SUBSCRIBER_CLIENT_ID"],
            "CH_SUBSCRIBER_CLIENT_MASTER_ID": member["CM_SUBSCRIBER_CLIENT_MASTER_ID"],
            "CH_SUBSCRIBER_SSN": member["CM_SUBSCRIBER_SSN"],
            "CH_BILLING_PROVIDER_CLIENT_ID": provider["CP_PROVIDER_CLIENT_ID"],
            "CH_BILLING_PROVIDER_CLIENT_MASTER_ID": provider["CP_PROVIDER_CLIENT_MASTER_ID"],
            "CH_BILLING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
            "CH_BILLING_PROVIDER_FEDERAL_TAX_ID": provider["CP_PROVIDER_FEDERAL_TAX_ID"],
            "CH_RENDERING_PROVIDER_CLIENT_ID": provider["CP_PROVIDER_CLIENT_ID"],
            "CH_RENDERING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
            "CH_REFERRING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
            "CH_SERVICE_FACILITY_NPI": provider["CP_PROVIDER_NPI"],
            "CH_ATTENDING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
            "CH_OPERATING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
            "CH_PATIENT_ACCOUNT_CONTROL_NUMBER": f"PAC{index + 1:010d}",
            "CH_CLAIM_SERVICE_FROM_DATE": service_from,
            "CH_CLAIM_SERVICE_TO_DATE": service_to,
            "CH_ADMISSION_DATE": service_from,
            "CH_DISCHARGE_DATE": service_to,
            "CH_ADMISSION_TYPE": "1",
            "CH_ADMISSION_SOURCE_CODE": "1",
            "CH_PATIENT_DISCHARGE_STATUS_CODE": "01",
            "CH_ICD_VERSION_CODE": "0",
            "CH_ADMITTING_DIAGNOSIS_CODE": "I10",
            "CH_DIAGNOSIS_CODE_01": randomizer.choice(("I10", "E119", "J069")),
            "CH_TYPE_OF_BILL_CODE": "131",
            "CH_PLACE_OF_SERVICE_CODE": randomizer.choice(("11", "22", "23")),
            "CH_CHARGE_AMOUNT": charge,
            "CH_ALLOWED_AMOUNT": allowed,
            "CH_COINSURANCE_AMOUNT": coinsurance,
            "CH_COPAY_AMOUNT": copay,
            "CH_DEDUCTIBLE_AMOUNT": deductible,
            "CH_PATIENT_LIABILITY_AMOUNT": liability,
            "CH_PAID_AMOUNT": paid,
            "CH_ADJUDICATED_DATE": service_to,
            "CH_CLAIM_PAID_DATE": service_to,
            "CH_CHECK_DATE": service_to,
            "CH_CHECK_NUMBER": f"CHK{index + 1:010d}",
            "CH_PAYMENT_CHECK_NUMBER": f"CHK{index + 1:010d}",
            "CH_PAYMENT_STATUS": "PAID",
            "CH_PAYMENT_STATUS_CODE": "1",
            "CH_PAYMENT_METHOD": "CHK",
            "CH_RECORD_TAG": "CH Verified" if index % 2 else "837 Provisional",
            "CH_RECORD_STATUS": "Active" if index % 2 else "New",
            "CH_SOURCE_UPDATED_AT": "20260805",
            "CH_PAYMENT_CLAIM_ID": claim_id,
            "CLAIM_DETAIL": [
                _line(
                    claim_id,
                    service_from,
                    service_to,
                    claim_type,
                    charge,
                    allowed,
                    copay,
                    deductible,
                    coinsurance,
                    liability,
                    paid,
                    provider,
                )
            ],
        }
    )
    if isinstance(address, Mapping):
        record["CH_PATIENT_ZIP"] = address.get("CM_MEMBER_ZIP", "")
    _remove_profile_exclusions(record, claim_type)
    return record


def _mutate(baseline: dict[str, object], scenario: Scenario) -> dict[str, object]:
    """Create a source-valid claim variation from a deterministic baseline.

    Args:
        baseline: Deterministic claim record selected by the scenario plan.
        scenario: Named variation to apply.

    Returns:
        A copied claim record with the requested source-valid variation.

    Raises:
        AssertionError: If replacement adjustment counts or detail rows have an
            unexpected shape.
    """
    record = _copy_record(baseline)
    if scenario.name == "duplicate":
        return record
    if scenario.name == "changed":
        record["CH_SOURCE_UPDATED_AT"] = "20260806"
        record["CH_RECORD_TAG"] = "837 Provisional"
        record["CH_RECORD_STATUS"] = "Active"
    elif scenario.name == "stale":
        record["CH_SOURCE_UPDATED_AT"] = "20260804"
        record["CH_CLAIM_PAID_DATE"] = "20250804"
        record["CH_CHECK_DATE"] = "20250804"
    elif scenario.name == "incomplete":
        record.pop("CH_SUBSCRIBER_SSN", None)
        record.pop("CH_REFERRING_PROVIDER_NPI", None)
        record.pop("CH_PATIENT_ACCOUNT_CONTROL_NUMBER", None)
    elif scenario.name == "replacement":
        record["CH_CLIENT_CLAIM_UNIQUE_ID"] = f"{record['CH_CLIENT_CLAIM_UNIQUE_ID']}-R2"
        record["CH_CLIENT_CLAIM_ID"] = f"{record['CH_CLIENT_CLAIM_ID']}-R2"
        record["CH_CLIENT_ORIGINAL_CLAIM_ID"] = baseline["CH_CLIENT_CLAIM_ID"]
        record["CH_CLIENT_CLAIM_VERSION_NUMBER"] = "2"
        adjustment_count = record["CH_NUMBER_OF_ADJUSTMENTS"]
        assert isinstance(adjustment_count, int)
        record["CH_NUMBER_OF_ADJUSTMENTS"] = adjustment_count + 1
        record["CH_CLAIM_FREQUENCY_CODE"] = "7"
        record["CH_SOURCE_UPDATED_AT"] = "20260806"
        record["CH_PAYMENT_CLAIM_ID"] = record["CH_CLIENT_CLAIM_ID"]
        _replacement_lines(record)
    elif scenario.name == "void":
        record["CH_CLAIM_FREQUENCY_CODE"] = "8"
        record["CH_SOURCE_UPDATED_AT"] = "20260806"
        record["CH_RECORD_STATUS"] = "Void"
    elif scenario.name == "orphan_payment":
        record["CH_RECORD_TAG"] = "835 Provisional"
        record["CH_RECORD_STATUS"] = "Orphan Payment"
        record["CH_PAYMENT_CLAIM_ID"] = f"ORPHAN-{record['CH_CLIENT_CLAIM_ID']}"
        record["CH_PATIENT_ACCOUNT_CONTROL_NUMBER"] = (
            f"ORPHAN-{record['CH_PATIENT_ACCOUNT_CONTROL_NUMBER']}"
        )
        _break_payment_composite(record)
        record["CH_SOURCE_UPDATED_AT"] = "20260806"
    return record


def _line(
    claim_id: str,
    service_from: str,
    service_to: str,
    claim_type: str,
    charge: float,
    allowed: float,
    copay: float,
    deductible: float,
    coinsurance: float,
    liability: float,
    paid: float,
    provider: Mapping[str, object],
) -> dict[str, object]:
    """Build one EIP/GDF claim-detail row whose payment amounts reconcile.

    Args:
        claim_id: Parent claim client ID.
        service_from: Compact service start date.
        service_to: Compact service end date.
        claim_type: Professional (``P``) or institutional (``I``) type.
        charge: Submitted charge amount.
        allowed: Adjudicated allowed amount.
        copay: Member copayment amount.
        deductible: Member deductible amount.
        coinsurance: Member coinsurance amount.
        liability: Total member liability amount.
        paid: Payer payment amount.
        provider: Linked provider source record.

    Returns:
        One source-shaped claim-detail mapping.
    """
    procedure = "99213" if claim_type == "P" else "99223"
    return {
        "CD_CLAIM_LINE_KEY": f"{claim_id}-01-{service_from}-{procedure}",
        "CD_CLAIM_LINE_NUMBER": 1,
        "CD_ORIGINAL_CLAIM_LINE_NUMBER": 1,
        "CD_SERVICE_FROM_DATE": service_from,
        "CD_SERVICE_TO_DATE": service_to,
        "CD_PLACE_OF_SERVICE_CODE": "11",
        "CD_DIAGNOSIS_POINTER_01": "1",
        "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER": "HC",
        "CD_SUBMITTED_PROCEDURE_CODE": procedure,
        "CD_SUBMITTED_REVENUE_CODE": "0510",
        "CD_ALLOWED_REVENUE_CODE": "0510",
        "CD_SUBMITTED_PROCEDURE_MODIFIER_01": "25",
        "CD_SUBMITTED_PROCEDURE_MODIFIER_02": "",
        "CD_SUBMITTED_UNITS": "1",
        "CD_SUBMITTED_UNITS_TYPE": "UN",
        "CD_CHARGE_AMOUNT": charge,
        "CD_ALLOWED_AMOUNT": allowed,
        "CD_COINSURANCE_AMOUNT": coinsurance,
        "CD_COPAY_AMOUNT": copay,
        "CD_DEDUCTIBLE_AMOUNT": deductible,
        "CD_PATIENT_LIABILITY_AMOUNT": liability,
        "CD_PAID_AMOUNT": paid,
        "CD_LINE_PAID_DATE": service_to,
        "CD_RENDERING_PROVIDER_CLIENT_ID": provider["CP_PROVIDER_CLIENT_ID"],
        "CD_RENDERING_PROVIDER_NPI": provider["CP_PROVIDER_NPI"],
        "CD_LINE_ADJUSTMENTS": [],
    }


def _replacement_lines(record: dict[str, object]) -> None:
    """Mark replacement detail rows with source-shaped adjustment values.

    Args:
        record: Replacement claim record with a ``CLAIM_DETAIL`` array.

    Raises:
        AssertionError: If claim detail is not a list of dictionaries.
    """
    lines = record["CLAIM_DETAIL"]
    assert isinstance(lines, list)
    for line in lines:
        assert isinstance(line, dict)
        line["CD_LINE_ADJUSTMENTS"] = [{"reason_code": "94", "amount": 0.0}]


def _break_payment_composite(record: dict[str, object]) -> None:
    """Change a source-supported key so an 835 payment cannot match a claim.

    Args:
        record: Orphan-payment claim envelope whose composite will be broken.

    Raises:
        AssertionError: If the expected first detail row is unavailable.
    """
    details = record["CLAIM_DETAIL"]
    assert isinstance(details, list) and details and isinstance(details[0], dict)
    line = details[0]
    if "CD_SUBMITTED_REVENUE_CODE" in line:
        line["CD_SUBMITTED_REVENUE_CODE"] = "9999"
    else:
        line["CD_PLACE_OF_SERVICE_CODE"] = "99"
        record["CH_PLACE_OF_SERVICE_CODE"] = "99"


def _remove_profile_exclusions(record: dict[str, object], claim_type: str) -> None:
    """Remove fields not represented by the active claim layout.

    Args:
        record: Claim envelope with profile-neutral fields populated.
        claim_type: Professional (``P``) or institutional (``I``) type.

    Raises:
        AssertionError: If claim detail is not a list of dictionaries.
    """
    excluded_headers = (
        _INSTITUTIONAL_ONLY_HEADERS if claim_type == "P" else _PROFESSIONAL_ONLY_HEADERS
    )
    for field in excluded_headers:
        record.pop(field, None)
    details = record["CLAIM_DETAIL"]
    assert isinstance(details, list)
    excluded_details = (
        _INSTITUTIONAL_ONLY_DETAIL_FIELDS if claim_type == "P" else _PROFESSIONAL_ONLY_DETAIL_FIELDS
    )
    for line in details:
        assert isinstance(line, dict)
        for field in excluded_details:
            line.pop(field, None)


_INSTITUTIONAL_ONLY_HEADERS = (
    "CH_TYPE_OF_BILL_CODE",
    "CH_ADMISSION_DATE",
    "CH_DISCHARGE_DATE",
    "CH_ADMISSION_TYPE",
    "CH_ADMISSION_SOURCE_CODE",
    "CH_PATIENT_DISCHARGE_STATUS_CODE",
    "CH_ADMITTING_DIAGNOSIS_CODE",
    "CH_ATTENDING_PROVIDER_NPI",
    "CH_OPERATING_PROVIDER_NPI",
)
_PROFESSIONAL_ONLY_HEADERS = (
    "CH_PLACE_OF_SERVICE_CODE",
    "CH_SUBSCRIBER_SSN",
    "CH_RENDERING_PROVIDER_CLIENT_ID",
    "CH_RENDERING_PROVIDER_NPI",
    "CH_REFERRING_PROVIDER_NPI",
    "CH_SERVICE_FACILITY_NPI",
)
_INSTITUTIONAL_ONLY_DETAIL_FIELDS = (
    "CD_SUBMITTED_REVENUE_CODE",
    "CD_ALLOWED_REVENUE_CODE",
)
_PROFESSIONAL_ONLY_DETAIL_FIELDS = (
    "CD_PLACE_OF_SERVICE_CODE",
    "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_01",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_02",
    "CD_RENDERING_PROVIDER_CLIENT_ID",
    "CD_RENDERING_PROVIDER_NPI",
)


def _profile_blanks(profile: str) -> dict[str, object]:
    """Initialize the selected profile's root fields as blanks.

    Args:
        profile: Checked-in GDF claim profile to load.

    Returns:
        A field-name-to-blank-value mapping for the selected profile.
    """
    layout = load_layout(profile)
    return {field.name: "" for field in layout.root}


def _copy_record(record: dict[str, object]) -> dict[str, object]:
    """Copy mutable embedded detail rows before applying a scenario variation.

    Args:
        record: Baseline claim record to copy.

    Returns:
        A shallow root copy with independent detail and adjustment collections.

    Raises:
        AssertionError: If ``CLAIM_DETAIL`` is not a list.
    """
    copied = dict(record)
    details = record["CLAIM_DETAIL"]
    assert isinstance(details, list)
    copied["CLAIM_DETAIL"] = [
        {**line, "CD_LINE_ADJUSTMENTS": list(line.get("CD_LINE_ADJUSTMENTS", []))}
        for line in details
        if isinstance(line, dict)
    ]
    return copied


def _claim_type(index: int, profile: str | None) -> str:
    """Resolve a source profile to its GDF professional/institutional type.

    Args:
        index: Stable claim position used for the mixed-profile default.
        profile: Optional explicit claim layout profile.

    Returns:
        ``P`` or ``I`` for the selected profile.
    """
    if profile == "claim-professional":
        return "P"
    if profile == "claim-institutional":
        return "I"
    return "P" if index % 2 else "I"


def _record_seed(seed: int, index: int) -> int:
    """Derive a stable claim-local seed.

    Args:
        seed: Shared generation seed.
        index: Stable claim position.

    Returns:
        Deterministic integer seed for this claim.
    """
    return (seed * 1_000_037) + index


def _entity_count(entity_counts: Mapping[str, int] | None, entity_name: str) -> int:
    """Return a positive configured related-entity count.

    Args:
        entity_counts: Optional enabled-entity output counts.
        entity_name: Related entity to count.

    Returns:
        Configured positive count or deterministic fallback count.
    """
    configured_count = entity_counts.get(entity_name, 10) if entity_counts else 10
    return max(1, configured_count)


def _linked_member(
    seed: int,
    claim_index: int,
    entity_counts: Mapping[str, int] | None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None,
) -> dict[str, object]:
    """Generate the exact member row selected from the configured output set.

    Args:
        seed: Shared deterministic generation seed.
        claim_index: Stable zero-based claim position.
        entity_counts: Enabled entity output counts, if supplied.
        entity_scenarios: Enabled entity scenario quantities, if supplied.

    Returns:
        The emitted member record selected for this claim, or a deterministic
        source-shaped fallback record when members are not part of the run.
    """
    member_index, scenario = _linked_position(
        seed, claim_index, "member", entity_counts, entity_scenarios
    )
    return generate_member(seed, member_index, entity_counts, scenario=scenario)


def _linked_provider(
    seed: int,
    claim_index: int,
    entity_counts: Mapping[str, int] | None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None,
) -> dict[str, object]:
    """Generate the exact provider row selected from the configured output set.

    Args:
        seed: Shared deterministic generation seed.
        claim_index: Stable zero-based claim position.
        entity_counts: Enabled entity output counts, if supplied.
        entity_scenarios: Enabled entity scenario quantities, if supplied.

    Returns:
        The emitted provider record selected for this claim, or a deterministic
        source-shaped fallback record when providers are not part of the run.
    """
    provider_index, scenario = _linked_position(
        seed, claim_index, "provider", entity_counts, entity_scenarios
    )
    return generate_provider(seed, provider_index, scenario=scenario)


def _linked_position(
    seed: int,
    claim_index: int,
    entity_name: str,
    entity_counts: Mapping[str, int] | None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None,
) -> tuple[int, Scenario | None]:
    """Select the emitted relationship row and its corresponding variation.

    Args:
        seed: Shared deterministic generation seed.
        claim_index: Stable zero-based claim position.
        entity_name: Related entity to select.
        entity_counts: Enabled entity output counts, if supplied.
        entity_scenarios: Enabled entity scenario quantities, if supplied.

    Returns:
        The output-row position and internal variation to use for generation.
    """
    count = _entity_count(entity_counts, entity_name)
    output_index = claim_index % count
    scenarios = entity_scenarios.get(entity_name, {}) if entity_scenarios else {}
    return output_index, plan(count, scenarios, seed).variation_for(output_index)


def _compact_date(value: date) -> str:
    """Format a date as the GDF compact date value.

    Args:
        value: Date to format.

    Returns:
        Compact ``YYYYMMDD`` source value.
    """
    return value.strftime("%Y%m%d")
