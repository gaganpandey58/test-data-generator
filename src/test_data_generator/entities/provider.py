"""Generate deterministic, source-shaped provider records.

This module owns provider root, address, and network field construction for
the checked-in GDF provider layout and its source-shaped happy-path values.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from faker import Faker

from test_data_generator.configuration.profiles import record_header_values
from test_data_generator.core.identifiers import (
    deterministic_uuid4,
    run_token,
    valid_ein,
    valid_npi,
    valid_phone_number,
)
from test_data_generator.layouts import load_layout
from test_data_generator.samples.shapes import complete_record

_LOCATIONS = (
    ("AZ", "Tucson", "85704", "Pima", "Southwest"),
    ("CO", "Denver", "80202", "Denver", "Mountain"),
    ("MD", "Baltimore", "21201", "Baltimore City", "Mid-Atlantic"),
    ("TX", "Harlingen", "78550", "Cameron", "South"),
)
_SPECIALTIES = (
    ("207Q00000X", "Family Medicine"),
    ("208D00000X", "General Practice"),
    ("261QP2300X", "Primary Care Clinic"),
)


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int],
    client_headers: Mapping[str, object],
    client_values: Mapping[str, object],
    profile: str,
) -> dict[str, object]:
    """Generate one GDF-profile provider happy-path record.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based output position.
        entity_counts: Enabled stream counts; unused for provider rows.
        client_headers: Client-specific envelope header values.
        client_values: Client-specific provider body values.
        profile: Selected layout; fixed to ``provider`` by configuration.

    Returns:
        A source-shaped provider record that satisfies the provider schema.
    """
    del entity_counts, profile
    randomizer = Random(seed * 1000003 + index)
    faker = Faker("en_US")
    faker.seed_instance(seed * 1000003 + index)
    state, city, zip_code, county, region = randomizer.choice(_LOCATIONS)
    start = _date(date(2020, 1, 1) + timedelta(days=randomizer.randrange(1800)))
    individual = randomizer.choice((True, False))
    record_type = "1" if individual else "2"
    first = faker.first_name().upper() if individual else ""
    middle = faker.first_name()[0].upper() if individual else ""
    last = faker.last_name().upper() if individual else f"{faker.company().upper()} MEDICAL GROUP"
    full = " ".join(part for part in (first, middle, last) if part)
    npi = valid_npi(randomizer)
    client_platform = str(client_headers.get("CP_CLIENT_DATA_PLATFORM", ""))
    values = dict(client_values)
    token = run_token(seed)
    provider_id = f"PPRV{token}{index + 1:07d}{state}"
    master_id = f"{1_000_000_000 + ((abs(seed) + index) % 9_000_000_000):010d}"
    taxonomy, specialty = randomizer.choice(_SPECIALTIES)
    record: dict[str, object] = {field.name: "" for field in load_layout("provider").root}
    record.update({f"CP_CUSTOM_FIELD_{number:02d}": "" for number in range(1, 21)})
    record.update({"CP_CUSTOM_DATE_01": "", "CP_CUSTOM_DATE_02": ""})
    record.update(
        {
            "CP_PROVIDER_CLIENT_ID": provider_id,
            "CP_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PROVIDER_FEDERAL_TAX_ID": valid_ein(randomizer),
            "CP_PROVIDER_NPI": npi,
            "CP_PROVIDER_DEA_NUMBER": f"{randomizer.choice('ABCDEFGH')}{_digits(randomizer, 8)}",
            "CP_PROVIDER_STATE_LICENSE_STATE_CODE": state,
            "CP_PROVIDER_STATE_LICENSE_NUMBER": f"{state}{_digits(randomizer, 7)}",
            "CP_PROVIDER_MEDICARE_ID": f"MED{_digits(randomizer, 8)}",
            "CP_PROVIDER_MEDICAID_ID": f"MCA{_digits(randomizer, 8)}",
            "CP_PROVIDER_CHASE_INDICATOR": "N",
            "CP_PRESCRIBING_PROVIDER_INDICATOR": "Y",
            "CP_PRESCRIBING_PROVIDER_CLIENT_ID": provider_id,
            "CP_PRESCRIBING_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PRESCRIBING_PROVIDER_NPI": npi,
            "CP_PROVIDER_RECORD_TYPE": record_type,
            "CP_PROVIDER_FIRST_NAME": first,
            "CP_PROVIDER_MIDDLE_NAME": middle,
            "CP_PROVIDER_LAST_NAME": last,
            "CP_PROVIDER_NAME_TITLE": "MD" if individual else "",
            "CP_PROVIDER_FULL_NAME": full,
            "CP_PROVIDER_PRIMARY_SPECIALTY_CODE": taxonomy,
            "CP_PROVIDER_TAXONOMY_CODE": taxonomy,
            "CP_PROVIDER_BILLING_GROUP_NUMBER": f"BG{_digits(randomizer, 6)}",
            "CP_PROVIDER_BILLING_GROUP_NAME": specialty,
            "CP_PROVIDER_CCD_ID": f"CCD{_digits(randomizer, 6)}",
            "CP_PROVIDER_RECORD_START_DATE": start,
            "CP_PROVIDER_RECORD_END_DATE": "",
            "CP_PROVIDER_SOURCE_RECORD_TAG": "MR Verified" if index % 2 else "Provider Roster",
            "CP_PROVIDER_SOURCE_UPDATED_AT": "20260805",
            "CP_PROVIDER_ADDRESSES": [
                _address(
                    provider_id,
                    master_id,
                    state,
                    city,
                    zip_code,
                    county,
                    region,
                    start,
                    faker,
                    randomizer,
                    client_platform,
                )
            ],
            "CP_PROVIDER_NETWORKS": [
                _network(
                    provider_id,
                    master_id,
                    start,
                    randomizer,
                    client_platform,
                    str(values.get("provider_network_id_prefix", "")),
                    str(values.get("provider_network_name", "")),
                )
            ],
            "CP_CUSTOM_FIELD_01": "Test",
        }
    )
    record.update(_transport_headers(seed, index, client_headers))
    return complete_record(record, "provider")


def _address(
    provider_id: str,
    master_id: str,
    state: str,
    city: str,
    zip_code: str,
    county: str,
    region: str,
    start: str,
    faker: Faker,
    randomizer: Random,
    client_platform: str,
) -> dict[str, object]:
    """Build one source-shaped provider address group.

    Args:
        provider_id: Parent provider client ID.
        master_id: Parent provider master ID.
        state: Two-character source state code.
        city: City for the complete address tuple.
        zip_code: Postal code for the complete address tuple.
        county: County for the complete address tuple.
        region: Business region for the address.
        start: Compact effective date.
        faker: Seeded name and address data generator.
        randomizer: Seeded numeric data generator.
        client_platform: Client-selected source platform value.

    Returns:
        A layout-compatible provider address record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("provider").groups["CP_PROVIDER_ADDRESSES"]
    }
    fields.update(
        {
            "CP_CLIENT_DATA_PLATFORM": client_platform,
            "CP_PROVIDER_CLIENT_ID": provider_id,
            "CP_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PROVIDER_ADDRESS_CLIENT_ID": f"ADDR{_digits(randomizer, 8)}",
            "CP_PROVIDER_ADDRESS_PRIMARY_INDICATOR": "Y",
            "CP_PROVIDER_BILLING_ADDRESS_INDICATOR": "Y",
            "CP_PROVIDER_ADDRESS_01": faker.street_address().replace("\n", " "),
            "CP_PROVIDER_CITY": city.upper(),
            "CP_PROVIDER_STATE": state,
            "CP_PROVIDER_ZIP": zip_code,
            "CP_PROVIDER_ZIP_PLUS_FOUR": _digits(randomizer, 4),
            "CP_PROVIDER_COUNTY": county,
            "CP_PROVIDER_REGION": region,
            "CP_PROVIDER_PHONE": valid_phone_number(randomizer),
            "CP_PROVIDER_EMAIL": (
                f"{faker.user_name()}.{randomizer.randrange(1000, 10000)}@example.test"
            ),
            "CP_PROVIDER_ADDRESS_START_DATE": start,
            "CP_PROVIDER_ADDRESS_TERMINATION_DATE": "",
        }
    )
    return fields


def _network(
    provider_id: str,
    master_id: str,
    start: str,
    randomizer: Random,
    client_platform: str,
    network_id_prefix: str,
    network_name: str,
) -> dict[str, object]:
    """Build one source-shaped provider network group.

    Args:
        provider_id: Parent provider client ID.
        master_id: Parent provider master ID.
        start: Compact network effective date.
        randomizer: Seeded numeric data generator.
        client_platform: Client-selected source platform value.
        network_id_prefix: Client-selected network identifier prefix.
        network_name: Client-selected network display name.

    Returns:
        A layout-compatible provider network record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("provider").groups["CP_PROVIDER_NETWORKS"]
    }
    fields.update(
        {
            "CP_CLIENT_DATA_PLATFORM": client_platform,
            "CP_PROVIDER_CLIENT_ID": provider_id,
            "CP_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PROVIDER_NETWORK_CLIENT_ID": f"{network_id_prefix}-{_digits(randomizer, 5)}",
            "CP_PROVIDER_NETWORK_NAME": network_name,
            "CP_PROVIDER_NETWORK_INDICATOR": randomizer.choice(("Y", "N")),
            "CP_PROVIDER_NETWORK_EFFECTIVE_DATE": start,
            "CP_PROVIDER_NETWORK_TERMINATION_DATE": "",
        }
    )
    return fields


def _transport_headers(
    seed: int, index: int, client_headers: Mapping[str, object] | None
) -> dict[str, object]:
    """Build the flattened Cotiviti envelope used by EIP provider samples.

    Provider roster samples carry their EIP transport and GDF ingestion
    attributes at the root of the provider record. Deterministic UUIDv4-format
    identifiers preserve the source wire format without copying sample values.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based provider output position.
        client_headers: Resolved client-specific header values, if supplied.

    Returns:
        Source-compatible Cotiviti and ingestion attributes for one provider.
    """
    sequence = index + 1
    namespace = "provider"
    headers: dict[str, object] = {
        "cotiviti.dataset_id": "provider",
        "cotiviti.schema_version": "gdf-ppc-v1",
        "cotiviti.message_id": deterministic_uuid4(seed, f"{namespace}:message:{sequence}"),
        "cotiviti.produced_at": f"2026-08-05T00:{index % 60:02d}:00Z",
        "cotiviti.source_format": "provider_roster",
        "cotiviti.batch_id": f"test-provider-{seed}-{sequence:06d}",
        "cotiviti.message_seq": sequence,
        "cotiviti.correlation_id": deterministic_uuid4(seed, f"{namespace}:correlation"),
        "cotiviti.source.raw_file_ref": f"providers-{seed}-{sequence:06d}.csv",
        "ROWID": deterministic_uuid4(seed, f"{namespace}:row:{sequence}"),
        "PUBLISHER_NAME": "client_provider_gdf",
        "GDF_VERSION": "gdf-ppc-v1",
        "FILE_TYPE": "PR",
        "DATA_CATEGORY": "provider",
        "INGESTION_DATE": "20260805",
        "INGESTION_EPOCH": 1785888000 + sequence,
    }
    headers.update(record_header_values("provider", client_headers or {}))
    return headers


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
