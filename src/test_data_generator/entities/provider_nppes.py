"""NPPES sample profiles used by the linked provider fixture generator.

The checked-in reference contains one individual (entity type ``1``) and one
organizational (entity type ``2``) record.  Keeping the profiles separate
prevents an organizational record from accidentally using an individual
template, or vice versa, while retaining the single public NPPES output file.
"""

from collections.abc import Mapping
from datetime import date, timedelta
from random import Random

from faker import Faker

from test_data_generator.core.identifiers import deterministic_uuid4

INDIVIDUAL = "individual"
ORGANIZATIONAL = "organizational"


def profile_name(entity_type_code: object) -> str:
    """Return the profile name for an NPPES entity type code."""
    return INDIVIDUAL if str(entity_type_code) == "1" else ORGANIZATIONAL


_STATES = (
    ("AZ", "Tucson", "85704"),
    ("CO", "Denver", "80202"),
    ("MD", "Baltimore", "21201"),
    ("TX", "Harlingen", "78550"),
)
_TAXONOMIES = ("207Q00000X", "208D00000X", "261QP2300X")


def generate_record(
    seed: int, index: int, entity_type_code: str | None = None
) -> dict[str, object]:
    """Generate one code-defined NPPES provider record.

    The NPPES entity contract is deliberately declared here rather than
    inferred from an external sample.  Entity type ``1`` is individual and
    entity type ``2`` is organizational.
    """
    randomizer = Random(seed * 1_000_003 + index)
    faker = Faker("en_US")
    faker.seed_instance(seed * 1_000_003 + index)
    code = entity_type_code or ("1" if index % 2 == 0 else "2")
    state, city, postal = randomizer.choice(_STATES)
    first = faker.first_name().upper() if code == "1" else ""
    middle = faker.first_name()[0].upper() if code == "1" else ""
    last = faker.last_name().upper() if code == "1" else f"{faker.company().upper()} MEDICAL GROUP"
    taxonomy = randomizer.choice(_TAXONOMIES)
    record: dict[str, object] = {
        "NPI": _npi(index),
        "ENTITY_TYPE_CODE": code,
        "ENTITY_TYPE_DESCRIPTION": "Individual" if code == "1" else "Organization",
        "PROVIDER_ORGANIZATION_NAME_LEGAL_BUSINESS_NAME": last if code == "2" else "",
        "PROVIDER_FIRST_NAME": first,
        "PROVIDER_MIDDLE_NAME": middle,
        "PROVIDER_LAST_NAME_LEGAL_NAME": last if code == "1" else "",
        "PROVIDER_NAME_PREFIX_TEXT": "",
        "PROVIDER_NAME_SUFFIX_TEXT": "",
        "PROVIDER_CREDENTIAL_TEXT": "MD" if code == "1" else "",
        "PROVIDER_OTHER_CREDENTIAL_TEXT": "",
        "PROVIDER_GENDER_CODE": randomizer.choice(("M", "F")) if code == "1" else "",
        "PROVIDER_ENUMERATION_DATE": _date(
            date(2018, 1, 1) + timedelta(days=randomizer.randrange(2500))
        ),
        "LAST_UPDATE_DATE": _date(date(2023, 1, 1) + timedelta(days=randomizer.randrange(900))),
        "CERTIFICATION_DATE": faker.date_between(start_date="-10y", end_date="today").strftime(
            "%m/%d/%Y"
        ),
        "NPI_DEACTIVATION_DATE": "",
        "NPI_DEACTIVATION_REASON_CODE": "",
        "NPI_REACTIVATION_DATE": "",
        "REPLACEMENT_NPI": "",
        "IS_SOLE_PROPRIETOR": "Y" if code == "1" and randomizer.choice((True, False)) else "N",
        "IS_ORGANIZATION_SUBPART": "Y" if code == "2" and randomizer.choice((True, False)) else "N",
        "EMPLOYER_IDENTIFICATION_NUMBER_EIN": _digits(randomizer, 9) if code == "2" else "",
        "PARENT_ORGANIZATION_LBN": "",
        "PARENT_ORGANIZATION_TIN": "",
        "AUTHORIZED_OFFICIAL_FIRST_NAME": faker.first_name().upper() if code == "2" else "",
        "AUTHORIZED_OFFICIAL_MIDDLE_NAME": "",
        "AUTHORIZED_OFFICIAL_LAST_NAME": faker.last_name().upper() if code == "2" else "",
        "AUTHORIZED_OFFICIAL_TITLE_OR_POSITION": "OWNER" if code == "2" else "",
        "AUTHORIZED_OFFICIAL_NAME_PREFIX_TEXT": "",
        "AUTHORIZED_OFFICIAL_NAME_SUFFIX_TEXT": "",
        "AUTHORIZED_OFFICIAL_CREDENTIAL_TEXT": "",
        "AUTHORIZED_OFFICIAL_TELEPHONE_NUMBER": _digits(randomizer, 10) if code == "2" else "",
        "PROVIDER_FIRST_LINE_BUSINESS_MAILING_ADDRESS": (
            f"{randomizer.randrange(100, 9999)} Main St"
        ),
        "PROVIDER_SECOND_LINE_BUSINESS_MAILING_ADDRESS": "",
        "PROVIDER_BUSINESS_MAILING_ADDRESS_CITY_NAME": city,
        "PROVIDER_BUSINESS_MAILING_ADDRESS_STATE_NAME": state,
        "PROVIDER_BUSINESS_MAILING_ADDRESS_POSTAL_CODE": postal,
        "PROVIDER_BUSINESS_MAILING_ADDRESS_COUNTRY_CODE": "US",
        "PROVIDER_BUSINESS_MAILING_ADDRESS_TELEPHONE_NUMBER": _digits(randomizer, 10),
        "PROVIDER_BUSINESS_MAILING_ADDRESS_FAX_NUMBER": _digits(randomizer, 10),
        "PROVIDER_FIRST_LINE_BUSINESS_PRACTICE_LOCATION_ADDRESS": "",
        "PROVIDER_SECOND_LINE_BUSINESS_PRACTICE_LOCATION_ADDRESS": "",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_CITY_NAME": "",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_STATE_NAME": "",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_POSTAL_CODE": "",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_COUNTRY_CODE": "US",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_TELEPHONE_NUMBER": "",
        "PROVIDER_BUSINESS_PRACTICE_LOCATION_ADDRESS_FAX_NUMBER": "",
        "PROVIDER_OTHER_FIRST_NAME": "",
        "PROVIDER_OTHER_MIDDLE_NAME": "",
        "PROVIDER_OTHER_LAST_NAME": "",
        "PROVIDER_OTHER_NAME_PREFIX_TEXT": "",
        "PROVIDER_OTHER_NAME_SUFFIX_TEXT": "",
        "PROVIDER_OTHER_LAST_NAME_TYPE_CODE": "",
        "PROVIDER_OTHER_ORGANIZATION_NAME": "",
        "PROVIDER_OTHER_ORGANIZATION_NAME_TYPE_CODE": "",
        "HEALTHCARE_PROVIDER": [
            {
                "INDEX": 0,
                "TAXONOMY_CODE": taxonomy,
                "PRIMARY_TAXONOMY_SWITCH": "Y",
                "LICENSE_NUMBER": _digits(randomizer, 8),
                "LICENSE_NUMBER_STATE_CODE": state,
            }
        ],
        "OTHER_PROVIDER_IDENTIFIER": [],
        "HEALTHCARE_PROVIDER_TAXONOMY_GROUP": [],
        "ENDPOINT": [],
        "OTHER_NAME": [],
        "PRACTICE_LOCATION": [],
        "PAYER_PLATFORM": "CHC-QNXT",
        "PAYER": "CHC",
        "CLIENT_DATA_PLATFORM": "QNXT",
        "INGESTION_DATE": _date(date(2024, 1, 1) + timedelta(days=index)),
        "INGESTION_EPOCH": 1723070801 + index,
        "ROWID": deterministic_uuid4(seed + index, "provider-nppes"),
        "PUBLISHER_NAME": "client_provider_nppes",
    }
    return record


def generate_record_from_cdf(cdf: Mapping[str, object], index: int, seed: int) -> dict[str, object]:
    """Generate a type-specific NPPES record linked to one CDF provider."""
    code = "1" if cdf.get("CP_PROVIDER_RECORD_TYPE") == "I" else "2"
    record = generate_record(seed, index, code)
    record["NPI"] = str(cdf.get("CP_PROVIDER_NPI", _npi(index)))
    record["PROVIDER_FIRST_NAME"] = cdf.get("CP_PROVIDER_FIRST_NAME", "") if code == "1" else ""
    record["PROVIDER_MIDDLE_NAME"] = cdf.get("CP_PROVIDER_MIDDLE_NAME", "") if code == "1" else ""
    record["PROVIDER_LAST_NAME_LEGAL_NAME"] = (
        cdf.get("CP_PROVIDER_LAST_NAME", "") if code == "1" else ""
    )
    record["PROVIDER_ORGANIZATION_NAME_LEGAL_BUSINESS_NAME"] = (
        cdf.get("CP_PROVIDER_LAST_NAME", cdf.get("CP_PROVIDER_BILLING_GROUP_NAME", ""))
        if code == "2"
        else ""
    )
    record["PROVIDER_NAME_SUFFIX_TEXT"] = cdf.get("CP_PROVIDER_NAME_SUFFIX", "")
    record["PROVIDER_ENUMERATION_DATE"] = cdf.get("CP_PROVIDER_RECORD_START_DATE", "")
    addresses = cdf.get("CP_PROVIDER_ADDRESSES")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], Mapping):
        address = addresses[0]
        record["PROVIDER_FIRST_LINE_BUSINESS_MAILING_ADDRESS"] = address.get(
            "CP_PROVIDER_ADDRESS_01", ""
        )
        record["PROVIDER_SECOND_LINE_BUSINESS_MAILING_ADDRESS"] = address.get(
            "CP_PROVIDER_ADDRESS_02", ""
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_CITY_NAME"] = address.get("CP_PROVIDER_CITY", "")
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_STATE_NAME"] = address.get(
            "CP_PROVIDER_STATE", ""
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_POSTAL_CODE"] = str(
            address.get("CP_PROVIDER_ZIP", "")
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_TELEPHONE_NUMBER"] = address.get(
            "CP_PROVIDER_PHONE", ""
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_FAX_NUMBER"] = address.get("CP_PROVIDER_FAX", "")
    taxonomy = str(cdf.get("CP_PROVIDER_TAXONOMY_CODE", ""))
    if taxonomy and isinstance(record["HEALTHCARE_PROVIDER"], list):
        record["HEALTHCARE_PROVIDER"][0]["TAXONOMY_CODE"] = taxonomy
    return record


def generate_records(count: int, seed: int) -> list[dict[str, object]]:
    """Generate standalone NPPES records with unique NPIs."""
    if count < 1:
        raise ValueError("NPPES count must be at least 1")
    return [generate_record(seed, index) for index in range(count)]


def _npi(index: int) -> str:
    return f"9{index + 1:09d}"


def _digits(randomizer: Random, length: int) -> str:
    return "".join(str(randomizer.randrange(10)) for _ in range(length))


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")
