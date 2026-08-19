"""Generate NPPES-to-provider-CDF matching fixtures.

The NPPES sample is the schema authority. Provider CDF rows are produced by
the existing provider generator, then matched rows in the updated copy are
overlaid with the supported NPPES provider fields.
"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Mapping

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
    if count < 1:
        raise ValueError("count must be at least 1")
    if unmatched_count < 0:
        raise ValueError("unmatched_count cannot be negative")
    templates = _read_nppes_objects(sample_path)
    if not templates:
        raise ValueError(f"NPPES sample {sample_path} contains no records")
    nppes_records = _generate_nppes_records(templates, count, seed)
    nppes_by_npi = {str(record["NPI"]): record for record in nppes_records}
    cdf_records = [
        _cdf_record(str(nppes["NPI"]), seed, index) for index, nppes in enumerate(nppes_records)
    ]
    cdf_records.extend(
        _cdf_record(_unmatched_npi(index), seed, count + index) for index in range(unmatched_count)
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


def _generate_nppes_records(
    templates: list[dict[str, object]], count: int, seed: int
) -> list[dict[str, object]]:
    if not templates:
        raise ValueError("NPPES sample contains no records")
    return [_nppes_record(templates[index % len(templates)], index, seed) for index in range(count)]


def _cdf_record(npi: str, seed: int, index: int) -> dict[str, object]:
    headers = load_client_headers("chc", "provider")
    values = load_client_values("chc", "provider")
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
