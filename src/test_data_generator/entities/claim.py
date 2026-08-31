"""Generate deterministic medical claims with embedded payment information.

This module produces professional or institutional claim envelopes from the
checked-in GDF layouts. A claim references generated member and provider
records and contains its line-level detail and payment fields in the same JSON
object.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from test_data_generator.configuration.profiles import (
    nested_header_values,
    record_header_values,
)
from test_data_generator.core.identifiers import deterministic_uuid4
from test_data_generator.entities.member import generate_record as generate_member
from test_data_generator.entities.provider import generate_record as generate_provider
from test_data_generator.layouts import load_layout
from test_data_generator.samples.shapes import complete_record


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int],
    client_headers: Mapping[str, object],
    client_values: Mapping[str, object],
    profile: str,
    lifecycle: tuple[str, int | None] | None = None,
) -> dict[str, object]:
    """Generate one GDF claim/payment happy-path envelope.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based claim position.
        entity_counts: Enabled member and provider row counts for linked records.
        client_headers: Client-specific envelope header values.
        client_values: Reserved client-owned claim values.
        profile: ``claim-professional`` or ``claim-institutional``.
        lifecycle: Claim frequency and source original-Claim index, when the
            configured Claim stream includes replacement or void fixtures.

    Returns:
        Medical claim linked to deterministic member and provider source records.
    """
    del client_values
    randomizer = Random(_record_seed(seed, index))
    claim_type = _claim_type(profile)
    frequency, original_index = _lifecycle(lifecycle, index)
    patient_index = original_index if frequency in {"7", "8"} else index
    assert patient_index is not None
    patient_identity_index = patient_index + (1_000_000 if claim_type == "I" else 0)
    member = _linked_member(seed, patient_identity_index, entity_counts)
    provider = _linked_provider(seed, index, entity_counts)
    profile_code = "P" if claim_type == "P" else "I"
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
    if frequency == "8":
        allowed = 0.0
        copay = 0.0
        deductible = 0.0
        coinsurance = 0.0
        liability = 0.0
        paid = 0.0
    claim_id = _claim_id(profile_code, index, frequency)
    original_claim_id = claim_id
    root_index = index
    if frequency in {"7", "8"}:
        assert original_index is not None
        original_claim_id = _claim_id(profile_code, original_index, "1")
        root_index = original_index
    entity_name = "claim_professional" if claim_type == "P" else "claim_institutional"
    member_addresses = member["CM_MEMBER_ADDRESSES"]
    address = member_addresses[0] if isinstance(member_addresses, list) else {}
    record = _profile_blanks("claim-professional" if claim_type == "P" else "claim-institutional")
    record.update(
        {
            "CH_CLIENT_CLAIM_UNIQUE_ID": f"{profile_code}CLU{index + 1:011d}",
            "CH_CLIENT_CLAIM_ID": claim_id,
            "CH_CLIENT_ROOT_CLAIM_ID": f"{profile_code}ROOT{root_index + 1:08d}",
            "CH_CLIENT_CLAIM_VERSION_NUMBER": "1" if frequency == "1" else "2",
            "CH_CLIENT_ORIGINAL_CLAIM_ID": original_claim_id,
            "CH_NUMBER_OF_ADJUSTMENTS": 0 if frequency == "1" else 1,
            "CH_BILLING_PROVIDER_CLAIM_ID": f"BPC{index + 1:010d}",
            "CH_CLAIM_TYPE": claim_type,
            "CH_CLAIM_FREQUENCY_CODE": frequency,
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
            "CH_PAYMENT_STATUS": "VOID" if frequency == "8" else "PAID",
            "CH_PAYMENT_STATUS_CODE": "1",
            "CH_PAYMENT_METHOD": "CHK",
            "CH_CMS_CLAIM_ADJUSTMENT_TYPE_CODE": _adjustment_type(frequency),
            "CH_CMS_CLAIM_QUERY_CODE": _query_code(frequency),
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
                    frequency,
                )
            ],
        }
    )
    record.update(
        _transport_headers(seed, index, claim_type, claim_id, entity_name, client_headers)
    )
    if isinstance(address, Mapping):
        _set_existing_fields(
            record,
            {
                "CH_PATIENT_MIDDLE_NAME": member.get("CM_MEMBER_MIDDLE_NAME", ""),
                "CH_PATIENT_ADDRESS_01": address.get("CM_MEMBER_ADDRESS_01", ""),
                "CH_PATIENT_CITY": address.get("CM_MEMBER_CITY", ""),
                "CH_PATIENT_STATE": address.get("CM_MEMBER_STATE", ""),
                "CH_PATIENT_ZIP": address.get("CM_MEMBER_ZIP", ""),
                "CH_PATIENT_ZIP_PLUS_FOUR": address.get("CM_MEMBER_ZIP_PLUS_FOUR", ""),
                "CH_PATIENT_PHONE": address.get("CM_MEMBER_PHONE", ""),
            },
        )
    provider_addresses = provider.get("CP_PROVIDER_ADDRESSES")
    provider_address = (
        provider_addresses[0]
        if isinstance(provider_addresses, list)
        and provider_addresses
        and isinstance(provider_addresses[0], Mapping)
        else {}
    )
    _set_existing_fields(
        record,
        {
            "CH_BILLING_PROVIDER_FIRST_NAME": provider.get("CP_PROVIDER_FIRST_NAME", ""),
            "CH_BILLING_PROVIDER_MIDDLE_NAME": provider.get("CP_PROVIDER_MIDDLE_NAME", ""),
            "CH_BILLING_PROVIDER_LAST_NAME": provider.get("CP_PROVIDER_LAST_NAME", ""),
            "CH_BILLING_PROVIDER_FULL_NAME": provider.get("CP_PROVIDER_FULL_NAME", ""),
            "CH_BILLING_PROVIDER_ADDRESS_01": provider_address.get("CP_PROVIDER_ADDRESS_01", ""),
            "CH_BILLING_PROVIDER_ADDRESS_02": provider_address.get("CP_PROVIDER_ADDRESS_02", ""),
            "CH_BILLING_PROVIDER_CITY": provider_address.get("CP_PROVIDER_CITY", ""),
            "CH_BILLING_PROVIDER_STATE": provider_address.get("CP_PROVIDER_STATE", ""),
            "CH_BILLING_PROVIDER_ZIP": provider_address.get("CP_PROVIDER_ZIP", ""),
            "CH_BILLING_PROVIDER_ZIP_PLUS_FOUR": provider_address.get(
                "CP_PROVIDER_ZIP_PLUS_FOUR", ""
            ),
            "CH_BILLING_PROVIDER_PHONE": provider_address.get("CP_PROVIDER_PHONE", ""),
            "CH_RENDERING_PROVIDER_FIRST_NAME": provider.get("CP_PROVIDER_FIRST_NAME", ""),
            "CH_RENDERING_PROVIDER_MIDDLE_NAME": provider.get("CP_PROVIDER_MIDDLE_NAME", ""),
            "CH_RENDERING_PROVIDER_LAST_NAME": provider.get("CP_PROVIDER_LAST_NAME", ""),
            "CH_RENDERING_PROVIDER_ADDRESS_01": provider_address.get("CP_PROVIDER_ADDRESS_01", ""),
            "CH_RENDERING_PROVIDER_CITY": provider_address.get("CP_PROVIDER_CITY", ""),
            "CH_RENDERING_PROVIDER_STATE": provider_address.get("CP_PROVIDER_STATE", ""),
            "CH_RENDERING_PROVIDER_ZIP": provider_address.get("CP_PROVIDER_ZIP", ""),
            "CH_RENDERING_PROVIDER_ZIP_PLUS_FOUR": provider_address.get(
                "CP_PROVIDER_ZIP_PLUS_FOUR", ""
            ),
            "CH_RENDERING_PROVIDER_PHONE": provider_address.get("CP_PROVIDER_PHONE", ""),
        },
    )
    completed = _complete_source_shape(record, claim_type)
    if claim_type == "I":
        completed["otherAttributes"] = None
    return completed


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
    frequency: str,
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
        frequency: Parent Claim lifecycle frequency code.

    Returns:
        One source-shaped claim-detail mapping.
    """
    procedure = "99213" if claim_type == "P" else "99223"
    line = {
        "CD_CLAIM_LINE_NUMBER": 1,
        "CD_ORIGINAL_CLAIM_LINE_NUMBER": 0,
        "CD_SERVICE_FROM_DATE": service_from,
        "CD_SERVICE_TO_DATE": service_to,
        "CD_DIAGNOSIS_POINTER_01": "1",
        "CD_SUBMITTED_PROCEDURE_CODE": procedure,
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
    if claim_type == "P":
        line.update(
            {
                "CD_PLACE_OF_SERVICE_CODE": "11",
                "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER": "HCPCS",
                "CD_SUBMITTED_PROCEDURE_MODIFIER_01": "25",
                "CD_SUBMITTED_PROCEDURE_MODIFIER_02": "",
                "CD_RENDERING_PROVIDER_ENTITY_TYPE": (
                    "P" if provider.get("CP_PROVIDER_RECORD_TYPE") == "1" else "O"
                ),
                "CD_RENDERING_PROVIDER_FIRST_NAME": provider.get("CP_PROVIDER_FIRST_NAME", ""),
                "CD_RENDERING_PROVIDER_LAST_NAME": provider.get("CP_PROVIDER_LAST_NAME", ""),
                "CD_RENDERING_PROVIDER_TAXONOMY_CODE": provider.get(
                    "CP_PROVIDER_TAXONOMY_CODE", ""
                ),
                "CH_CLAIM_FILING_INDICATOR_CODE": "CI",
            }
        )
    else:
        line.update(
            {
                "CD_SUBMITTED_REVENUE_CODE": "0510",
                "CD_ALLOWED_REVENUE_CODE": "0510",
                "CD_NUMBER_OF_ADJUSTMENTS": 0,
                "CD_PAYMENT_METHOD": "CHK",
                "CD_PAYMENT_STATUS": "VOID" if frequency == "8" else "PAID",
            }
        )
    return line


def _set_existing_fields(record: dict[str, object], values: Mapping[str, object]) -> None:
    """Copy a related entity value only where the active Claim layout defines it."""
    for field, value in values.items():
        if field in record:
            record[field] = value


def _lifecycle(lifecycle: tuple[str, int | None] | None, index: int) -> tuple[str, int | None]:
    """Return a validated Claim lifecycle, defaulting to an original Claim."""
    if lifecycle is None:
        return "1", index
    frequency, original_index = lifecycle
    if frequency not in {"1", "7", "8"}:
        raise ValueError(f"Unsupported Claim frequency {frequency!r}")
    if frequency in {"7", "8"} and original_index is None:
        raise ValueError(f"Claim frequency {frequency} requires an original Claim index")
    return frequency, original_index


def _claim_id(profile_code: str, index: int, frequency: str) -> str:
    """Create a unique Claim ID while making replacement and void fixtures visible."""
    suffix = {"1": "", "7": "-R2", "8": "-V8"}[frequency]
    return f"{profile_code}CLM{index + 1:09d}{suffix}"


def _adjustment_type(frequency: str) -> str:
    """Map GDF Claim frequency values to their adjustment lifecycle code."""
    return {"1": "0", "7": "2", "8": "1"}[frequency]


def _query_code(frequency: str) -> str:
    """Emit a deterministic GDF payment-processing query code per lifecycle."""
    return {"1": "3", "7": "5", "8": "0"}[frequency]


def _complete_source_shape(record: dict[str, object], claim_type: str) -> dict[str, object]:
    """Complete a claim with its claim and payment sample-type patterns.

    Claims carry payment fields in the same generated JSON object. Both
    supplied source patterns therefore participate in the default/type pass,
    while all populated values remain test values from this generator.

    Args:
        record: Test claim envelope ready for source-shape completion.
        claim_type: ``P`` for professional or ``O`` for institutional.

    Returns:
        Source-complete, JSON-kind-normalized claim/payment envelope.
    """
    suffix = "professional" if claim_type == "P" else "institutional"
    other_suffix = "institutional" if suffix == "professional" else "professional"
    return complete_record(
        record,
        f"claim_{suffix}",
        f"payment_{suffix}",
        f"claim_{other_suffix}",
        f"payment_{other_suffix}",
    )


def _transport_headers(
    seed: int,
    index: int,
    claim_type: str,
    claim_id: str,
    entity_name: str | None,
    client_headers: Mapping[str, object] | None,
) -> dict[str, object]:
    """Build Cotiviti transport attributes for one independent 837 envelope.

    The sample payloads use flattened ``cotiviti.*`` keys rather than a
    nested transport object.  These values deliberately model that wire
    format while remaining test and repeatable.  The standardized Claim
    contract uses a UUID ``ROWID`` for both source streams.

    Args:
        seed: Shared generation seed used for deterministic UUID namespaces.
        index: Stable zero-based row position within the selected claim profile.
        claim_type: ``P`` for 837 professional or ``O`` for 837 institutional.
        claim_id: Test claim identifier used in source-control metadata.
        entity_name: Optional configured entity name for traceable namespaces.
        client_headers: Resolved client-specific header values, if supplied.

    Returns:
        Flattened transport/header attributes ready to merge into a claim row.
    """
    file_type = "837P" if claim_type == "P" else "837I"
    namespace = entity_name or (
        "claim_professional" if claim_type == "P" else "claim_institutional"
    )
    sequence = index + 1
    batch = f"test-{namespace}-{seed}-{sequence:06d}"
    profile_entity = entity_name or (
        "claim_professional" if claim_type == "P" else "claim_institutional"
    )
    headers: dict[str, object] = {
        "FILE_TYPE": file_type,
        "INGESTION_DATE": "20260805",
        "INGESTION_EPOCH": 1785888000 + sequence,
        "DATA_CATEGORY": "MEDICAL_CLAIMS",
        "GDF_VERSION": "v2.9",
        "PUBLISHER_NAME": "test-data-generator",
        "x-connector-name": "test-eip-837",
        "cotiviti.dataset_id": "medical_claims",
        "cotiviti.schema_version": "gdf-eip-v1",
        "cotiviti.source_format": f"edi_x12_{file_type}",
        "cotiviti.batch_id": batch,
        "cotiviti.correlation_id": deterministic_uuid4(seed, f"{namespace}:correlation"),
        "cotiviti.message_id": deterministic_uuid4(seed, f"{namespace}:message:{sequence}"),
        "cotiviti.message_seq": sequence,
        "cotiviti.produced_at": f"2026-08-05T00:{index % 60:02d}:00Z",
        "cotiviti.producer_version": "test-data-generator/1",
        "cotiviti.source.isa_control": f"{seed % 1_000_000_000:09d}",
        "cotiviti.source.gs_control": sequence,
        "cotiviti.source.st_control": f"{sequence:04d}",
        "cotiviti.source.claim_id": claim_id,
        "cotiviti.source.raw_file_ref": f"{namespace}-{seed}-{sequence:06d}.x12",
        "otherAttributes": _other_attributes(
            claim_type,
            sequence,
            nested_header_values(profile_entity, client_headers or {}, "otherAttributes"),
        ),
    }
    headers["ROWID"] = deterministic_uuid4(seed, f"{namespace}:row:{sequence}")
    headers.update(record_header_values(profile_entity, client_headers or {}))
    return headers


def _other_attributes(
    claim_type: str, sequence: int, client_headers: Mapping[str, object]
) -> dict[str, str] | None:
    """Create the source-compatible EDI control attributes for a claim row.

    Professional examples carry an ``otherAttributes`` object while the
    institutional example carries ``null``.  Keeping that distinction makes
    the generated envelopes match the source contracts without reusing sample
    values.

    Args:
        claim_type: ``P`` for professional or ``O`` for institutional.
        sequence: One-based deterministic transaction sequence.
        client_headers: Declared nested payer values from the client profile.

    Returns:
        Professional EDI control attributes, or ``None`` for institutional
        claims as represented by the supplied institutional source sample.
    """
    if claim_type != "P":
        return None
    control = f"{sequence:04d}"
    return {
        "payerName": str(client_headers.get("payerName", "")),
        "payerIdentifier": str(client_headers.get("payerIdentifier", "")),
        "payerIdCodeQualifier": str(client_headers.get("payerIdCodeQualifier", "")),
        "payerAddressLine1": str(client_headers.get("payerAddressLine1", "")),
        "payerCity": str(client_headers.get("payerCity", "")),
        "payerState": str(client_headers.get("payerState", "")),
        "payerZip": str(client_headers.get("payerZip", "")),
        "submitterName": str(client_headers.get("submitterName", "")),
        "submitterIdentifier": str(client_headers.get("submitterIdentifier", "")),
        "submitterTelephone": str(client_headers.get("submitterTelephone", "")),
        "receiverName": str(client_headers.get("receiverName", "")),
        "receiverIdentifier": str(client_headers.get("receiverIdentifier", "")),
        "tradingPartner": str(client_headers.get("tradingPartner", "")),
        "batchPurpose": "CH",
        "claimPurpose": "CH",
        "batchControlNumber": control,
        "interchangeControlNumber": f"{sequence:09d}",
        "functionalGroupControlNumber": str(sequence),
        "transactionSetControlNumber": control,
        "interchangeUsageIndicator": str(client_headers.get("interchangeUsageIndicator", "")),
        "interchangeSenderIdentifierQualifier": str(
            client_headers.get("interchangeSenderIdentifierQualifier", "")
        ),
        "interchangeReceiverIdentifierQualifier": str(
            client_headers.get("interchangeReceiverIdentifierQualifier", "")
        ),
    }


def _profile_blanks(profile: str) -> dict[str, object]:
    """Initialize the selected profile's root fields as blanks.

    Args:
        profile: Checked-in GDF claim profile to load.

    Returns:
        A field-name-to-blank-value mapping for the selected profile.
    """
    layout = load_layout(profile)
    return {field.name: "" for field in layout.root}


def _claim_type(profile: str) -> str:
    """Resolve a source profile to its GDF professional/institutional type.

    Args:
        profile: Explicit claim layout profile.

    Returns:
        ``P`` or ``I`` for the selected professional/institutional stream.
    """
    if profile == "claim-professional":
        return "P"
    if profile == "claim-institutional":
        return "I"
    raise ValueError(f"Unsupported claim layout {profile!r}")


def _record_seed(seed: int, index: int) -> int:
    """Derive a stable claim-local seed.

    Args:
        seed: Shared generation seed.
        index: Stable claim position.

    Returns:
        Deterministic integer seed for this claim.
    """
    return (seed * 1_000_037) + index


def _entity_count(entity_counts: Mapping[str, int], entity_name: str) -> int:
    """Return a positive configured related-entity count.

    Args:
        entity_counts: Enabled entity output counts.
        entity_name: Related entity to count.

    Returns:
        Configured positive count or deterministic fallback count.
    """
    return max(1, entity_counts.get(entity_name, 10))


def _linked_member(
    seed: int,
    patient_index: int,
    entity_counts: Mapping[str, int],
) -> dict[str, object]:
    """Generate the Claim-owned patient identity for a Claim lifecycle.

    Args:
        seed: Shared deterministic generation seed.
        patient_index: Stable zero-based identity index. Replacements and voids
            reuse the original Claim's patient index.
        entity_counts: Enabled entity output counts.

    Returns:
        Deterministic source-shaped patient/member data owned by the Claim.
    """
    return generate_member(seed, patient_index, entity_counts, {}, {}, "member")


def _linked_provider(
    seed: int,
    claim_index: int,
    entity_counts: Mapping[str, int],
) -> dict[str, object]:
    """Generate the exact provider row selected from the configured output set.

    Args:
        seed: Shared deterministic generation seed.
        claim_index: Stable zero-based claim position.
        entity_counts: Enabled entity output counts.

    Returns:
        The emitted provider record selected for this claim, or a deterministic
        source-shaped fallback record when providers are not part of the run.
    """
    provider_index = claim_index % _entity_count(entity_counts, "provider")
    return generate_provider(seed, provider_index, entity_counts, {}, {}, "provider")


def _compact_date(value: date) -> str:
    """Format a date as the GDF compact date value.

    Args:
        value: Date to format.

    Returns:
        Compact ``YYYYMMDD`` source value.
    """
    return value.strftime("%Y%m%d")
