"""Generate deterministic, source-shaped member records.

The generator builds member roots and their address, enrollment, and COB
groups from the checked-in GDF member layout. It resolves PCP references to
the provider rows selected for the same run.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from faker import Faker

from test_data_generator.configuration.profiles import record_header_values
from test_data_generator.core.identifiers import deterministic_uuid4
from test_data_generator.entities.provider import generate_record as generate_provider
from test_data_generator.layouts import load_layout
from test_data_generator.samples.shapes import complete_record

_LOCATIONS = (
    ("AZ", "Tucson", "85704", "Pima"),
    ("CO", "Denver", "80202", "Denver"),
    ("MD", "Baltimore", "21201", "Baltimore City"),
    ("TX", "Harlingen", "78550", "Cameron"),
)


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int],
    client_headers: Mapping[str, object],
    client_values: Mapping[str, object],
    profile: str,
) -> dict[str, object]:
    """Generate one GDF-profile member happy-path record.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based output position.
        entity_counts: Enabled entity counts used for PCP links.
        client_headers: Client-specific envelope header values.
        client_values: Client-specific member body values.
        profile: Selected layout; fixed to ``member`` by configuration.

    Returns:
        A source-shaped member record that satisfies the member schema.
    """
    del profile
    randomizer = Random(_record_seed(seed, index))
    faker = Faker("en_US")
    faker.seed_instance(_record_seed(seed, index))
    dependent = index % 4 == 3
    subscriber_index = index - 1 if dependent else index
    state, city, zip_code, county = randomizer.choice(_LOCATIONS)
    start = _date(date(2020, 1, 1) + timedelta(days=randomizer.randrange(1800)))
    birth = _date(date(1940, 1, 1) + timedelta(days=randomizer.randrange(28000)))
    member_id = f"MBR{index + 1:010d}"
    client_platform = str(client_headers.get("CM_CLIENT_DATA_PLATFORM", ""))
    values = dict(client_values)
    record = _profile_blanks("member")
    record.update(
        {
            "CM_MEMBER_CLIENT_ID": member_id,
            "CM_PAYER_SHORT": str(client_headers.get("CM_PAYER_SHORT", "")),
            "CM_MEMBER_CLIENT_MASTER_ID": f"MM{1500000000 + index:010d}",
            "CM_MEMBER_DEPENDENT_NUMBER": 1 if dependent else 0,
            "CM_SUBSCRIBER_CLIENT_ID": f"SUB{subscriber_index + 1:010d}",
            "CM_SUBSCRIBER_CLIENT_MASTER_ID": f"SM{1500000000 + subscriber_index:010d}",
            "CM_SUBSCRIBER_SSN": _digits(randomizer, 9),
            "CM_MEMBER_ALTERNATE_ID": f"ALT{index + 1:010d}",
            "CM_MEMBER_SSN": _digits(randomizer, 9),
            "CM_MEMBER_MEDICARE_HICN_ID": f"H{_digits(randomizer, 10)}A",
            "CM_MEMBER_MEDICARE_BENEFICIARY_ID": (
                f"{randomizer.randrange(1, 10)}{faker.bothify('??#####??##')}"
            ),
            "CM_MEMBER_MEDICAID_ID": f"MCD{index + 1:010d}",
            "CM_MEMBER_MEDICAL_RECORD_NUMBER": f"MRN{index + 1:010d}",
            "CM_MEMBER_FIRST_NAME": faker.first_name().upper(),
            "CM_MEMBER_MIDDLE_NAME": faker.first_name()[0].upper(),
            "CM_MEMBER_LAST_NAME": faker.last_name().upper(),
            "CM_MEMBER_BIRTH_DATE": birth,
            "CM_MEMBER_GENDER": randomizer.choice(("F", "M", "X")),
            "CM_MEMBER_RELATIONSHIP_TO_SUBSCRIBER": "19" if dependent else "18",
            "CM_MEMBER_RELATIONSHIP_CODE": "19" if dependent else "18",
            "CM_MEMBER_RECORD_START_DATE": start,
            "CM_MEMBER_RECORD_END_DATE": "",
            "CM_MEMBER_SOURCE_RECORD_TAG": "MR Verified" if index % 2 else "834 Provisional",
            "CM_MEMBER_RECORD_STATUS": "Active" if index % 2 else "New",
            "CM_MEMBER_SOURCE_UPDATED_AT": "20260805",
            "CM_MEMBER_ADDRESSES": [
                _address(
                    index,
                    member_id,
                    state,
                    city,
                    zip_code,
                    county,
                    start,
                    faker,
                    randomizer,
                    client_platform,
                )
            ],
            "CM_MEMBER_ENROLLMENTS": [
                {
                    "CM_MEMBER_ENROLLMENT_CLIENT_ID": f"ENR{index + 1:010d}",
                    "CM_MEMBER_ENROLLMENT_EFFECTIVE_DATE": start,
                    "CM_MEMBER_ENROLLMENT_TERMINATION_DATE": "",
                    "CM_LINE_OF_BUSINESS_CODE": "MED",
                    "CM_NETWORK_CLIENT_ID": str(values.get("member_network_client_id", "")),
                    "CM_PLAN_CLIENT_ID": "PLAN-GOLD",
                    "CM_PCP_PROVIDER_CLIENT_ID": _pcp_provider_id(seed, index, entity_counts),
                }
            ],
            "CM_MEMBER_COB": [_cob(index, start)],
        }
    )
    record.update(_transport_headers(seed, index, client_headers))
    return complete_record(record, "member")


def _profile_blanks(profile: str) -> dict[str, object]:
    """Create blank root fields for one checked-in layout profile.

    Args:
        profile: Name of the GDF layout profile to load.

    Returns:
        A field-name-to-blank-value mapping for the profile root.
    """
    layout = load_layout(profile)
    return {field.name: "" for field in layout.root}


def _transport_headers(
    seed: int, index: int, client_headers: Mapping[str, object] | None
) -> dict[str, object]:
    """Build the flattened Cotiviti envelope used by EIP member samples.

    The member source examples place transport attributes beside the GDF
    member body, rather than inside a nested envelope. Deterministic UUIDv4
    identifiers remain stable for a given seed and output position while the
    generator never repeats a sample value.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based member output position.
        client_headers: Resolved client-specific header values, if supplied.

    Returns:
        Source-compatible Cotiviti and ingestion attributes for one member.
    """
    sequence = index + 1
    namespace = "member"
    headers: dict[str, object] = {
        "cotiviti.dataset_id": "members",
        "cotiviti.schema_version": "gdf-ppc-v1",
        "cotiviti.message_id": deterministic_uuid4(seed, f"{namespace}:message:{sequence}"),
        "cotiviti.produced_at": f"2026-08-05T00:{index % 60:02d}:00Z",
        "cotiviti.source_format": "edi_x12_834",
        "cotiviti.batch_id": deterministic_uuid4(seed, f"{namespace}:batch"),
        "cotiviti.message_seq": sequence,
        "cotiviti.correlation_id": deterministic_uuid4(seed, f"{namespace}:correlation"),
        "cotiviti.producer_version": "test-data-generator/1",
        "cotiviti.source.isa_control": f"{seed % 1_000_000_000:09d}",
        "cotiviti.source.gs_control": sequence,
        "cotiviti.source.raw_file_ref": f"members-{seed}-{sequence:06d}.834",
        "ROWID": deterministic_uuid4(seed, f"{namespace}:row:{sequence}"),
        "PUBLISHER_NAME": "client_member_gdf",
        "GDF_VERSION": "gdf-ppc-v1",
        "FILE_TYPE": "834",
        "DATA_CATEGORY": "member",
        "INGESTION_DATE": "20260805",
        "INGESTION_EPOCH": 1785888000 + sequence,
    }
    headers.update(record_header_values("member", client_headers or {}))
    return headers


def _address(
    index: int,
    member_id: str,
    state: str,
    city: str,
    zip_code: str,
    county: str,
    start: str,
    faker: Faker,
    randomizer: Random,
    client_platform: str,
) -> dict[str, object]:
    """Build one source-shaped member address group.

    Args:
        index: Stable member position used for deterministic identifiers.
        member_id: Parent member client ID.
        state: Two-character source state code.
        city: City for the complete address tuple.
        zip_code: Postal code for the complete address tuple.
        county: County for the complete address tuple.
        start: Compact effective date.
        faker: Seeded name and address data generator.
        randomizer: Seeded numeric data generator.
        client_platform: Client-selected source platform value.

    Returns:
        A layout-compatible member address record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("member").groups["CM_MEMBER_ADDRESSES"]
    }
    fields.update(
        {
            "CM_CLIENT_DATA_PLATFORM": client_platform,
            "CM_MEMBER_CLIENT_ID": member_id,
            "CM_MEMBER_CLIENT_MASTER_ID": f"MM{1500000000 + index:010d}",
            "CM_MEMBER_ADDRESS_CLIENT_ID": f"MADDR{index + 1:08d}",
            "CM_MEMBER_ADDRESS_TYPE": "HOME",
            "CM_MEMBER_ADDRESS_TYPE_CODE": "HOME",
            "CM_MEMBER_ADDRESS_PRIMARY_INDICATOR": "Y",
            "CM_MEMBER_ADDRESS_01": faker.street_address().replace("\n", " "),
            "CM_MEMBER_CITY": city.upper(),
            "CM_MEMBER_STATE": state,
            "CM_MEMBER_ZIP": zip_code,
            "CM_MEMBER_ZIP_PLUS_FOUR": _digits(randomizer, 4),
            "CM_MEMBER_COUNTY": county.upper(),
            "CM_MEMBER_EMAIL": f"member{index + 1}@example.test",
            "CM_MEMBER_PHONE": _digits(randomizer, 10),
            "CM_MEMBER_ADDRESS_START_DATE": start,
            "CM_MEMBER_ADDRESS_TERMINATION_DATE": "",
            "CM_MEMBER_ADDRESS_END_DATE": "",
        }
    )
    return fields


def _cob(index: int, start: str) -> dict[str, object]:
    """Build one source-compatible member coordination-of-benefits item.

    Args:
        index: Stable member position used for deterministic source values.
        start: Compact effective date shared with the member record.

    Returns:
        A complete COB group containing the fields declared by the layout.
    """
    return {
        "CM_MEMBER_COB_ORDER_NUMBER": "1",
        "CM_OTHER_PAYER_NAME": f"OTHER PAYER {index + 1:06d}",
        "CM_MEMBER_COB_EFFECTIVE_DATE": start,
        "CM_MEMBER_COB_TERMINATION_DATE": "",
    }


def _pcp_provider_id(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int],
) -> str:
    """Resolve a deterministic PCP provider ID for one member.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable member position.
        entity_counts: Enabled entity counts.

    Returns:
        A linked provider ID, or a blank value when providers are disabled.

    Raises:
        AssertionError: If the provider generator returns a non-string ID.
    """
    if entity_counts.get("provider", 0) < 1:
        return ""
    count = entity_counts["provider"]
    provider_index = index % count
    provider_id = generate_provider(seed, provider_index, entity_counts, {}, {}, "provider")[
        "CP_PROVIDER_CLIENT_ID"
    ]
    assert isinstance(provider_id, str)
    return provider_id


def _record_seed(seed: int, index: int) -> int:
    """Derive a stable member-local random seed.

    Args:
        seed: Shared generation seed.
        index: Stable member position.

    Returns:
        Deterministic integer seed for this member.
    """
    return seed * 1000033 + index


def _date(value: date) -> str:
    """Format a date as an eight-digit GDF date.

    Args:
        value: Date to format.

    Returns:
        Compact ``YYYYMMDD`` source value.
    """
    return value.strftime("%Y%m%d")


def _digits(randomizer: Random, width: int) -> str:
    """Generate a fixed-width, non-leading-zero numeric source value.

    Args:
        randomizer: Seeded pseudo-random value source.
        width: Required digit count.

    Returns:
        Numeric string of exactly ``width`` characters.
    """
    return f"{randomizer.randrange(10 ** (width - 1), 10**width):0{width}d}"
