"""Generate complete standalone Payment I/P records linked to claims.

The claim builder owns the shared healthcare identity and claim-line values.
This module owns the Payment envelope contract, payment-specific amount
reconciliation, Payment I/P distinctions, and the source-defined relationship
fields that must remain aligned with the originating claim.
"""

from collections.abc import Mapping

from test_data_generator.entities.claim import generate_record as generate_claim
from test_data_generator.layouts import project_record
from test_data_generator.samples.shapes import complete_record
from test_data_generator.update.payment_relationships import PAYMENT_MATCHING_RULES

_PAYMENT_TYPES = {
    "payment-professional": "P",
    "payment-institutional": "O",
}
_PAYMENT_FILE_TYPES = {
    "payment-professional": "837P",
    "payment-institutional": "837I",
}
_REQUIRED_FIELDS = (
    "FILE_TYPE",
    "CH_CLAIM_TYPE",
    "CH_PATIENT_CLIENT_ID",
    "CH_BILLING_PROVIDER_NPI",
    "CH_CLAIM_SERVICE_FROM_DATE",
    "CH_CLAIM_SERVICE_TO_DATE",
    "CH_CHARGE_AMOUNT",
    "CLAIM_DETAIL",
    "INGESTION_DATE",
    "INGESTION_EPOCH",
)


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
    source = (
        "payment_professional" if profile == "payment-professional" else "payment_institutional"
    )
    claim = generate_claim(
        seed,
        index,
        entity_counts,
        client_headers,
        client_values,
        claim_profile,
    )
    payment = complete_record(claim, source)
    _apply_payment_values(payment, profile)
    projected = project_record(payment, profile)
    _set_payment_dates(projected)
    _validate_payment_record(projected, profile)
    return projected


def _apply_payment_values(record: dict[str, object], profile: str) -> None:
    """Populate Payment-specific financial values after claim generation.

    Claim generation establishes the common charge, allowed, liability, and
    paid amounts. Payment I also carries the source-defined reconciliation
    fields that are not meaningful in the professional shape; those values
    are derived here so they cannot drift from the base claim amounts.
    """
    if profile != "payment-institutional":
        return
    charge = _number(record.get("CH_CHARGE_AMOUNT"))
    allowed = _number(record.get("CH_ALLOWED_AMOUNT"))
    disallowed = max(charge - allowed, 0)
    for field, value in {
        "CH_PATIENT_RESPONSIBILITY_AMOUNT": _number(record.get("CH_PATIENT_LIABILITY_AMOUNT")),
        "CH_DENIED_AMOUNT": 0,
        "CH_DISALLOWED_AMOUNT": disallowed,
        "CH_NON_COVERED_AMOUNT": 0,
        "CH_CONTRACT_AMOUNT": allowed,
        "CH_PRIOR_PAYMENT_AMOUNT": 0,
    }.items():
        if field in record:
            record[field] = value

    details = record.get("CLAIM_DETAIL")
    if not isinstance(details, list):
        return
    for detail in details:
        if not isinstance(detail, dict):
            continue
        line_charge = _number(detail.get("CD_CHARGE_AMOUNT"))
        line_allowed = _number(detail.get("CD_ALLOWED_AMOUNT"))
        line_disallowed = max(line_charge - line_allowed, 0)
        for field, value in {
            "CD_DENIED_AMOUNT": 0,
            "CD_DISALLOWED_AMOUNT": line_disallowed,
            "CD_NON_COVERED_AMOUNT": 0,
            "CD_DISCOUNT_AMOUNT": line_disallowed,
            "CD_OTHER_REDUCTION_AMOUNT": 0,
        }.items():
            if field in detail:
                detail[field] = value


def _set_payment_dates(record: dict[str, object]) -> None:
    """Populate the source-defined payment dates without creating fields."""
    paid_date = record.get("CH_CHECK_DATE") or record.get("CH_CLAIM_SERVICE_TO_DATE", "")
    if "CH_CLAIM_PAID_DATE" in record and not record["CH_CLAIM_PAID_DATE"]:
        record["CH_CLAIM_PAID_DATE"] = paid_date
    details = record.get("CLAIM_DETAIL")
    if isinstance(details, list):
        for detail in details:
            if (
                isinstance(detail, dict)
                and "CD_LINE_PAID_DATE" in detail
                and not detail["CD_LINE_PAID_DATE"]
            ):
                detail["CD_LINE_PAID_DATE"] = paid_date


def _validate_payment_record(record: Mapping[str, object], profile: str) -> None:
    """Validate the generated Payment envelope and relationship shape."""
    missing = [
        field for field in _REQUIRED_FIELDS if field not in record or record[field] in (None, "")
    ]
    if missing:
        raise ValueError(f"Generated {profile} payment is missing fields: {', '.join(missing)}")
    if record["FILE_TYPE"] != _PAYMENT_FILE_TYPES[profile]:
        raise ValueError(f"Generated {profile} payment has an invalid FILE_TYPE")
    if record["CH_CLAIM_TYPE"] != _PAYMENT_TYPES[profile]:
        raise ValueError(f"Generated {profile} payment has an invalid CH_CLAIM_TYPE")

    details = record["CLAIM_DETAIL"]
    if not isinstance(details, list) or not details:
        raise ValueError(f"Generated {profile} payment must contain CLAIM_DETAIL")
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
