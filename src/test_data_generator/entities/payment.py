"""Generate complete Payment I/P records linked to Claims.

The claim builder owns the shared healthcare identity and claim-line values.
This module owns the Payment envelope contract, payment-specific amount
reconciliation, Payment I/P distinctions, and the source-defined relationship
fields that must remain aligned with the originating Claim. It also derives
Payments from immutable existing Claim JSONL files for configured scenarios.
"""

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from test_data_generator.entities.claim import generate_record as generate_claim
from test_data_generator.layouts import load_layout, project_record
from test_data_generator.samples.shapes import complete_record
from test_data_generator.update.payment_relationships import PAYMENT_MATCHING_RULES

_PAYMENT_TYPES = {
    "payment-professional": "P",
    "payment-institutional": "I",
}
_PAYMENT_FILE_TYPES = {
    "payment-professional": "835P",
    "payment-institutional": "835I",
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
_PAYMENT_SOURCES = {
    "payment-professional": "payment_professional",
    "payment-institutional": "payment_institutional",
}
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
) -> dict[str, object]:
    """Project one immutable Claim into a source-shaped Payment record."""
    try:
        source = _PAYMENT_SOURCES[profile]
    except KeyError as error:
        raise ValueError(f"Unsupported Payment profile {profile!r}") from error
    payment = complete_record(deepcopy(claim), source)
    _set_payment_transport(payment, profile)
    _copy_line_indexes(payment, profile)
    _copy_claim_lineage(payment, claim)
    _set_source_payment_defaults(payment, claim, profile)
    _apply_payment_values(payment, profile)
    _apply_scenario(payment, claim, profile, scenario, seed, index)
    projected = project_record(payment, profile)
    _validate_payment_record(projected, profile)
    _validate_source_relationship(projected, claim, profile, scenario)
    return projected


def _set_payment_transport(payment: dict[str, object], profile: str) -> None:
    """Convert Claim transport metadata to the derived Payment 835 stream."""
    file_type = "835P" if profile == "payment-professional" else "835I"
    payment["FILE_TYPE"] = file_type
    payment["cotiviti.source_format"] = f"edi_x12_{file_type}"
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
    claims = load_claim_records(path)
    count = limit if limit is not None else sum(scenario_counts.values())
    if count < 1:
        return []
    requested = {
        str(name).upper(): int(value) for name, value in scenario_counts.items() if int(value) > 0
    }
    if not requested:
        requested = {"MATCHED": count}
    records: list[dict[str, object]] = []
    prior_payment_claims: list[Mapping[str, object]] = []
    source_index = 0
    for scenario in ("MATCHED", "REPLACEMENT", "STALE", "ORPHAN"):
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
            records.append(derive_payment_from_claim(claim, profile, seed, len(records), scenario))
            if scenario != "ORPHAN":
                prior_payment_claims.append(claim)
            source_index += 1
    reversal_count = requested.get("REVERSAL", 0)
    if reversal_count and not prior_payment_claims:
        raise ValueError(
            "REVERSAL Payment scenario requires an earlier MATCHED, REPLACEMENT, or STALE Payment"
        )
    for reversal_index in range(reversal_count):
        reversal_claim = prior_payment_claims[reversal_index % len(prior_payment_claims)]
        records.append(
            derive_payment_from_claim(reversal_claim, profile, seed, len(records), "REVERSAL")
        )
    if len(records) != count:
        raise ValueError(
            f"Payment source scenario counts produced {len(records)} records; expected {count}"
        )
    return records


def _copy_claim_lineage(payment: dict[str, object], claim: Mapping[str, object]) -> None:
    """Copy Claim and Original Claim identifiers into declared Payment fields."""
    for field in _CLAIM_LINEAGE_FIELDS:
        if field in claim:
            payment[field] = claim[field]


def _copy_line_indexes(payment: dict[str, object], profile: str) -> None:
    """Add stable one-based line indexes when the selected layout declares them."""
    layout = load_layout(profile)
    line_fields = {field.name for field in layout.groups.get("CLAIM_DETAIL", ())}
    if "INDEX" not in line_fields:
        return
    details = payment.get("CLAIM_DETAIL")
    if not isinstance(details, list):
        return
    for index, detail in enumerate(details, start=1):
        if isinstance(detail, dict):
            detail["INDEX"] = index


def _set_source_payment_defaults(
    payment: dict[str, object], claim: Mapping[str, object], profile: str
) -> None:
    """Fill Payment-only fields without changing copied Claim values."""
    if "CLP02" not in payment and any(field.name == "CLP02" for field in load_layout(profile).root):
        payment["CLP02"] = "1"
    paid_date = _first_value(
        claim,
        ("CH_CLAIM_PAID_DATE", "CH_CHECK_DATE", "CH_CLAIM_SERVICE_TO_DATE"),
    )
    if paid_date is not None and "CH_CLAIM_PAID_DATE" in payment:
        payment["CH_CLAIM_PAID_DATE"] = paid_date
    if "CLP02" in payment and not payment["CLP02"]:
        payment["CLP02"] = "1"
    if "CH_PAYMENT_STATUS" in payment and not payment["CH_PAYMENT_STATUS"]:
        payment["CH_PAYMENT_STATUS"] = "PAID"
    details = payment.get("CLAIM_DETAIL")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and "CD_LINE_PAID_DATE" in detail and paid_date is not None:
                detail["CD_LINE_PAID_DATE"] = paid_date
    if profile == "payment-institutional":
        _set_source_institutional_amount_defaults(payment)


def _set_source_institutional_amount_defaults(payment: dict[str, object]) -> None:
    """Derive only missing institutional Payment amounts from Claim amounts."""
    charge = _number(payment.get("CH_CHARGE_AMOUNT"))
    allowed = _number(payment.get("CH_ALLOWED_AMOUNT"))
    if not _present(payment.get("CH_ALLOWED_AMOUNT")):
        allowed = round(charge * 0.75)
    disallowed = max(charge - allowed, 0)
    values = {
        "CH_ALLOWED_AMOUNT": allowed,
        "CH_PATIENT_RESPONSIBILITY_AMOUNT": _number(payment.get("CH_PATIENT_LIABILITY_AMOUNT")),
        "CH_DENIED_AMOUNT": 0,
        "CH_DISALLOWED_AMOUNT": disallowed,
        "CH_NON_COVERED_AMOUNT": 0,
        "CH_CONTRACT_AMOUNT": allowed,
        "CH_PRIOR_PAYMENT_AMOUNT": 0,
    }
    for field, value in values.items():
        if field in payment and not _present(payment[field]):
            payment[field] = value
    details = payment.get("CLAIM_DETAIL")
    if not isinstance(details, list):
        return
    for detail in details:
        if not isinstance(detail, dict):
            continue
        line_charge = _number(detail.get("CD_CHARGE_AMOUNT"))
        line_allowed = _number(detail.get("CD_ALLOWED_AMOUNT"))
        if not _present(detail.get("CD_ALLOWED_AMOUNT")):
            line_allowed = round(line_charge * 0.75)
        line_disallowed = max(line_charge - line_allowed, 0)
        values = {
            "CD_ALLOWED_AMOUNT": line_allowed,
            "CD_DENIED_AMOUNT": 0,
            "CD_DISALLOWED_AMOUNT": line_disallowed,
            "CD_NON_COVERED_AMOUNT": 0,
            "CD_DISCOUNT_AMOUNT": line_disallowed,
            "CD_OTHER_REDUCTION_AMOUNT": 0,
        }
        for field, value in values.items():
            if field in detail and not _present(detail[field]):
                detail[field] = value


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
        if "CLP02" not in payment:
            raise ValueError(f"Payment layout {profile!r} does not declare CLP02")
        payment["CLP02"] = "22"
        if "CH_PAYMENT_STATUS" in payment:
            payment["CH_PAYMENT_STATUS"] = "REVERSED"
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
    """Break populated declared matching keys while preserving their types."""
    token = f"ORPHAN-{seed}-{index:06d}"
    payment["CH_CLIENT_CLAIM_ID"] = token
    for field, suffix in {
        "CH_CLIENT_ORIGINAL_CLAIM_ID": "ORIGINAL",
        "CH_CLIENT_ROOT_CLAIM_ID": "ROOT",
        "CH_CLIENT_CLAIM_UNIQUE_ID": "UNIQUE",
    }.items():
        if field in payment:
            payment[field] = f"{token}-{suffix}"
    if "CH_CLIENT_CLAIM_VERSION_NUMBER" in payment:
        payment["CH_CLIENT_CLAIM_VERSION_NUMBER"] = "0"
    changed = False
    for field in PAYMENT_MATCHING_RULES[profile]["header"]:
        if field in _PATIENT_RELATIONSHIP_FIELDS:
            continue
        if field not in payment or not _present(payment[field]):
            continue
        payment[field] = _orphan_value(payment[field], token)
        changed = True
    if not changed:
        raise ValueError(f"Could not create an orphan Payment for layout {profile!r}")


def _orphan_value(value: object, token: str) -> object:
    """Return a deterministic non-matching value with the source JSON type."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    return f"{value}-{token}"


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
    if expected_claim_id is not None and payment.get("CH_CLIENT_CLAIM_ID") != expected_claim_id:
        raise ValueError("Payment Claim ID does not match the source Claim")
    expected_original_id = _first_value(claim, _ORIGINAL_CLAIM_IDS)
    if (
        expected_original_id is not None
        and payment.get("CH_CLIENT_ORIGINAL_CLAIM_ID") != expected_original_id
    ):
        raise ValueError("Payment Original Claim ID does not match the source Claim")
    for field in (*_CLAIM_LINEAGE_FIELDS, *_PATIENT_RELATIONSHIP_FIELDS):
        if field in claim and field in payment and claim[field] != payment[field]:
            raise ValueError(f"Payment relationship field {field!r} differs from the source Claim")
    rules = PAYMENT_MATCHING_RULES[profile]
    for field in rules["header"]:
        if field in claim and field in payment and claim[field] != payment[field]:
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
                if claim_detail[field] != payment_detail[field]:
                    raise ValueError(
                        f"Payment line relationship field {field!r} differs from the source Claim"
                    )


def _matches_claim(
    payment: Mapping[str, object], claim: Mapping[str, object], profile: str
) -> bool:
    """Return whether the Payment retains all populated header match keys."""
    return all(
        field not in claim or field not in payment or claim[field] == payment[field]
        for field in PAYMENT_MATCHING_RULES[profile]["header"]
    )
