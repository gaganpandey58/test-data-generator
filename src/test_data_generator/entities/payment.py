"""Generate standalone Payment I/P records linked to deterministic claims."""

from collections.abc import Mapping

from test_data_generator.entities.claim import generate_record as generate_claim
from test_data_generator.layouts import project_record
from test_data_generator.samples.shapes import complete_record


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
    projected = project_record(payment, profile)
    _set_payment_dates(projected)
    return projected


def _set_payment_dates(record: dict[str, object]) -> None:
    """Populate documented payment match dates when those fields are present."""
    paid_date = record.get("CH_CHECK_DATE", record.get("CH_CLAIM_SERVICE_TO_DATE", ""))
    if "CH_CLAIM_PAID_DATE" in record:
        record["CH_CLAIM_PAID_DATE"] = paid_date
    details = record.get("CLAIM_DETAIL")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and "CD_LINE_PAID_DATE" in detail:
                detail["CD_LINE_PAID_DATE"] = paid_date
