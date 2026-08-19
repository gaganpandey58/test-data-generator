"""Generate NPPES-to-provider-CDF matching fixtures.

The NPPES sample is the schema authority. Provider CDF rows are produced by
the existing provider generator, then matched rows in the updated copy are
overlaid with the supported NPPES provider fields.
"""

import json
from copy import deepcopy
from pathlib import Path
from random import Random
from typing import Mapping

from faker import Faker

from test_data_generator.configuration.profiles import load_client_headers, load_client_values
from test_data_generator.core.identifiers import deterministic_uuid4
from test_data_generator.entities.provider import generate_record
from test_data_generator.layouts import project_record


def generate_provider_cdf(
    sample_path: Path,
    output_directory: Path,
    count: int = 10,
    unmatched_count: int = 2,
    seed: int = 20260805,
) -> dict[str, Path]:
    """Generate ``provider_nppes``, ``provider_cdf``, and updated CDF files.

    ``count`` NPPES records become matching CDF records. ``unmatched_count``
    additional CDF records receive unique NPIs absent from NPPES and remain
    byte-for-byte equal in the updated logical records.
    """
    return generate_linked_provider_fixtures(
        sample_path, output_directory, count, unmatched_count, seed
    )


def generate_linked_provider_fixtures(
    sample_path: Path,
    output_directory: Path,
    nppes_count: int,
    additional_cdf_count: int = 0,
    seed: int = 20260805,
    client_headers: Mapping[str, object] | None = None,
    client_values: Mapping[str, object] | None = None,
) -> dict[str, Path]:
    """Generate linked NPPES/CDF rows plus configurable CDF-only rows."""
    if nppes_count < 1:
        raise ValueError("nppes_count must be at least 1")
    if additional_cdf_count < 0:
        raise ValueError("additional_cdf_count cannot be negative")
    templates = _read_nppes_objects(sample_path)
    if not templates:
        raise ValueError(f"NPPES sample {sample_path} contains no records")
    resolved_headers = client_headers or load_client_headers("chc", "provider")
    resolved_values = client_values or load_client_values("chc", "provider")
    cdf_records = [
        _cdf_record(_nppes_npi(index), seed, index, resolved_headers, resolved_values)
        for index in range(nppes_count)
    ]
    nppes_records = [
        _nppes_record_from_cdf(templates[index % len(templates)], cdf, index, seed)
        for index, cdf in enumerate(cdf_records)
    ]
    nppes_by_npi = {str(record["NPI"]): record for record in nppes_records}
    cdf_records.extend(
        _cdf_record(
            _unmatched_npi(index),
            seed,
            nppes_count + index,
            resolved_headers,
            resolved_values,
        )
        for index in range(additional_cdf_count)
    )
    updated_records = [
        _apply_nppes_update(record, nppes_by_npi.get(str(record.get("CP_PROVIDER_NPI"))))
        for record in cdf_records
    ]
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "provider_nppes": output_directory / "provider_nppes.jsonl",
        "provider_cdf": output_directory / "provider_cdf.jsonl",
        "provider_cdf_updated": output_directory / "provider_cdf_updated.jsonl",
    }
    _write_jsonl(paths["provider_nppes"], nppes_records)
    _write_jsonl(paths["provider_cdf"], cdf_records)
    _write_jsonl(paths["provider_cdf_updated"], updated_records)
    return paths


def generate_nppes_file(sample_path: Path, output_path: Path, count: int, seed: int) -> Path:
    """Generate one configured NPPES JSONL file from the checked-in sample."""
    if count < 1:
        raise ValueError("NPPES count must be at least 1")
    records = _generate_nppes_records(_read_nppes_objects(sample_path), count, seed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_path, records)
    return output_path


def _read_nppes_objects(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read NPPES sample {path}") from error
    decoder = json.JSONDecoder()
    records: list[dict[str, object]] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        try:
            value, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError as error:
            raise ValueError(f"NPPES sample {path} contains invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"NPPES sample {path} must contain JSON objects")
        records.append(value)
    return records


def _nppes_record(template: Mapping[str, object], index: int, seed: int) -> dict[str, object]:
    record = deepcopy(dict(template))
    record["NPI"] = _nppes_npi(index)
    record["ROWID"] = deterministic_uuid4(seed + index, "provider-nppes")
    return record


def _nppes_record_from_cdf(
    template: Mapping[str, object], cdf: Mapping[str, object], index: int, seed: int
) -> dict[str, object]:
    """Format one logical CDF provider using the NPPES sample shape."""
    record = deepcopy(dict(template))
    randomizer = Random(seed * 1_000_003 + index)
    faker = Faker("en_US")
    faker.seed_instance(seed * 1_000_003 + index)
    _randomize_nppes_values(record, randomizer, faker)
    record["NPI"] = str(cdf.get("CP_PROVIDER_NPI", _nppes_npi(index)))
    individual = cdf.get("CP_PROVIDER_RECORD_TYPE") == "I"
    record["ENTITY_TYPE_CODE"] = "1" if individual else "2"
    record["ENTITY_TYPE_DESCRIPTION"] = "Individual" if individual else "Organization"
    record["PROVIDER_FIRST_NAME"] = cdf.get("CP_PROVIDER_FIRST_NAME", "")
    record["PROVIDER_MIDDLE_NAME"] = cdf.get("CP_PROVIDER_MIDDLE_NAME", "")
    record["PROVIDER_LAST_NAME_LEGAL_NAME"] = cdf.get("CP_PROVIDER_LAST_NAME", "")
    if not individual:
        record["PROVIDER_ORGANIZATION_NAME_LEGAL_BUSINESS_NAME"] = cdf.get(
            "CP_PROVIDER_LAST_NAME",
            cdf.get("CP_PROVIDER_BILLING_GROUP_NAME", cdf.get("CP_PROVIDER_FULL_NAME", "")),
        )
        record["PROVIDER_FIRST_NAME"] = ""
        record["PROVIDER_MIDDLE_NAME"] = ""
        record["PROVIDER_LAST_NAME_LEGAL_NAME"] = ""
    record["PROVIDER_NAME_SUFFIX_TEXT"] = cdf.get("CP_PROVIDER_NAME_SUFFIX", "")
    record["PROVIDER_ENUMERATION_DATE"] = cdf.get("CP_PROVIDER_RECORD_START_DATE", "")
    record["LAST_UPDATE_DATE"] = cdf.get("CP_PROVIDER_SOURCE_UPDATED_AT", "")
    taxonomy = str(cdf.get("CP_PROVIDER_TAXONOMY_CODE", ""))
    _set_primary_taxonomy(record, taxonomy)
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
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_POSTAL_CODE"] = (
            f"{address.get('CP_PROVIDER_ZIP', '')}{address.get('CP_PROVIDER_ZIP_PLUS_FOUR', '')}"
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_TELEPHONE_NUMBER"] = address.get(
            "CP_PROVIDER_PHONE", ""
        )
        record["PROVIDER_BUSINESS_MAILING_ADDRESS_FAX_NUMBER"] = address.get("CP_PROVIDER_FAX", "")
    record["ROWID"] = deterministic_uuid4(seed + index, "provider-nppes")
    return record


def _randomize_nppes_values(record: dict[str, object], randomizer: Random, faker: Faker) -> None:
    """Refresh non-structural sample values without changing its shape."""
    for key, value in list(record.items()):
        if isinstance(value, dict):
            _randomize_nppes_values(value, randomizer, faker)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _randomize_nppes_values(item, randomizer, faker)
        elif (
            isinstance(value, str)
            and value
            and key
            not in {
                "PAYER_PLATFORM",
                "PAYER",
                "CLIENT_DATA_PLATFORM",
                "PUBLISHER_NAME",
                "PRIMARY_TAXONOMY_SWITCH",
            }
        ):
            upper = key.upper()
            if upper == "CERTIFICATION_DATE":
                record[key] = faker.date_between(start_date="-10y", end_date="today").strftime(
                    "%m/%d/%Y"
                )
            elif "DATE" in upper:
                record[key] = faker.date_between(start_date="-10y", end_date="today").strftime(
                    "%Y%m%d"
                )
            elif "PHONE" in upper or "FAX" in upper or "TELEPHONE" in upper:
                record[key] = "".join(str(randomizer.randrange(10)) for _ in range(len(value)))
            elif "POSTAL_CODE" in upper:
                record[key] = "".join(str(randomizer.randrange(10)) for _ in range(len(value)))
            elif "NAME" in upper and "CODE" not in upper:
                record[key] = faker.word().upper()


def _set_primary_taxonomy(record: dict[str, object], taxonomy: str) -> None:
    values = record.get("HEALTHCARE_PROVIDER")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict) and value.get("PRIMARY_TAXONOMY_SWITCH") == "Y":
                value["TAXONOMY_CODE"] = taxonomy
                return


def _generate_nppes_records(
    templates: list[dict[str, object]], count: int, seed: int
) -> list[dict[str, object]]:
    if not templates:
        raise ValueError("NPPES sample contains no records")
    return [_nppes_record(templates[index % len(templates)], index, seed) for index in range(count)]


def _cdf_record(
    npi: str,
    seed: int,
    index: int,
    headers: Mapping[str, object] | None = None,
    values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    headers = headers or load_client_headers("chc", "provider")
    values = values or load_client_values("chc", "provider")
    record = generate_record(seed, index, {"provider": 1}, headers, values, "provider")
    projected = project_record(record, "provider")
    projected["CP_PROVIDER_NPI"] = npi
    if projected.get("CP_PRESCRIBING_PROVIDER_NPI") == record.get("CP_PROVIDER_NPI"):
        projected["CP_PRESCRIBING_PROVIDER_NPI"] = npi
    return projected


def _apply_nppes_update(
    record: dict[str, object], nppes: dict[str, object] | None
) -> dict[str, object]:
    updated = deepcopy(record)
    if nppes is None:
        return updated
    root_mapping = {
        "PROVIDER_FIRST_NAME": "CP_PROVIDER_FIRST_NAME",
        "PROVIDER_MIDDLE_NAME": "CP_PROVIDER_MIDDLE_NAME",
        "PROVIDER_LAST_NAME": "CP_PROVIDER_LAST_NAME",
        "PROVIDER_NAME_SUFFIX_TEXT": "CP_PROVIDER_NAME_SUFFIX",
        "PROVIDER_ENUMERATION_DATE": "CP_PROVIDER_RECORD_START_DATE",
        "LAST_UPDATE_DATE": "CP_PROVIDER_SOURCE_UPDATED_AT",
    }
    for source, target in root_mapping.items():
        _set_existing(updated, target, nppes.get(source, ""))
    full_name = _nppes_full_name(nppes)
    _set_existing(updated, "CP_PROVIDER_FULL_NAME", full_name)
    taxonomy = _primary_taxonomy(nppes)
    _set_existing(updated, "CP_PROVIDER_PRIMARY_SPECIALTY_CODE", taxonomy)
    _set_existing(updated, "CP_PROVIDER_TAXONOMY_CODE", taxonomy)
    _set_existing(
        updated, "CP_PROVIDER_STATE_LICENSE_NUMBER", _primary_value(nppes, "LICENSE_NUMBER")
    )
    _set_existing(
        updated,
        "CP_PROVIDER_STATE_LICENSE_STATE_CODE",
        _primary_value(nppes, "LICENSE_NUMBER_STATE_CODE"),
    )
    addresses = updated.get("CP_PROVIDER_ADDRESSES")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        address = addresses[0]
        source_mapping = {
            "PROVIDER_FIRST_LINE_BUSINESS_MAILING_ADDRESS": "CP_PROVIDER_ADDRESS_01",
            "PROVIDER_SECOND_LINE_BUSINESS_MAILING_ADDRESS": "CP_PROVIDER_ADDRESS_02",
            "PROVIDER_BUSINESS_MAILING_ADDRESS_CITY_NAME": "CP_PROVIDER_CITY",
            "PROVIDER_BUSINESS_MAILING_ADDRESS_STATE_NAME": "CP_PROVIDER_STATE",
            "PROVIDER_BUSINESS_MAILING_ADDRESS_TELEPHONE_NUMBER": "CP_PROVIDER_PHONE",
            "PROVIDER_BUSINESS_MAILING_ADDRESS_FAX_NUMBER": "CP_PROVIDER_FAX",
        }
        for source, target in source_mapping.items():
            _set_existing(address, target, nppes.get(source, ""))
        postal = str(nppes.get("PROVIDER_BUSINESS_MAILING_ADDRESS_POSTAL_CODE", ""))
        _set_existing(address, "CP_PROVIDER_ZIP", postal[:5])
        _set_existing(address, "CP_PROVIDER_ZIP_PLUS_FOUR", postal[5:9])
    return updated


def _primary_taxonomy(record: Mapping[str, object]) -> object:
    return _primary_value(record, "TAXONOMY_CODE")


def _primary_value(record: Mapping[str, object], key: str) -> object:
    values = record.get("HEALTHCARE_PROVIDER")
    if isinstance(values, list):
        for value in values:
            if isinstance(value, Mapping) and value.get("PRIMARY_TAXONOMY_SWITCH") == "Y":
                return value.get(key, "")
    return ""


def _nppes_full_name(record: Mapping[str, object]) -> str:
    organization = str(record.get("PROVIDER_ORGANIZATION_NAME_LEGAL_BUSINESS_NAME", "")).strip()
    if organization:
        return organization
    return " ".join(
        str(record.get(field, "")).strip()
        for field in ("PROVIDER_FIRST_NAME", "PROVIDER_MIDDLE_NAME", "PROVIDER_LAST_NAME")
        if str(record.get(field, "")).strip()
    )


def _set_existing(record: dict[str, object], field: str, value: object) -> None:
    if field in record:
        record[field] = value


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _nppes_npi(index: int) -> str:
    return f"9{index + 1:09d}"


def _unmatched_npi(index: int) -> str:
    return f"8{index + 1:09d}"
