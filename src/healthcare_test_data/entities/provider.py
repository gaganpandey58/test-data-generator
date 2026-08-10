"""Generate source-shaped provider records with deterministic scenario variations."""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from faker import Faker

from healthcare_test_data.layouts import load_layout
from healthcare_test_data.scenarios import Scenario

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
_OPTIONAL_INCOMPLETE_FIELDS = (
    "CP_PROVIDER_DEA_NUMBER",
    "CP_PROVIDER_MEDICARE_ID",
    "CP_PROVIDER_MEDICAID_ID",
)


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int] | None = None,
    *,
    scenario: Scenario | None = None,
) -> dict[str, object]:
    """Generate one GDF-profile provider record, optionally varying a baseline.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based output position.
        entity_counts: Unused related-entity counts accepted by the common API.
        scenario: Optional planned variation for this output position.

    Returns:
        A source-shaped provider record that satisfies the provider schema.
    """
    del entity_counts
    if scenario is not None and scenario.baseline_index is not None:
        return _mutate(generate_record(seed, scenario.baseline_index), scenario)
    randomizer = Random(seed * 1000003 + index)
    faker = Faker("en_US")
    faker.seed_instance(seed * 1000003 + index)
    state, city, zip_code, county, region = randomizer.choice(_LOCATIONS)
    start = _date(date(2020, 1, 1) + timedelta(days=randomizer.randrange(1800)))
    individual = randomizer.choice((True, False))
    record_type = "I" if individual else "O"
    first = faker.first_name().upper() if individual else ""
    middle = faker.first_name()[0].upper() if individual else ""
    last = faker.last_name().upper() if individual else f"{faker.company().upper()} MEDICAL GROUP"
    full = " ".join(part for part in (first, middle, last) if part)
    npi = _npi(randomizer)
    provider_id = f"PPRV{index + 1:010d}{state}"
    master_id = f"{1500000000 + index:010d}"
    taxonomy, specialty = randomizer.choice(_SPECIALTIES)
    record: dict[str, object] = {field.name: "" for field in load_layout("provider").root}
    record.update({f"CP_CUSTOM_FIELD_{number:02d}": "" for number in range(1, 21)})
    record.update({"CP_CUSTOM_DATE_01": "", "CP_CUSTOM_DATE_02": ""})
    record.update(
        {
            "CP_CLIENT_DATA_PLATFORM": "QNXT",
            "CLIENT_DATA_PLATFORM": "QNXT",
            "CP_PROVIDER_CLIENT_ID": provider_id,
            "CP_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PROVIDER_FEDERAL_TAX_ID": _digits(randomizer, 9),
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
                )
            ],
            "CP_PROVIDER_NETWORKS": [_network(provider_id, master_id, start, randomizer)],
            "PAYER_PLATFORM": "CHC-QNXT",
            "PAYER": "CHC",
            "INGESTION_DATE": "20260805",
            "INGESTION_EPOCH": 1785945600 + index,
            "ROWID": f"ROW{_digits(randomizer, 12)}",
            "PUBLISHER_NAME": "synthetic_provider_generator",
            "CP_CUSTOM_FIELD_01": "SYNTHETIC",
        }
    )
    return record


def _mutate(baseline: dict[str, object], scenario: Scenario) -> dict[str, object]:
    """Apply one provider variation without changing the baseline record.

    Args:
        baseline: Deterministic provider record selected by the scenario plan.
        scenario: Named variation to apply.

    Returns:
        A copied provider record with the requested source-valid variation.
    """
    record = _copy_record(baseline)
    if scenario.name == "duplicate":
        return record
    if scenario.name == "changed":
        record["CP_PROVIDER_SOURCE_UPDATED_AT"] = "20260806"
        addresses = record["CP_PROVIDER_ADDRESSES"]
        assert isinstance(addresses, list) and addresses
        address = addresses[0]
        assert isinstance(address, dict)
        state, city, zip_code, county, region = _next_location(str(address["CP_PROVIDER_STATE"]))
        address.update(
            {
                "CP_PROVIDER_ADDRESS_01": "900 UPDATED AVENUE",
                "CP_PROVIDER_CITY": city.upper(),
                "CP_PROVIDER_STATE": state,
                "CP_PROVIDER_ZIP": zip_code,
                "CP_PROVIDER_COUNTY": county,
                "CP_PROVIDER_REGION": region,
            }
        )
    elif scenario.name == "stale":
        record["CP_PROVIDER_SOURCE_UPDATED_AT"] = "20260804"
        record["CP_PROVIDER_RECORD_START_DATE"] = "20190101"
        addresses = record["CP_PROVIDER_ADDRESSES"]
        assert isinstance(addresses, list) and addresses
        address = addresses[0]
        assert isinstance(address, dict)
        address["CP_PROVIDER_ADDRESS_START_DATE"] = "20190101"
    elif scenario.name == "incomplete":
        for field in _OPTIONAL_INCOMPLETE_FIELDS:
            record.pop(field, None)
        addresses = record["CP_PROVIDER_ADDRESSES"]
        assert isinstance(addresses, list) and addresses
        address = addresses[0]
        assert isinstance(address, dict)
        address.pop("CP_PROVIDER_ADDRESS_01", None)
    return record


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

    Returns:
        A layout-compatible provider address record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("provider").groups["CP_PROVIDER_ADDRESSES"]
    }
    fields.update(
        {
            "CP_CLIENT_DATA_PLATFORM": "QNXT",
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
            "CP_PROVIDER_PHONE": _digits(randomizer, 10),
            "CP_PROVIDER_EMAIL": f"provider{_digits(randomizer, 5)}@example.test",
            "CP_PROVIDER_ADDRESS_START_DATE": start,
            "CP_PROVIDER_ADDRESS_TERMINATION_DATE": "",
        }
    )
    return fields


def _network(provider_id: str, master_id: str, start: str, randomizer: Random) -> dict[str, object]:
    """Build one source-shaped provider network group.

    Args:
        provider_id: Parent provider client ID.
        master_id: Parent provider master ID.
        start: Compact network effective date.
        randomizer: Seeded numeric data generator.

    Returns:
        A layout-compatible provider network record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("provider").groups["CP_PROVIDER_NETWORKS"]
    }
    fields.update(
        {
            "CP_CLIENT_DATA_PLATFORM": "QNXT",
            "CP_PROVIDER_CLIENT_ID": provider_id,
            "CP_PROVIDER_CLIENT_MASTER_ID": master_id,
            "CP_PROVIDER_NETWORK_CLIENT_ID": f"CHC-QNXT-{_digits(randomizer, 5)}",
            "CP_PROVIDER_NETWORK_NAME": "CHC QNXT Network",
            "CP_PROVIDER_NETWORK_INDICATOR": "I",
            "CP_PROVIDER_NETWORK_EFFECTIVE_DATE": start,
            "CP_PROVIDER_NETWORK_TERMINATION_DATE": "",
        }
    )
    return fields


def _copy_record(record: dict[str, object]) -> dict[str, object]:
    """Copy mutable provider groups before changing a scenario variation.

    Args:
        record: Baseline provider record to copy.

    Returns:
        A shallow root copy with independent nested group dictionaries.

    Raises:
        AssertionError: If expected nested groups are not lists.
    """
    copied = dict(record)
    addresses = record["CP_PROVIDER_ADDRESSES"]
    networks = record["CP_PROVIDER_NETWORKS"]
    assert isinstance(addresses, list)
    assert isinstance(networks, list)
    copied["CP_PROVIDER_ADDRESSES"] = [
        dict(item) for item in addresses if isinstance(item, Mapping)
    ]
    copied["CP_PROVIDER_NETWORKS"] = [dict(item) for item in networks if isinstance(item, Mapping)]
    return copied


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


def _npi(randomizer: Random) -> str:
    """Generate a valid ten-digit National Provider Identifier.

    Args:
        randomizer: Seeded pseudo-random value source.

    Returns:
        Luhn-valid ten-digit NPI.

    Raises:
        AssertionError: If no check digit produces a valid NPI.
    """
    body = f"{randomizer.randrange(100000000, 1000000000):09d}"
    for check in range(10):
        candidate = body + str(check)
        if _is_luhn_valid("80840" + candidate):
            return candidate
    raise AssertionError("could not generate NPI")


def _is_luhn_valid(number: str) -> bool:
    """Check a decimal string with the Luhn checksum algorithm.

    Args:
        number: Digits to validate.

    Returns:
        ``True`` when the sequence passes the Luhn checksum.
    """
    total = 0
    for position, character in enumerate(reversed(number)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _next_location(state: str) -> tuple[str, str, str, str, str]:
    """Return the next complete source location after a known state.

    Args:
        state: Existing two-character state code from ``_LOCATIONS``.

    Returns:
        State, city, ZIP code, county, and region for the next location.

    Raises:
        StopIteration: If ``state`` is not represented by ``_LOCATIONS``.
    """
    location_index = next(
        index for index, location in enumerate(_LOCATIONS) if location[0] == state
    )
    return _LOCATIONS[(location_index + 1) % len(_LOCATIONS)]
