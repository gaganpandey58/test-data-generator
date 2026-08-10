"""Evaluate match tiers and survivorship actions for generated source records.

The evaluator encodes the member, provider, and claim composite matching rules
from the supplied survivorship documentation.  It is an internal quality aid:
the generator does not emit decisions or scenario markers, but generators and
maintainers can use this module to confirm that a source-shaped variation
would receive the intended create, update, ignore, or payment-link outcome.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from healthcare_test_data.scenarios import Scenario


class ExpectedAction(StrEnum):
    """Enumerate actions a receiving system can take for an incoming record.

    Values model the source-document outcomes: create a separate record,
    update a matched record, retain both records, ignore an older or voided
    input, or link an incoming transaction to an unmatched payment.
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    KEEP_BOTH = "KEEP_BOTH"
    IGNORE = "IGNORE"
    LINK_PAYMENT = "LINK_PAYMENT"


@dataclass(frozen=True)
class ExpectedDecision:
    """Capture the action selected for an incoming record and its match tier.

    Attributes:
        action: Survivorship action selected after matching and recency checks.
        match_tier: One-based source-rule tier that matched, or ``None`` when
            no supported identity or claim composite matched.
    """

    action: ExpectedAction
    match_tier: int | None


def evaluate(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
    scenario: Scenario,
) -> ExpectedDecision:
    """Determine an action by matching first and applying the recency gate.

    Matching establishes whether the incoming row represents an existing
    entity or claim.  If it does, source timestamps prevent stale data from
    overwriting newer data before the scenario-specific void and payment rules
    are applied.

    Args:
        existing: Existing source-shaped member, provider, or claim record.
        incoming: New source-shaped record to evaluate against ``existing``.
        scenario: Internal scenario that describes the incoming variation.

    Returns:
        The expected survivorship action and the matching tier, if any.
    """
    tier = _match_tier(existing, incoming)
    if tier is None:
        return ExpectedDecision(ExpectedAction.CREATE, None)
    if not _is_newer(existing, incoming):
        return ExpectedDecision(ExpectedAction.IGNORE, tier)
    if scenario.name == "void":
        return ExpectedDecision(ExpectedAction.IGNORE, tier)
    if _is_orphan_payment(existing) and _is_claim_transaction(incoming):
        return ExpectedDecision(ExpectedAction.LINK_PAYMENT, tier)
    if _is_verified(existing) and _is_provisional(incoming):
        return ExpectedDecision(ExpectedAction.UPDATE, tier)
    return ExpectedDecision(ExpectedAction.UPDATE, tier)


def _match_tier(existing: Mapping[str, object], incoming: Mapping[str, object]) -> int | None:
    """Return the first documented member/provider or claim tier that matches.

    Args:
        existing: Existing source-shaped record.
        incoming: Incoming source-shaped record.

    Returns:
        One-based identity tier for member/provider matching, a profile-local
        claim tier, or ``None`` when no supported keys match.
    """
    for tier, keys in enumerate(_MATCH_KEY_TIERS, start=1):
        if _matches_all(existing, incoming, keys):
            return tier
    return _claim_match_tier(existing, incoming)


_MATCH_KEY_TIERS = (
    ("CM_MEMBER_CLIENT_ID", "CM_PAYER_SHORT"),
    ("CM_MEMBER_CLIENT_ID", "CM_MEMBER_BIRTH_DATE", "CM_MEMBER_GENDER"),
    (
        "CM_MEMBER_FIRST_NAME",
        "CM_MEMBER_LAST_NAME",
        "CM_MEMBER_BIRTH_DATE",
        "CM_MEMBER_GENDER",
    ),
    ("CM_MEMBER_SSN", "CM_MEMBER_ADDRESS_01", "CM_MEMBER_ZIP"),
    (
        "CM_SUBSCRIBER_CLIENT_ID",
        "CM_MEMBER_BIRTH_DATE",
        "CM_MEMBER_DEPENDENT_NUMBER",
    ),
    ("CP_PROVIDER_CLIENT_ID",),
    (
        "CP_PROVIDER_NPI",
        "CP_PROVIDER_FULL_NAME",
        "CP_PROVIDER_ADDRESS_01",
        "CP_PROVIDER_ZIP",
    ),
    ("CP_PROVIDER_RECORD_TYPE", "CP_PROVIDER_FEDERAL_TAX_ID"),
    (
        "CP_PROVIDER_TAXONOMY_CODE",
        "CP_PROVIDER_FULL_NAME",
        "CP_PROVIDER_ADDRESS_01",
        "CP_PROVIDER_ZIP",
    ),
)

_PROFESSIONAL_CLAIM_TIERS = (
    (
        "CH_PATIENT_CLIENT_ID",
        "CH_CLAIM_SERVICE_FROM_DATE",
        "CH_CLAIM_SERVICE_TO_DATE",
        "CH_BILLING_PROVIDER_NPI",
        "CH_RENDERING_PROVIDER_NPI",
        "CH_PLACE_OF_SERVICE_CODE",
        "CH_DIAGNOSIS_CODE_01",
        "CH_SUBSCRIBER_CLIENT_ID",
        "CH_CHARGE_AMOUNT",
        "CH_PATIENT_ACCOUNT_CONTROL_NUMBER",
    ),
    (
        "CH_PATIENT_CLIENT_ID",
        "CH_CLAIM_SERVICE_FROM_DATE",
        "CH_CLAIM_SERVICE_TO_DATE",
        "CH_BILLING_PROVIDER_NPI",
        "CH_RENDERING_PROVIDER_NPI",
        "CH_PLACE_OF_SERVICE_CODE",
        "CH_DIAGNOSIS_CODE_01",
        "CH_SUBSCRIBER_CLIENT_ID",
    ),
)

_INSTITUTIONAL_CLAIM_TIERS = (
    (
        "CH_PATIENT_CLIENT_ID",
        "CH_CLAIM_SERVICE_FROM_DATE",
        "CH_CLAIM_SERVICE_TO_DATE",
        "CH_BILLING_PROVIDER_NPI",
        "CH_ATTENDING_PROVIDER_NPI",
        "CH_DIAGNOSIS_CODE_01",
        "CD_SUBMITTED_REVENUE_CODE",
        "CH_TYPE_OF_BILL_CODE",
        "CH_SUBSCRIBER_CLIENT_ID",
        "CH_CHARGE_AMOUNT",
        "CH_PATIENT_ACCOUNT_CONTROL_NUMBER",
    ),
    (
        "CH_PATIENT_CLIENT_ID",
        "CH_CLAIM_SERVICE_FROM_DATE",
        "CH_CLAIM_SERVICE_TO_DATE",
        "CH_BILLING_PROVIDER_NPI",
        "CH_ATTENDING_PROVIDER_NPI",
        "CH_DIAGNOSIS_CODE_01",
        "CD_SUBMITTED_REVENUE_CODE",
        "CH_TYPE_OF_BILL_CODE",
        "CH_SUBSCRIBER_CLIENT_ID",
    ),
)


def _claim_match_tier(existing: Mapping[str, object], incoming: Mapping[str, object]) -> int | None:
    """Apply the documented profile-specific 837 composite matching tiers.

    Args:
        existing: Existing professional or institutional claim.
        incoming: Incoming claim to compare with ``existing``.

    Returns:
        One-based composite tier, or ``None`` when profiles differ or no
        documented composite is complete and equal.
    """
    if _claim_profile(existing) != _claim_profile(incoming):
        return None
    tiers = (
        _INSTITUTIONAL_CLAIM_TIERS
        if _claim_profile(existing) == "institutional"
        else _PROFESSIONAL_CLAIM_TIERS
    )
    for tier, keys in enumerate(tiers, start=1):
        if _claim_matches_all(existing, incoming, keys):
            return tier
    return None


def _matches_all(
    existing: Mapping[str, object], incoming: Mapping[str, object], keys: tuple[str, ...]
) -> bool:
    """Require populated equality for every field in a member/provider tier.

    Args:
        existing: Existing source-shaped record.
        incoming: Incoming source-shaped record.
        keys: Root or nested-address field names for the tier.

    Returns:
        ``True`` only when every key has the same nonblank source value.  The
        organization provider tier additionally requires both record types to
        be organization records.
    """
    if keys == ("CP_PROVIDER_RECORD_TYPE", "CP_PROVIDER_FEDERAL_TAX_ID"):
        return (
            existing.get("CP_PROVIDER_RECORD_TYPE") == "O"
            and incoming.get("CP_PROVIDER_RECORD_TYPE") == "O"
            and _source_value(existing, "CP_PROVIDER_FEDERAL_TAX_ID") not in (None, "")
            and _source_value(existing, "CP_PROVIDER_FEDERAL_TAX_ID")
            == _source_value(incoming, "CP_PROVIDER_FEDERAL_TAX_ID")
        )
    return all(
        (existing_value := _source_value(existing, key)) not in (None, "")
        and existing_value == _source_value(incoming, key)
        for key in keys
    )


def _source_value(record: Mapping[str, object], key: str) -> object | None:
    """Read a root source value or a value from the first address group.

    Args:
        record: Member or provider source record to inspect.
        key: Field name used by a documented member/provider identity tier.

    Returns:
        The populated value, or ``None`` when the field is absent or blank.
    """
    if key in record:
        return record[key]
    for group in ("CM_MEMBER_ADDRESSES", "CP_PROVIDER_ADDRESSES"):
        values = record.get(group)
        if isinstance(values, list) and values and isinstance(values[0], Mapping):
            value = values[0].get(key)
            if value not in (None, ""):
                return cast(object, value)
    return None


def _claim_matches_all(
    existing: Mapping[str, object], incoming: Mapping[str, object], keys: tuple[str, ...]
) -> bool:
    """Require populated equality for every field in a claim composite tier.

    Args:
        existing: Existing source-shaped claim.
        incoming: Incoming source-shaped claim.
        keys: Header or detail-row field names in the selected claim composite.

    Returns:
        ``True`` only when every composite key has the same nonblank value.
    """
    return all(
        (existing_value := _claim_value(existing, key)) not in (None, "")
        and existing_value == _claim_value(incoming, key)
        for key in keys
    )


def _claim_profile(record: Mapping[str, object]) -> str | None:
    """Select the documented claim composite family from the claim type.

    Args:
        record: Source-shaped claim record whose type is inspected.

    Returns:
        ``"professional"`` for type ``P``, ``"institutional"`` for type
        ``O``, or ``None`` when no supported type is present.  The Cotiviti
        institutional sample labels the claim class ``O`` while its transport
        header uses ``FILE_TYPE: 837I``.
    """
    claim_type = record.get("CH_CLAIM_TYPE")
    if claim_type == "P":
        return "professional"
    if claim_type in {"I", "O"}:
        return "institutional"
    return None


def _claim_value(record: Mapping[str, object], key: str) -> object | None:
    """Read a claim header value or a value from the first detail row.

    Args:
        record: Source-shaped claim record to inspect.
        key: Field name used by a documented claim composite tier.

    Returns:
        The populated value, or ``None`` when the field is absent or blank.
    """
    if key in record:
        return record[key]
    for line_group in ("CLAIM_DETAIL", "CD_CLAIM_LINES"):
        lines = record.get(line_group)
        if isinstance(lines, list) and lines and isinstance(lines[0], Mapping):
            value = lines[0].get(key)
            if value not in (None, ""):
                return cast(object, value)
    return None


def _is_newer(existing: Mapping[str, object], incoming: Mapping[str, object]) -> bool:
    """Compare the first shared source, payment, or effective date value.

    Args:
        existing: Existing source-shaped record.
        incoming: Incoming source-shaped record.

    Returns:
        ``True`` when no common recency field is present or the incoming value
        sorts later than the existing value; otherwise ``False``.
    """
    for key in _RECENCY_KEYS:
        if key in existing and key in incoming:
            return str(incoming[key]) > str(existing[key])
    return True


_RECENCY_KEYS = (
    "CM_MEMBER_SOURCE_UPDATED_AT",
    "CP_PROVIDER_SOURCE_UPDATED_AT",
    "CH_SOURCE_UPDATED_AT",
    "CH_CLAIM_PAID_DATE",
    "CD_LINE_PAID_DATE",
)


def _is_verified(record: Mapping[str, object]) -> bool:
    """Identify authoritative roster or claim-history source tags.

    Args:
        record: Source-shaped entity or claim record to inspect.

    Returns:
        ``True`` for normalized tags that identify verified, MR, or CH input.
    """
    tag = _source_tag(record)
    return "verified" in tag or tag.startswith("mr") or tag.startswith("ch")


def _is_provisional(record: Mapping[str, object]) -> bool:
    """Identify incremental 834 or 837 source tags.

    Args:
        record: Source-shaped entity or claim record to inspect.

    Returns:
        ``True`` for normalized tags that identify 834, 837, or provisional
        input.
    """
    tag = _source_tag(record)
    return "834" in tag or "837" in tag or "provisional" in tag


def _is_claim_transaction(record: Mapping[str, object]) -> bool:
    """Identify an incoming 837 transaction from its source tag.

    Args:
        record: Source-shaped claim record to inspect.

    Returns:
        ``True`` when the normalized source tag contains ``837``.
    """
    return "837" in _source_tag(record)


def _is_orphan_payment(record: Mapping[str, object]) -> bool:
    """Identify an existing unmatched 835 payment record.

    Args:
        record: Source-shaped claim/payment envelope to inspect.

    Returns:
        ``True`` when the source identifies an 835 transaction and record
        status identifies it as orphaned.
    """
    return (
        "835" in _source_tag(record) and "orphan" in str(record.get("CH_RECORD_STATUS", "")).lower()
    )


def _source_tag(record: Mapping[str, object]) -> str:
    """Return the first normalized source tag across supported entity shapes.

    Args:
        record: Member, provider, or claim record to inspect.

    Returns:
        The first nonblank supported source tag in lowercase, or an empty
        string when none is available.
    """
    for key in ("CM_MEMBER_SOURCE_RECORD_TAG", "CP_PROVIDER_SOURCE_RECORD_TAG", "CH_RECORD_TAG"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).lower()
    return ""
