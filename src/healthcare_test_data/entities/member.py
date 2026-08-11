"""Generate deterministic, source-shaped member records.

The generator builds member roots and their address, enrollment, and COB
groups from the checked-in GDF member profile.  It resolves PCP references to
the provider rows selected for the same run and applies configured scenario
variations only to copied deterministic baselines.
"""

import json
from collections.abc import Mapping
from datetime import date, timedelta
from importlib.resources import files
from random import Random

from faker import Faker

from healthcare_test_data.entities.provider import generate_record as generate_provider
from healthcare_test_data.identifiers import deterministic_uuid4
from healthcare_test_data.layouts import load_layout
from healthcare_test_data.scenarios import Scenario, plan

_LOCATIONS = (
    ("AZ", "Tucson", "85704", "Pima"),
    ("CO", "Denver", "80202", "Denver"),
    ("MD", "Baltimore", "21201", "Baltimore City"),
    ("TX", "Harlingen", "78550", "Cameron"),
)
_OPTIONAL_INCOMPLETE_FIELDS = (
    "CM_PAYER_SHORT",
    "CM_MEMBER_SSN",
    "CM_MEMBER_ALTERNATE_ID",
    "CM_MEMBER_MEDICARE_HICN_ID",
)


def generate_record(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int] | None = None,
    *,
    scenario: Scenario | None = None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None = None,
) -> dict[str, object]:
    """Generate one GDF-profile member record, optionally varying a baseline.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based output position.
        entity_counts: Optional enabled-entity counts used for PCP links.
        scenario: Optional planned variation for this output position.
        entity_scenarios: Optional scenario quantities for linked provider
            output rows.

    Returns:
        A source-shaped member record that satisfies the member schema.
    """
    if scenario is not None and scenario.baseline_index is not None:
        baseline = generate_record(
            seed,
            scenario.baseline_index,
            entity_counts,
            entity_scenarios=entity_scenarios,
        )
        return _mutate(baseline, scenario)
    randomizer = Random(_record_seed(seed, index))
    faker = Faker("en_US")
    faker.seed_instance(_record_seed(seed, index))
    dependent = index % 4 == 3
    subscriber_index = index - 1 if dependent else index
    state, city, zip_code, county = randomizer.choice(_LOCATIONS)
    start = _date(date(2020, 1, 1) + timedelta(days=randomizer.randrange(1800)))
    birth = _date(date(1940, 1, 1) + timedelta(days=randomizer.randrange(28000)))
    member_id = f"MBR{index + 1:010d}"
    # The EIP reference sample is the authoritative JSON-kind contract.  Add
    # GDF-only fields first, then let sample defaults preserve kinds for fields
    # that appear in both sources (for example risk score).
    record = {**_profile_blanks("member"), **_sample_root_defaults()}
    record.update(
        {
            "CM_CLIENT_DATA_PLATFORM": "QNXT",
            "CM_PAYER_SHORT": "CHC",
            "CM_MEMBER_CLIENT_ID": member_id,
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
                _address(index, member_id, state, city, zip_code, county, start, faker, randomizer)
            ],
            "CM_MEMBER_ENROLLMENTS": [
                {
                    "CM_MEMBER_ENROLLMENT_CLIENT_ID": f"ENR{index + 1:010d}",
                    "CM_MEMBER_ENROLLMENT_EFFECTIVE_DATE": start,
                    "CM_MEMBER_ENROLLMENT_TERMINATION_DATE": "",
                    "CM_LINE_OF_BUSINESS_CODE": "MED",
                    "CM_NETWORK_CLIENT_ID": "CHC-QNXT",
                    "CM_PLAN_CLIENT_ID": "PLAN-GOLD",
                    "CM_PCP_PROVIDER_CLIENT_ID": _pcp_provider_id(
                        seed, index, entity_counts, entity_scenarios
                    ),
                }
            ],
            "CM_MEMBER_COB": [],
        }
    )
    record.update(_transport_headers(seed, index))
    return record


def _mutate(baseline: dict[str, object], scenario: Scenario) -> dict[str, object]:
    """Apply one member variation without changing the baseline record.

    Args:
        baseline: Deterministic source record selected by the scenario plan.
        scenario: Named variation to apply.

    Returns:
        A copied member record with the requested source-valid variation.
    """
    record = _copy_record(baseline)
    if scenario.name == "duplicate":
        return record
    if scenario.name == "changed":
        record["CM_MEMBER_SOURCE_UPDATED_AT"] = "20260806"
        record["CM_MEMBER_RECORD_STATUS"] = "Active"
        record["CM_MEMBER_SOURCE_RECORD_TAG"] = "834 Provisional"
        addresses = record["CM_MEMBER_ADDRESSES"]
        assert isinstance(addresses, list) and addresses
        address = addresses[0]
        assert isinstance(address, dict)
        state, city, zip_code, county = _next_location(str(address["CM_MEMBER_STATE"]))
        address.update(
            {
                "CM_MEMBER_ADDRESS_01": "900 UPDATED AVENUE",
                "CM_MEMBER_CITY": city.upper(),
                "CM_MEMBER_STATE": state,
                "CM_MEMBER_ZIP": zip_code,
                "CM_MEMBER_COUNTY": county.upper(),
            }
        )
    elif scenario.name == "stale":
        record["CM_MEMBER_SOURCE_UPDATED_AT"] = "20260804"
        record["CM_MEMBER_RECORD_START_DATE"] = "20190101"
        addresses = record["CM_MEMBER_ADDRESSES"]
        assert isinstance(addresses, list) and addresses
        address = addresses[0]
        assert isinstance(address, dict)
        address["CM_MEMBER_ADDRESS_START_DATE"] = "20190101"
    elif scenario.name == "incomplete":
        for field in _OPTIONAL_INCOMPLETE_FIELDS:
            record[field] = (
                "000000000" if field == "CM_MEMBER_SSN" else _blank_like(record.get(field))
            )
    elif scenario.name == "new":
        # A new row is independently generated before this branch is reached.
        return record
    return record


def _profile_blanks(profile: str) -> dict[str, object]:
    """Create blank root fields for one checked-in layout profile.

    Args:
        profile: Name of the GDF layout profile to load.

    Returns:
        A field-name-to-blank-value mapping for the profile root.
    """
    layout = load_layout(profile)
    return {field.name: "" for field in layout.root}


def _sample_root_defaults() -> dict[str, object]:
    """Create blank values for every root field in the supplied member sample.

    The checked-in field-kind registry preserves the complete EIP member
    envelope without copying source values. Real generated values overwrite
    identity, relationship, address, enrollment, and transport fields later in
    :func:`generate_record`; the remaining optional fields retain source-valid
    blank values of the matching JSON kind.

    Returns:
        A new mutable mapping containing every documented member root field.

    Raises:
        RuntimeError: If the packaged field-kind registry is malformed.
    """
    try:
        raw = json.loads(
            files("healthcare_test_data")
            .joinpath("member_field_defaults.json")
            .read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not load packaged member field defaults") from error
    if not isinstance(raw, dict) or not all(
        isinstance(name, str) and isinstance(kind, str) for name, kind in raw.items()
    ):
        raise RuntimeError("Packaged member field defaults are malformed")
    blank_by_kind: dict[str, object] = {
        "string": "",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
        "array": [],
        "object": {},
    }
    if any(kind not in blank_by_kind for kind in raw.values()):
        raise RuntimeError("Packaged member field defaults contain an unknown JSON kind")
    defaults: dict[str, object] = {}
    for name, kind in raw.items():
        if kind == "array":
            defaults[name] = []
        elif kind == "object":
            defaults[name] = {}
        else:
            defaults[name] = blank_by_kind[kind]
    return defaults


def _blank_like(value: object | None) -> object:
    """Return a source-valid empty value while preserving a JSON value type.

    Scenario records must keep the same EIP field set as baseline records.
    This helper represents a missing optional value using the source's normal
    empty convention instead of deleting its field from the JSON object.

    Args:
        value: Existing generated value whose JSON type is retained.

    Returns:
        An empty string, numeric zero, false, empty list, or empty mapping.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return ""


def _transport_headers(seed: int, index: int) -> dict[str, object]:
    """Build the flattened Cotiviti envelope used by EIP member samples.

    The member source examples place transport attributes beside the GDF
    member body, rather than inside a nested envelope. Deterministic UUIDv4
    identifiers remain stable for a given seed and output position while the
    generator never repeats a sample value.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable zero-based member output position.

    Returns:
        Source-compatible Cotiviti and ingestion attributes for one member.
    """
    sequence = index + 1
    namespace = "member"
    return {
        "cotiviti.dataset_id": "members",
        "cotiviti.tenant_id": "tnt_ppc_synthetic",
        "cotiviti.schema_version": "gdf-ppc-v1",
        "cotiviti.client_id": "synthetic.health.payer",
        "cotiviti.client_system": "synthetic.health.payer",
        "cotiviti.message_id": deterministic_uuid4(seed, f"{namespace}:message:{sequence}"),
        "cotiviti.produced_at": f"2026-08-05T00:{index % 60:02d}:00Z",
        "cotiviti.source_format": "edi_x12_834",
        "cotiviti.source_system": "PPC",
        "cotiviti.batch_id": deterministic_uuid4(seed, f"{namespace}:batch"),
        "cotiviti.message_seq": sequence,
        "cotiviti.correlation_id": deterministic_uuid4(seed, f"{namespace}:correlation"),
        "cotiviti.producer_version": "test-data-generator/1",
        "cotiviti.source.isa_control": f"{seed % 1_000_000_000:09d}",
        "cotiviti.source.gs_control": sequence,
        "cotiviti.source.raw_file_ref": f"members-{seed}-{sequence:06d}.834",
        "ROWID": deterministic_uuid4(seed, f"{namespace}:row:{sequence}"),
        "PAYER": "CHC",
        "PAYER_PLATFORM": "CHC-QNXT",
        "CLIENT_DATA_PLATFORM": "QNXT",
        "PUBLISHER_NAME": "client_member_gdf",
        "PRODUCT": "PPC",
        "GDF_VERSION": "gdf-ppc-v1",
        "FILE_TYPE": "834",
        "DATA_CATEGORY": "member",
        "LOB": "tnt_ppc_synthetic",
        "INGESTION_DATE": "20260805",
        "INGESTION_EPOCH": 1785888000 + sequence,
    }


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

    Returns:
        A layout-compatible member address record.
    """
    fields: dict[str, object] = {
        field.name: "" for field in load_layout("member").groups["CM_MEMBER_ADDRESSES"]
    }
    fields.update(
        {
            "CM_CLIENT_DATA_PLATFORM": "QNXT",
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


def _copy_record(record: dict[str, object]) -> dict[str, object]:
    """Copy mutable member groups before changing a scenario variation.

    Args:
        record: Baseline member record to copy.

    Returns:
        A shallow root copy with independent nested group dictionaries.

    Raises:
        AssertionError: If expected nested groups are not lists.
    """
    copied = dict(record)
    addresses = record["CM_MEMBER_ADDRESSES"]
    enrollments = record["CM_MEMBER_ENROLLMENTS"]
    cob = record["CM_MEMBER_COB"]
    assert isinstance(addresses, list)
    assert isinstance(enrollments, list)
    assert isinstance(cob, list)
    copied["CM_MEMBER_ADDRESSES"] = [dict(item) for item in addresses if isinstance(item, Mapping)]
    copied["CM_MEMBER_ENROLLMENTS"] = [
        dict(item) for item in enrollments if isinstance(item, Mapping)
    ]
    copied["CM_MEMBER_COB"] = [dict(item) for item in cob if isinstance(item, Mapping)]
    return copied


def _pcp_provider_id(
    seed: int,
    index: int,
    entity_counts: Mapping[str, int] | None,
    entity_scenarios: Mapping[str, Mapping[str, int]] | None,
) -> str:
    """Resolve a deterministic PCP provider ID for one member.

    Args:
        seed: Shared deterministic generation seed.
        index: Stable member position.
        entity_counts: Optional enabled-entity counts.
        entity_scenarios: Optional scenario quantities for emitted providers.

    Returns:
        A linked provider ID, or a blank value when providers are disabled.

    Raises:
        AssertionError: If the provider generator returns a non-string ID.
    """
    if entity_counts is not None and entity_counts.get("provider", 0) < 1:
        return ""
    count = max(1, entity_counts.get("provider", 10) if entity_counts else 10)
    provider_index = index % count
    scenarios = entity_scenarios.get("provider", {}) if entity_scenarios else {}
    provider_scenario = plan(count, scenarios, seed).variation_for(provider_index)
    provider_id = generate_provider(seed, provider_index, scenario=provider_scenario)[
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


def _next_location(state: str) -> tuple[str, str, str, str]:
    """Return the next complete source location after a known state.

    Args:
        state: Existing two-character state code from ``_LOCATIONS``.

    Returns:
        State, city, ZIP code, and county for the next source location.

    Raises:
        StopIteration: If ``state`` is not represented by ``_LOCATIONS``.
    """
    location_index = next(
        index for index, location in enumerate(_LOCATIONS) if location[0] == state
    )
    return _LOCATIONS[(location_index + 1) % len(_LOCATIONS)]
