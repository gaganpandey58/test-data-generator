"""Generate complete Payment I/P records linked to Claims.

The claim builder owns the shared healthcare identity and claim-line values.
This module owns the Payment envelope contract, payment-specific amount
reconciliation, Payment I/P distinctions, and the source-defined relationship
fields that must remain aligned with the originating Claim. It also derives
Payments from immutable existing Claim JSONL files for configured scenarios.
"""

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from test_data_generator.core.dates import current_ingestion_date
from test_data_generator.core.identifiers import deterministic_uuid4
from test_data_generator.entities.claim import generate_record as generate_claim
from test_data_generator.layouts import project_record
from test_data_generator.samples.shapes import complete_record
from test_data_generator.update.payment_relationships import PAYMENT_MATCHING_RULES

_PAYMENT_TYPES = {
    "payment-professional": "P",
    "payment-institutional": "I",
}
_PAYMENT_FILE_TYPE = "835"
_PAYMENT_SOURCE_FORMAT = "edi_x12_835"
_PAYMENT_SOURCES = {
    "payment-professional": "payment_professional",
    "payment-institutional": "payment_institutional",
}
_PAYMENT_HEADER_FIELDS = (
    "CH_DIAGNOSIS_CODE_01",
    "CH_BILLING_PROVIDER_CLAIM_ID",
    "CH_PATIENT_CLIENT_ID",
    "CH_CREDIT_DEBIT_FLAG_CODE",
    "CH_PAYMENT_METHOD_CODE",
    "CH_REFERENCE_IDENTIFICATION_NUMBER",
    "CH_CLAIM_TYPE",
    "CH_CHARGE_AMOUNT",
    "CH_PAID_AMOUNT",
    "CH_CLAIM_FILING_INDICATOR_CODE",
    "CH_PAYER_ORGANIZATION_NAME",
    "CH_CLIENT_RECEIVED_DATE",
    "CH_CLIENT_CLAIM_ID",
    "CH_CLIENT_ORIGINAL_CLAIM_ID",
    "CH_PLACE_OF_SERVICE_CODE",
    "CH_TYPE_OF_BILL_CODE",
    "CH_CLAIM_FREQUENCY_CODE",
    "CH_CLAIM_SERVICE_FROM_DATE",
    "CH_CLAIM_SERVICE_TO_DATE",
    "CH_BILLING_PROVIDER_FEDERAL_TAX_ID",
    "CH_BILLING_PROVIDER_NPI",
    "CH_BILLING_PROVIDER_FULL_NAME",
    "CH_RENDERING_PROVIDER_FEDERAL_TAX_ID",
    "CH_RENDERING_PROVIDER_NPI",
    "CH_PATIENT_ACCOUNT_CONTROL_NUMBER",
    "CH_PATIENT_MEDICAL_RECORD_NUMBER",
    "CH_SUBSCRIBER_CLIENT_ID",
    "CH_CLAIM_PAID_DATE",
    "CH_ALLOWED_AMOUNT",
    "CH_COINSURANCE_AMOUNT",
    "CH_COPAY_AMOUNT",
    "CH_DEDUCTIBLE_AMOUNT",
    "CH_PATIENT_LIABILITY_AMOUNT",
    "CH_CLAIM_STATUS_CODE",
    "CH_PAYER_ID",
    "CH_PAYEE_TAX_ID",
    "CH_PAYEE_ID",
    "CH_PAYEE_ID_QUALIFIER",
)
_PAYMENT_METADATA_FIELDS = (
    "cotiviti.dataset_id",
    "cotiviti.tenant_id",
    "cotiviti.schema_version",
    "cotiviti.client_id",
    "cotiviti.client_system",
    "cotiviti.message_id",
    "cotiviti.produced_at",
    "cotiviti.source_format",
    "cotiviti.source_system",
    "cotiviti.batch_id",
    "cotiviti.message_seq",
    "cotiviti.correlation_id",
    "cotiviti.producer_version",
    "cotiviti.source.isa_control",
    "cotiviti.source.gs_control",
    "cotiviti.source.raw_file_ref",
)
_PAYMENT_ENVELOPE_FIELDS = (
    "INGESTION_EPOCH",
    "ROWID",
    "PAYER",
    "PRODUCT",
    "GDF_VERSION",
    "FILE_TYPE",
    "DATA_CATEGORY",
    "LOB",
    "INGESTION_DATE",
    "PUBLISHER_NAME",
)
_PAYMENT_DETAIL_FIELDS = (
    "CD_CHARGE_AMOUNT",
    "CD_PAID_AMOUNT",
    *(
        field
        for number in range(1, 7)
        for field in (
            f"CD_CLAIM_ADJUSTMENT_GROUP_CODE_{number}",
            f"CD_CLAIM_ADJUSTMENT_REASON_CODE_{number}",
            f"CD_ADJUSTMENT_AMOUNT_{number}",
        )
    ),
    "CD_REMITTANCE_ADVICE_GROUP_CODE",
    "CD_REMITTANCE_ADVICE_REASON_CODE",
    "CD_SERVICE_FROM_DATE",
    "CD_SERVICE_TO_DATE",
    "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER",
    "CD_SUBMITTED_PROCEDURE_CODE",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_01",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_02",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_03",
    "CD_SUBMITTED_PROCEDURE_MODIFIER_04",
    "CD_SUBMITTED_REVENUE_CODE",
    "CD_LINE_PAID_DATE",
    "CD_ALLOWED_AMOUNT",
    "CD_COINSURANCE_AMOUNT",
    "CD_COPAY_AMOUNT",
    "CD_DEDUCTIBLE_AMOUNT",
    "CD_PATIENT_LIABILITY_AMOUNT",
    "CD_ALLOWED_PROCEDURE_CODE",
)
_PAYMENT_ROOT_FIELDS = frozenset(
    (*_PAYMENT_HEADER_FIELDS, *_PAYMENT_METADATA_FIELDS, *_PAYMENT_ENVELOPE_FIELDS, "CLAIM_DETAIL")
)
_CLAIM_IDS = ("CH_CLIENT_CLAIM_ID", "CLAIM_ID", "CH_CLAIM_ID")
_ORIGINAL_CLAIM_IDS = (
    "CH_CLIENT_ORIGINAL_CLAIM_ID",
    "ORIGINAL_CLAIM_ID",
    "CH_ORIGINAL_CLAIM_ID",
)
_CLAIM_LINEAGE_FIELDS = (
    "CH_CLIENT_CLAIM_ID",
    "CH_CLIENT_ORIGINAL_CLAIM_ID",
    "CH_CLIENT_ROOT_CLAIM_ID",
    "CH_CLIENT_CLAIM_UNIQUE_ID",
    "CH_CLIENT_CLAIM_VERSION_NUMBER",
)
_PATIENT_RELATIONSHIP_FIELDS = (
    "CH_PATIENT_CLIENT_ID",
    "CH_PATIENT_CLIENT_MASTER_ID",
    "CH_PATIENT_FIRST_NAME",
    "CH_PATIENT_MIDDLE_NAME",
    "CH_PATIENT_LAST_NAME",
    "CH_PATIENT_BIRTH_DATE",
    "CH_PATIENT_GENDER",
    "CH_SUBSCRIBER_CLIENT_ID",
    "CH_SUBSCRIBER_CLIENT_MASTER_ID",
    "CH_SUBSCRIBER_SSN",
)
_ORPHAN_SOURCE_INDEX_OFFSET = 10_000_000


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int],
    client_headers: Mapping[str, object],
    client_values: Mapping[str, object],
    profile: str,
) -> dict[str, object]:
    """Generate one Payment I or Payment P source-shaped record.

    Payment I and Payment P share the claim/payment relationship and generator
    contract, while their layouts retain the distinct source field sets.
    """
    if profile not in {"payment-professional", "payment-institutional"}:
        raise ValueError(f"Unsupported payment layout {profile!r}")
    claim_profile = (
        "claim-professional" if profile == "payment-professional" else "claim-institutional"
    )
    claim = generate_claim(
        seed,
        index,
        entity_counts,
        client_headers,
        client_values,
        claim_profile,
    )
    return derive_payment_from_claim(claim, profile, seed, index)


def _validate_payment_record(record: Mapping[str, object], profile: str) -> None:
    """Validate the generated Payment envelope and relationship shape."""
    unexpected = sorted(set(record) - _PAYMENT_ROOT_FIELDS)
    absent = [field for field in _PAYMENT_ROOT_FIELDS if field not in record]
    optional_empty_root = {"CH_TYPE_OF_BILL_CODE"} if profile == "payment-professional" else set()
    missing = [
        field
        for field in _PAYMENT_ROOT_FIELDS
        if field in record
        and field not in optional_empty_root
        and field != "CLAIM_DETAIL"
        and not _present(record[field])
    ]
    if unexpected:
        raise ValueError(
            f"Generated {profile} payment contains unsupported fields: {', '.join(unexpected)}"
        )
    if absent or missing:
        missing_fields = sorted({*absent, *missing})
        raise ValueError(
            f"Generated {profile} payment is missing fields: {', '.join(missing_fields)}"
        )
    if record["FILE_TYPE"] != _PAYMENT_FILE_TYPE:
        raise ValueError(f"Generated {profile} payment has an invalid FILE_TYPE")
    if record["cotiviti.source_format"] != _PAYMENT_SOURCE_FORMAT:
        raise ValueError(f"Generated {profile} payment has an invalid cotiviti.source_format")
    if record["CH_CLAIM_TYPE"] != _PAYMENT_TYPES[profile]:
        raise ValueError(f"Generated {profile} payment has an invalid CH_CLAIM_TYPE")

    details = record["CLAIM_DETAIL"]
    if not isinstance(details, list) or not details:
        raise ValueError(f"Generated {profile} payment must contain CLAIM_DETAIL")
    for line_number, detail in enumerate(details, start=1):
        if not isinstance(detail, Mapping):
            raise ValueError(f"Generated {profile} payment detail {line_number} must be an object")
        unexpected_detail = sorted(set(detail) - set(_PAYMENT_DETAIL_FIELDS))
        absent_detail = [field for field in _PAYMENT_DETAIL_FIELDS if field not in detail]
        optional_empty_detail = {
            *(
                field
                for number in range(1, 7)
                for field in (
                    f"CD_CLAIM_ADJUSTMENT_GROUP_CODE_{number}",
                    f"CD_CLAIM_ADJUSTMENT_REASON_CODE_{number}",
                )
            ),
        }
        if profile == "payment-professional":
            optional_empty_detail.add("CD_SUBMITTED_REVENUE_CODE")
        missing_detail = [
            field
            for field in _PAYMENT_DETAIL_FIELDS
            if field in detail
            and field not in optional_empty_detail
            and not _present(detail[field])
        ]
        if unexpected_detail or absent_detail or missing_detail:
            missing_detail_fields = sorted({*absent_detail, *missing_detail})
            raise ValueError(
                f"Generated {profile} payment detail {line_number} has an invalid field contract; "
                f"unexpected={unexpected_detail}, missing={missing_detail_fields}"
            )
        _validate_adjustments(detail, profile, line_number)
        _validate_detail_financials(detail, profile, line_number)
    _validate_header_financials(record, details, profile)
    rules = PAYMENT_MATCHING_RULES[profile]
    missing_header = [field for field in rules["header"] if field not in record]
    missing_line = [field for field in rules["line"] if field not in details[0]]
    if missing_header or missing_line:
        missing_fields = [*missing_header, *missing_line]
        raise ValueError(
            f"Generated {profile} payment is missing relationship fields: "
            f"{', '.join(missing_fields)}"
        )


def _number(value: object) -> int | float:
    """Return a numeric source value while keeping blanks safe for arithmetic."""
    return value if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _number_or_default(value: object, default: int | float) -> int | float:
    """Preserve numeric zero while defaulting only absent or non-numeric values."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return default


def _decimal_amount(value: object, field: str, profile: str) -> Decimal:
    """Return one finite, non-negative monetary value or reject the record."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError(f"Generated {profile} payment field {field} must be numeric")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"Generated {profile} payment field {field} must be numeric") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Generated {profile} payment field {field} must be non-negative")
    return amount


def _validate_adjustments(detail: Mapping[str, object], profile: str, line_number: int) -> None:
    """Require codes only for adjustment slots that carry a positive amount."""
    for number in range(1, 7):
        amount_field = f"CD_ADJUSTMENT_AMOUNT_{number}"
        group_field = f"CD_CLAIM_ADJUSTMENT_GROUP_CODE_{number}"
        reason_field = f"CD_CLAIM_ADJUSTMENT_REASON_CODE_{number}"
        amount = _decimal_amount(detail[amount_field], amount_field, profile)
        group_present = _present(detail[group_field])
        reason_present = _present(detail[reason_field])
        if amount > 0 and not (group_present and reason_present):
            raise ValueError(
                f"Generated {profile} payment detail {line_number} adjustment {number} "
                "requires group and reason codes"
            )
        if amount == 0 and (group_present or reason_present):
            raise ValueError(
                f"Generated {profile} payment detail {line_number} adjustment {number} "
                "must not carry codes when its amount is zero"
            )


def _validate_detail_financials(
    detail: Mapping[str, object], profile: str, line_number: int
) -> None:
    """Validate line-level 835 payment arithmetic."""
    amounts = {
        field: _decimal_amount(detail[field], field, profile)
        for field in (
            "CD_CHARGE_AMOUNT",
            "CD_ALLOWED_AMOUNT",
            "CD_PAID_AMOUNT",
            "CD_COINSURANCE_AMOUNT",
            "CD_COPAY_AMOUNT",
            "CD_DEDUCTIBLE_AMOUNT",
            "CD_PATIENT_LIABILITY_AMOUNT",
        )
    }
    expected_liability = (
        amounts["CD_COINSURANCE_AMOUNT"]
        + amounts["CD_COPAY_AMOUNT"]
        + amounts["CD_DEDUCTIBLE_AMOUNT"]
    )
    if amounts["CD_PATIENT_LIABILITY_AMOUNT"] != expected_liability:
        raise ValueError(
            f"Generated {profile} payment detail {line_number} patient liability does not reconcile"
        )
    if amounts["CD_ALLOWED_AMOUNT"] != (
        amounts["CD_PAID_AMOUNT"] + amounts["CD_PATIENT_LIABILITY_AMOUNT"]
    ):
        raise ValueError(
            f"Generated {profile} payment detail {line_number} allowed amount does not reconcile"
        )
    adjustments = sum(
        (
            _decimal_amount(
                detail[f"CD_ADJUSTMENT_AMOUNT_{number}"],
                f"CD_ADJUSTMENT_AMOUNT_{number}",
                profile,
            )
            for number in range(1, 7)
        ),
        Decimal(0),
    )
    if amounts["CD_CHARGE_AMOUNT"] != amounts["CD_PAID_AMOUNT"] + adjustments:
        raise ValueError(
            f"Generated {profile} payment detail {line_number} adjustments do not reconcile"
        )


def _validate_header_financials(
    record: Mapping[str, object], details: Sequence[Mapping[str, object]], profile: str
) -> None:
    """Validate Claim Header arithmetic and its totals against Claim Detail."""
    header_to_detail = {
        "CH_CHARGE_AMOUNT": "CD_CHARGE_AMOUNT",
        "CH_ALLOWED_AMOUNT": "CD_ALLOWED_AMOUNT",
        "CH_PAID_AMOUNT": "CD_PAID_AMOUNT",
        "CH_COINSURANCE_AMOUNT": "CD_COINSURANCE_AMOUNT",
        "CH_COPAY_AMOUNT": "CD_COPAY_AMOUNT",
        "CH_DEDUCTIBLE_AMOUNT": "CD_DEDUCTIBLE_AMOUNT",
        "CH_PATIENT_LIABILITY_AMOUNT": "CD_PATIENT_LIABILITY_AMOUNT",
    }
    header_amounts = {
        header: _decimal_amount(record[header], header, profile) for header in header_to_detail
    }
    expected_liability = (
        header_amounts["CH_COINSURANCE_AMOUNT"]
        + header_amounts["CH_COPAY_AMOUNT"]
        + header_amounts["CH_DEDUCTIBLE_AMOUNT"]
    )
    if header_amounts["CH_PATIENT_LIABILITY_AMOUNT"] != expected_liability:
        raise ValueError(f"Generated {profile} payment header patient liability does not reconcile")
    if header_amounts["CH_ALLOWED_AMOUNT"] != (
        header_amounts["CH_PAID_AMOUNT"] + header_amounts["CH_PATIENT_LIABILITY_AMOUNT"]
    ):
        raise ValueError(f"Generated {profile} payment header allowed amount does not reconcile")
    for header, detail_field in header_to_detail.items():
        detail_total = sum(
            (_decimal_amount(detail[detail_field], detail_field, profile) for detail in details),
            Decimal(0),
        )
        if header_amounts[header] != detail_total:
            raise ValueError(
                f"Generated {profile} payment field {header} does not equal its detail total"
            )


def load_claim_records(path: Path) -> list[dict[str, object]]:
    """Load non-empty JSON objects from an existing Claim JSONL file."""
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Could not read source Claims file {path}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid Claim JSON at {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"Claim record at {path}:{line_number} must be a JSON object")
        records.append(value)
    if not records:
        raise ValueError(f"Source Claims file {path} contains no records")
    return records


def derive_payment_from_claim(
    claim: Mapping[str, object],
    profile: str,
    seed: int,
    index: int,
    scenario: str = "MATCHED",
    changed_fields: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Project one immutable Claim into a source-shaped Payment record."""
    try:
        source = _PAYMENT_SOURCES[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported Payment profile {profile!r}") from error
    payment = complete_record(deepcopy(claim), source)
    _set_payment_transport(payment, profile, seed, index)
    _copy_claim_lineage(payment, claim)
    _set_source_payment_defaults(payment, claim, profile, seed, index, changed_fields)
    _apply_scenario(payment, claim, profile, scenario, seed, index)
    projected = project_record(payment, profile)
    _validate_payment_record(projected, profile)
    _validate_source_relationship(projected, claim, profile, scenario)
    return projected


def _set_payment_transport(payment: dict[str, object], profile: str, seed: int, index: int) -> None:
    """Convert Claim transport metadata to the derived Payment 835 stream."""
    payment["FILE_TYPE"] = _PAYMENT_FILE_TYPE
    payment["cotiviti.source_format"] = _PAYMENT_SOURCE_FORMAT
    payment["cotiviti.message_id"] = deterministic_uuid4(seed, f"{profile}:message:{index + 1}")
    payment["cotiviti.message_seq"] = index + 1
    produced_at = datetime(2026, 8, 5) + timedelta(seconds=index)
    payment["cotiviti.produced_at"] = produced_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    payment["x-connector-name"] = "test-eip-835"
    raw_reference = payment.get("cotiviti.source.raw_file_ref")
    if isinstance(raw_reference, str) and raw_reference:
        payment["cotiviti.source.raw_file_ref"] = raw_reference.removesuffix(".x12") + ".835.x12"


def derive_payments_from_claims(
    path: Path,
    profile: str,
    scenario_counts: Mapping[str, int],
    seed: int,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Derive Payments from existing Claims in configured scenario order."""
    claim_backed = any(
        str(name).upper() != "ORPHAN" and int(value) > 0 for name, value in scenario_counts.items()
    )
    claims = load_claim_records(path) if claim_backed else []
    return derive_payments_from_records(claims, profile, scenario_counts, seed, limit)


def derive_payments_from_records(
    claims: Sequence[Mapping[str, object]],
    profile: str,
    scenario_counts: Mapping[str, int],
    seed: int,
    limit: int | None = None,
    claim_changed_fields: Sequence[frozenset[str]] | None = None,
) -> list[dict[str, object]]:
    """Derive Payments from already-materialized Claims in scenario order."""
    count = limit if limit is not None else sum(scenario_counts.values())
    if count < 1:
        return []
    requested = {
        str(name).upper(): int(value) for name, value in scenario_counts.items() if int(value) > 0
    }
    if not requested:
        requested = {"MATCHED": count}
    claim_backed = {
        scenario: scenario_count
        for scenario, scenario_count in requested.items()
        if scenario != "ORPHAN"
    }
    if claim_backed and not claims:
        raise ValueError("Claim-backed Payment scenarios require at least one source Claim")
    if claim_changed_fields is not None and len(claim_changed_fields) != len(claims):
        raise ValueError("Claim update metadata must align with the Claim source records")
    changes_by_record = (
        {id(claim): changed for claim, changed in zip(claims, claim_changed_fields, strict=True)}
        if claim_changed_fields is not None
        else {}
    )
    records: list[dict[str, object]] = []
    prior_payment_claims: list[Mapping[str, object]] = []
    source_index = 0
    for scenario in ("MATCHED", "REPLACEMENT", "STALE"):
        scenario_count = requested.get(scenario, 0)
        if not scenario_count:
            continue
        candidates = claims
        if scenario == "REPLACEMENT":
            candidates = [claim for claim in claims if _is_replacement(claim)]
            if scenario_count and not candidates:
                raise ValueError(
                    "REPLACEMENT Payment scenario requires at least one source Claim "
                    "with CH_CLAIM_FREQUENCY_CODE 7"
                )
        for _ in range(scenario_count):
            claim = candidates[source_index % len(candidates)]
            records.append(
                derive_payment_from_claim(
                    claim,
                    profile,
                    seed,
                    len(records),
                    scenario,
                    changes_by_record.get(id(claim), frozenset()),
                )
            )
            prior_payment_claims.append(claim)
            source_index += 1
    records.extend(
        generate_orphan_payments(profile, requested.get("ORPHAN", 0), seed, len(records))
    )
    reversal_count = requested.get("REVERSAL", 0)
    if reversal_count and not prior_payment_claims:
        raise ValueError(
            "REVERSAL Payment scenario requires an earlier MATCHED, REPLACEMENT, or STALE Payment"
        )
    for reversal_index in range(reversal_count):
        reversal_claim = prior_payment_claims[reversal_index % len(prior_payment_claims)]
        records.append(
            derive_payment_from_claim(
                reversal_claim,
                profile,
                seed,
                len(records),
                "REVERSAL",
                changes_by_record.get(id(reversal_claim), frozenset()),
            )
        )
    if len(records) != count:
        raise ValueError(
            f"Payment source scenario counts produced {len(records)} records; expected {count}"
        )
    return records


def generate_orphan_payments(
    profile: str, count: int, seed: int, start_index: int = 0
) -> list[dict[str, object]]:
    """Create standalone orphan Payments without writing or referencing Claim records.

    A source-shaped temporary value is used only to populate the normal 835
    structure.  Its identifiers are allocated outside the configured Claim
    stream and it is never returned, persisted, or configured as a Claim.
    """
    if count < 0:
        raise ValueError("Orphan Payment count cannot be negative")
    claim_profile = (
        "claim-professional" if profile == "payment-professional" else "claim-institutional"
    )
    return [
        derive_payment_from_claim(
            generate_claim(
                seed,
                _ORPHAN_SOURCE_INDEX_OFFSET + start_index + index,
                {},
                {},
                {},
                claim_profile,
            ),
            profile,
            seed,
            start_index + index,
            "ORPHAN",
        )
        for index in range(count)
    ]


def _copy_claim_lineage(payment: dict[str, object], claim: Mapping[str, object]) -> None:
    """Copy Claim and Original Claim identifiers into declared Payment fields."""
    for field in _CLAIM_LINEAGE_FIELDS:
        if field in claim:
            payment[field] = claim[field]


def _set_source_payment_defaults(
    payment: dict[str, object],
    claim: Mapping[str, object],
    profile: str,
    seed: int,
    index: int,
    changed_fields: frozenset[str] = frozenset(),
) -> None:
    """Populate the exact, non-empty 835 field contract from one Claim."""
    claim_type = _PAYMENT_TYPES[profile]
    prefix = "P" if claim_type == "P" else "I"
    service_from = str(claim.get("CH_CLAIM_SERVICE_FROM_DATE") or "20260101")
    service_to = str(claim.get("CH_CLAIM_SERVICE_TO_DATE") or service_from)
    paid_date = str(
        _first_value(claim, ("CH_CLAIM_PAID_DATE", "CH_CHECK_DATE", "CH_CLAIM_SERVICE_TO_DATE"))
        or service_to
    )
    claim_id = str(claim.get("CH_CLIENT_CLAIM_ID") or f"{prefix}CLM{index + 1:09d}")
    original_claim_id = str(claim.get("CH_CLIENT_ORIGINAL_CLAIM_ID") or claim_id)
    billing_npi = claim.get("CH_BILLING_PROVIDER_NPI") or "1234567893"
    rendering_npi = claim.get("CH_RENDERING_PROVIDER_NPI") or billing_npi
    billing_tax_id = str(claim.get("CH_BILLING_PROVIDER_FEDERAL_TAX_ID") or "521234567")
    source_details = claim.get("CLAIM_DETAIL")
    source_detail = (
        source_details[0]
        if isinstance(source_details, list)
        and source_details
        and isinstance(source_details[0], Mapping)
        else {}
    )
    financials = _reconciled_payment_financials(claim, source_detail, changed_fields)
    charge = financials["charge"]
    allowed = financials["allowed"]
    paid = financials["paid"]
    payment.update(
        {
            "CH_DIAGNOSIS_CODE_01": claim.get("CH_DIAGNOSIS_CODE_01") or "I10",
            "CH_BILLING_PROVIDER_CLAIM_ID": claim.get("CH_BILLING_PROVIDER_CLAIM_ID")
            or f"BPC{index + 1:010d}",
            "CH_PATIENT_CLIENT_ID": claim.get("CH_PATIENT_CLIENT_ID") or f"PT{index + 1:010d}",
            "CH_CREDIT_DEBIT_FLAG_CODE": "C",
            "CH_PAYMENT_METHOD_CODE": "ACH",
            "CH_REFERENCE_IDENTIFICATION_NUMBER": f"{prefix}PAYREF{index + 1:010d}",
            "CH_CLAIM_TYPE": claim_type,
            "CH_CHARGE_AMOUNT": charge,
            "CH_PAID_AMOUNT": paid,
            "CH_CLAIM_FILING_INDICATOR_CODE": claim.get("CH_CLAIM_FILING_INDICATOR_CODE") or "CI",
            "CH_PAYER_ORGANIZATION_NAME": claim.get("CH_PAYER_ORGANIZATION_NAME")
            or "COTIVITI TEST HEALTH PLAN",
            "CH_CLIENT_RECEIVED_DATE": claim.get("CH_CLIENT_RECEIVED_DATE") or service_to,
            "CH_CLIENT_CLAIM_ID": claim_id,
            "CH_CLIENT_ORIGINAL_CLAIM_ID": original_claim_id,
            "CH_PLACE_OF_SERVICE_CODE": claim.get("CH_PLACE_OF_SERVICE_CODE")
            or ("11" if claim_type == "P" else "21"),
            "CH_TYPE_OF_BILL_CODE": claim.get("CH_TYPE_OF_BILL_CODE")
            or ("" if claim_type == "P" else "131"),
            "CH_CLAIM_FREQUENCY_CODE": claim.get("CH_CLAIM_FREQUENCY_CODE") or "1",
            "CH_CLAIM_SERVICE_FROM_DATE": service_from,
            "CH_CLAIM_SERVICE_TO_DATE": service_to,
            "CH_BILLING_PROVIDER_FEDERAL_TAX_ID": billing_tax_id,
            "CH_BILLING_PROVIDER_NPI": billing_npi,
            "CH_BILLING_PROVIDER_FULL_NAME": claim.get("CH_BILLING_PROVIDER_FULL_NAME")
            or "TAYLOR JORDAN MEDICAL GROUP",
            "CH_RENDERING_PROVIDER_FEDERAL_TAX_ID": claim.get(
                "CH_RENDERING_PROVIDER_FEDERAL_TAX_ID"
            )
            or billing_tax_id,
            "CH_RENDERING_PROVIDER_NPI": rendering_npi,
            "CH_PATIENT_ACCOUNT_CONTROL_NUMBER": claim.get("CH_PATIENT_ACCOUNT_CONTROL_NUMBER")
            or f"PAC{index + 1:010d}",
            "CH_PATIENT_MEDICAL_RECORD_NUMBER": claim.get("CH_PATIENT_MEDICAL_RECORD_NUMBER")
            or f"MRN{index + 1:010d}",
            "CH_SUBSCRIBER_CLIENT_ID": claim.get("CH_SUBSCRIBER_CLIENT_ID")
            or f"SUB{index + 1:010d}",
            "CH_CLAIM_PAID_DATE": paid_date,
            "CH_ALLOWED_AMOUNT": allowed,
            "CH_COINSURANCE_AMOUNT": financials["coinsurance"],
            "CH_COPAY_AMOUNT": financials["copay"],
            "CH_DEDUCTIBLE_AMOUNT": financials["deductible"],
            "CH_PATIENT_LIABILITY_AMOUNT": financials["liability"],
            "CH_CLAIM_STATUS_CODE": "1",
            "CH_PAYER_ID": "CHC",
            "CH_PAYEE_TAX_ID": billing_tax_id,
            "CH_PAYEE_ID": str(billing_npi),
            "CH_PAYEE_ID_QUALIFIER": "XX",
            "cotiviti.dataset_id": "claims_payment",
            "cotiviti.tenant_id": claim.get("cotiviti.tenant_id") or "test-health",
            "cotiviti.schema_version": claim.get("cotiviti.schema_version") or "gdf-eip-v1",
            "cotiviti.client_id": claim.get("cotiviti.client_id") or "test.health.payer",
            "cotiviti.client_system": claim.get("cotiviti.client_system") or "test.health.payer",
            "cotiviti.source_system": claim.get("cotiviti.source_system") or "PPC",
            "cotiviti.batch_id": f"test-{profile}-{seed}-{index + 1:06d}",
            "cotiviti.correlation_id": deterministic_uuid4(
                seed, f"{profile}:correlation:{index + 1}"
            ),
            "cotiviti.producer_version": "test-data-generator/1",
            "cotiviti.source.isa_control": claim.get("cotiviti.source.isa_control")
            or f"{seed % 1_000_000_000:09d}",
            "cotiviti.source.gs_control": claim.get("cotiviti.source.gs_control") or index + 1,
            "cotiviti.source.raw_file_ref": f"{profile}-{seed}-{index + 1:06d}.835.x12",
            "INGESTION_EPOCH": claim.get("INGESTION_EPOCH") or 1785888000 + index + 1,
            "ROWID": deterministic_uuid4(seed, f"{profile}:row:{index + 1}"),
            "PAYER": claim.get("PAYER") or "CHC",
            "PRODUCT": claim.get("PRODUCT") or "PPC",
            "GDF_VERSION": claim.get("GDF_VERSION") or "v2.9",
            "DATA_CATEGORY": "CLAIMS_PAYMENT",
            "LOB": claim.get("LOB") or "COMMERCIAL",
            "INGESTION_DATE": claim.get("INGESTION_DATE") or current_ingestion_date(),
            "PUBLISHER_NAME": claim.get("PUBLISHER_NAME") or "test-data-generator",
        }
    )
    line_charge = charge
    line_allowed = allowed
    line_paid = paid
    adjustments = [
        ("CO", "45", max(line_charge - line_allowed, 0)),
        ("PR", "1", financials["deductible"]),
        ("PR", "2", financials["coinsurance"]),
        ("PR", "3", financials["copay"]),
    ]
    adjustments = [adjustment for adjustment in adjustments if adjustment[2] > 0]
    adjustments.extend(("", "", 0) for _ in range(6 - len(adjustments)))
    detail: dict[str, object] = {
        "CD_CHARGE_AMOUNT": line_charge,
        "CD_PAID_AMOUNT": line_paid,
        "CD_REMITTANCE_ADVICE_GROUP_CODE": "CO",
        "CD_REMITTANCE_ADVICE_REASON_CODE": "45",
        "CD_SERVICE_FROM_DATE": source_detail.get("CD_SERVICE_FROM_DATE") or service_from,
        "CD_SERVICE_TO_DATE": source_detail.get("CD_SERVICE_TO_DATE") or service_to,
        "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER": source_detail.get(
            "CD_SUBMITTED_PROCEDURE_CODE_QUALIFIER"
        )
        or "HCPCS",
        "CD_SUBMITTED_PROCEDURE_CODE": source_detail.get("CD_SUBMITTED_PROCEDURE_CODE")
        or ("99213" if claim_type == "P" else "99223"),
        "CD_SUBMITTED_PROCEDURE_MODIFIER_01": source_detail.get(
            "CD_SUBMITTED_PROCEDURE_MODIFIER_01"
        )
        or "25",
        "CD_SUBMITTED_PROCEDURE_MODIFIER_02": source_detail.get(
            "CD_SUBMITTED_PROCEDURE_MODIFIER_02"
        )
        or "59",
        "CD_SUBMITTED_PROCEDURE_MODIFIER_03": source_detail.get(
            "CD_SUBMITTED_PROCEDURE_MODIFIER_03"
        )
        or "LT",
        "CD_SUBMITTED_PROCEDURE_MODIFIER_04": source_detail.get(
            "CD_SUBMITTED_PROCEDURE_MODIFIER_04"
        )
        or "KX",
        "CD_SUBMITTED_REVENUE_CODE": source_detail.get("CD_SUBMITTED_REVENUE_CODE")
        or ("" if claim_type == "P" else "0510"),
        "CD_LINE_PAID_DATE": paid_date,
        "CD_ALLOWED_AMOUNT": line_allowed,
        "CD_COINSURANCE_AMOUNT": financials["coinsurance"],
        "CD_COPAY_AMOUNT": financials["copay"],
        "CD_DEDUCTIBLE_AMOUNT": financials["deductible"],
        "CD_PATIENT_LIABILITY_AMOUNT": financials["liability"],
        "CD_ALLOWED_PROCEDURE_CODE": source_detail.get("CD_ALLOWED_PROCEDURE_CODE")
        or source_detail.get("CD_SUBMITTED_PROCEDURE_CODE")
        or ("99213" if claim_type == "P" else "99223"),
    }
    for number, (group, reason, amount) in enumerate(adjustments, start=1):
        detail[f"CD_CLAIM_ADJUSTMENT_GROUP_CODE_{number}"] = group
        detail[f"CD_CLAIM_ADJUSTMENT_REASON_CODE_{number}"] = reason
        detail[f"CD_ADJUSTMENT_AMOUNT_{number}"] = amount
    payment["CLAIM_DETAIL"] = [detail]


def _reconciled_payment_financials(
    claim: Mapping[str, object],
    detail: Mapping[str, object],
    changed_fields: frozenset[str],
) -> dict[str, int | float]:
    """Project Claim financial changes into a valid, balanced 835 transaction.

    A Claim update is allowed to target an individual financial value.  An 835
    Payment has stricter arithmetic, so it retains the changed source value
    when possible and recalculates its dependent payment-only amounts.
    """
    charge = _number_or_default(
        claim.get("CH_CHARGE_AMOUNT"),
        _number_or_default(detail.get("CD_CHARGE_AMOUNT"), 1000),
    )
    allowed = _number_or_default(
        claim.get("CH_ALLOWED_AMOUNT"),
        _number_or_default(detail.get("CD_ALLOWED_AMOUNT"), round(charge * 0.75)),
    )
    paid = _number_or_default(
        claim.get("CH_PAID_AMOUNT"),
        _number_or_default(detail.get("CD_PAID_AMOUNT"), round(allowed * 0.70)),
    )
    coinsurance = _number_or_default(
        claim.get("CH_COINSURANCE_AMOUNT"), _number(detail.get("CD_COINSURANCE_AMOUNT"))
    )
    copay = _number_or_default(claim.get("CH_COPAY_AMOUNT"), _number(detail.get("CD_COPAY_AMOUNT")))
    deductible = _number_or_default(
        claim.get("CH_DEDUCTIBLE_AMOUNT"), _number(detail.get("CD_DEDUCTIBLE_AMOUNT"))
    )
    liability = _number_or_default(
        claim.get("CH_PATIENT_LIABILITY_AMOUNT"),
        _number_or_default(
            detail.get("CD_PATIENT_LIABILITY_AMOUNT"), coinsurance + copay + deductible
        ),
    )
    total_changed = bool(
        changed_fields.intersection({"CH_PATIENT_LIABILITY_AMOUNT", "CD_PATIENT_LIABILITY_AMOUNT"})
    )
    components_changed = bool(
        changed_fields.intersection(
            {
                "CH_COINSURANCE_AMOUNT",
                "CD_COINSURANCE_AMOUNT",
                "CH_COPAY_AMOUNT",
                "CD_COPAY_AMOUNT",
                "CH_DEDUCTIBLE_AMOUNT",
                "CD_DEDUCTIBLE_AMOUNT",
            }
        )
    )
    if total_changed or (not components_changed and liability != coinsurance + copay + deductible):
        copay, deductible, coinsurance = _allocate_liability(liability, copay, deductible)
    elif components_changed:
        liability = coinsurance + copay + deductible

    charge = max(charge, 0)
    liability = min(max(liability, 0), charge)
    copay, deductible, coinsurance = _allocate_liability(liability, copay, deductible)
    paid_changed = bool(changed_fields.intersection({"CH_PAID_AMOUNT", "CD_PAID_AMOUNT"}))
    allowed_changed = bool(changed_fields.intersection({"CH_ALLOWED_AMOUNT", "CD_ALLOWED_AMOUNT"}))
    if paid_changed and not allowed_changed:
        allowed = paid + liability
    allowed = min(max(allowed, liability), charge)
    paid = allowed - liability
    return {
        "charge": charge,
        "allowed": allowed,
        "paid": paid,
        "coinsurance": coinsurance,
        "copay": copay,
        "deductible": deductible,
        "liability": liability,
    }


def _allocate_liability(
    liability: int | float, copay: int | float, deductible: int | float
) -> tuple[int | float, int | float, int | float]:
    """Allocate total patient liability without producing negative components."""
    remaining = max(liability, 0)
    allocated_copay = min(max(copay, 0), remaining)
    remaining -= allocated_copay
    allocated_deductible = min(max(deductible, 0), remaining)
    remaining -= allocated_deductible
    return allocated_copay, allocated_deductible, remaining


def _apply_scenario(
    payment: dict[str, object],
    claim: Mapping[str, object],
    profile: str,
    scenario: str,
    seed: int,
    index: int,
) -> None:
    """Apply only Payment-side scenario changes after Claim values are copied."""
    normalized = scenario.upper()
    if normalized in {"MATCHED", "REPLACEMENT"}:
        return
    if normalized == "REVERSAL":
        payment["CH_CLAIM_STATUS_CODE"] = "22"
        payment["CH_CREDIT_DEBIT_FLAG_CODE"] = "D"
        return
    if normalized == "STALE":
        paid_date = _first_value(
            claim,
            ("CH_CLAIM_PAID_DATE", "CH_CHECK_DATE", "CH_CLAIM_SERVICE_TO_DATE"),
        )
        if not isinstance(paid_date, str):
            raise ValueError("STALE Payment scenario requires a parseable Claim payment date")
        stale_date = _older_date(paid_date, index)
        if "CH_CLAIM_PAID_DATE" in payment:
            payment["CH_CLAIM_PAID_DATE"] = stale_date
        details = payment.get("CLAIM_DETAIL")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict) and "CD_LINE_PAID_DATE" in detail:
                    detail["CD_LINE_PAID_DATE"] = stale_date
        return
    if normalized == "ORPHAN":
        _make_orphan(payment, profile, seed, index)
        return
    raise ValueError(f"Unsupported source Payment scenario {scenario!r}")


def _make_orphan(payment: dict[str, object], profile: str, seed: int, index: int) -> None:
    """Ensure a standalone Payment cannot match its transient source shape."""
    del seed, index
    preferred = "CH_PATIENT_ACCOUNT_CONTROL_NUMBER"
    candidates = (preferred,) + tuple(
        field
        for field in PAYMENT_MATCHING_RULES[profile]["header"]
        if field not in _PATIENT_RELATIONSHIP_FIELDS and field != preferred
    )
    for field in candidates:
        if field in payment and _present(payment[field]):
            payment[field] = _nonmatching_value(payment[field])
            return
    else:
        raise ValueError(f"Could not create an orphan Payment for layout {profile!r}")


def _nonmatching_value(value: object) -> object:
    """Return a normal-looking value distinct from the supplied source value."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if not isinstance(value, str):
        return value
    if not value:
        return "1"
    final = value[-1]
    if final.isdigit():
        return value[:-1] + str((int(final) + 1) % 10)
    if final.isupper():
        return value[:-1] + chr((ord(final) - ord("A") + 1) % 26 + ord("A"))
    if final.islower():
        return value[:-1] + chr((ord(final) - ord("a") + 1) % 26 + ord("a"))
    return value + "1"


def _is_replacement(claim: Mapping[str, object]) -> bool:
    """Identify corrected source Claims without changing their source values."""
    return str(claim.get("CH_CLAIM_FREQUENCY_CODE", "")) == "7"


def _older_date(value: str, index: int) -> str:
    """Move a YYYYMMDD or ISO date backwards by at least one day."""
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        return (parsed - timedelta(days=index + 1)).strftime(fmt)
    raise ValueError(f"Unsupported Claim payment date format {value!r}")


def _first_value(record: Mapping[str, object], fields: tuple[str, ...]) -> object | None:
    """Return the first present, non-empty source value."""
    for field in fields:
        value = record.get(field)
        if value not in (None, ""):
            return value
    return None


def _present(value: object) -> bool:
    """Treat zero as a valid populated amount while excluding empty values."""
    return value is not None and value != ""


def _validate_source_relationship(
    payment: Mapping[str, object],
    claim: Mapping[str, object],
    profile: str,
    scenario: str,
) -> None:
    """Verify copied Claim relationships before a derived Payment is emitted."""
    normalized = scenario.upper()
    if normalized == "ORPHAN":
        if _matches_claim(payment, claim, profile):
            raise ValueError("ORPHAN Payment still matches its source Claim")
        return
    expected_claim_id = _first_value(claim, _CLAIM_IDS)
    if expected_claim_id is not None and not _same_logical_value(
        payment.get("CH_CLIENT_CLAIM_ID"), expected_claim_id
    ):
        raise ValueError("Payment Claim ID does not match the source Claim")
    expected_original_id = _first_value(claim, _ORIGINAL_CLAIM_IDS)
    if expected_original_id is not None and not _same_logical_value(
        payment.get("CH_CLIENT_ORIGINAL_CLAIM_ID"), expected_original_id
    ):
        raise ValueError("Payment Original Claim ID does not match the source Claim")
    for field in (*_CLAIM_LINEAGE_FIELDS, *_PATIENT_RELATIONSHIP_FIELDS):
        if (
            field in claim
            and field in payment
            and not _same_logical_value(claim[field], payment[field])
        ):
            raise ValueError(f"Payment relationship field {field!r} differs from the source Claim")
    rules = PAYMENT_MATCHING_RULES[profile]
    for field in rules["header"]:
        if (
            field in claim
            and field in payment
            and not _same_logical_value(claim[field], payment[field])
        ):
            raise ValueError(f"Payment relationship field {field!r} differs from the source Claim")
    claim_details = claim.get("CLAIM_DETAIL")
    payment_details = payment.get("CLAIM_DETAIL")
    if not isinstance(claim_details, list) or not isinstance(payment_details, list):
        return
    if len(claim_details) != len(payment_details):
        raise ValueError("Payment line count differs from the source Claim")
    for claim_detail, payment_detail in zip(claim_details, payment_details, strict=True):
        if not isinstance(claim_detail, Mapping) or not isinstance(payment_detail, Mapping):
            continue
        for field in rules["line"]:
            if field in claim_detail and field in payment_detail:
                if not _same_logical_value(claim_detail[field], payment_detail[field]):
                    raise ValueError(
                        f"Payment line relationship field {field!r} differs from the source Claim"
                    )


def _same_logical_value(left: object, right: object) -> bool:
    """Compare relationship values after layout-specific JSON type coercion."""
    if left is None or right is None:
        return left is right
    left_text = str(left)
    right_text = str(right)
    if left_text == right_text:
        return True
    try:
        return Decimal(left_text) == Decimal(right_text)
    except InvalidOperation:
        return False


def _matches_claim(
    payment: Mapping[str, object], claim: Mapping[str, object], profile: str
) -> bool:
    """Return whether the Payment retains all populated header match keys."""
    return all(
        field not in claim or field not in payment or claim[field] == payment[field]
        for field in PAYMENT_MATCHING_RULES[profile]["header"]
    )
