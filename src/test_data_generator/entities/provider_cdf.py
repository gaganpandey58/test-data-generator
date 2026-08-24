"""Generate code-defined NPPES-to-provider-CDF matching fixtures."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from test_data_generator.configuration.profiles import load_client_headers, load_client_values
from test_data_generator.entities.provider import generate_record
from test_data_generator.entities.provider_nppes import generate_record_from_cdf, generate_records
from test_data_generator.layouts import project_record


def generate_provider_cdf(
    output_directory: Path, count: int = 10, unmatched_count: int = 2, seed: int = 20260805
) -> dict[str, Path]:
    """Generate linked ``provider_nppes`` and ``provider_cdf`` files."""
    return generate_linked_provider_fixtures(output_directory, count, unmatched_count, seed)


def generate_linked_provider_fixtures(
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
    resolved_headers = client_headers or load_client_headers("chc", "provider")
    resolved_values = client_values or load_client_values("chc", "provider")
    cdf_records = [
        _set_linked_record_type(
            _cdf_record(_nppes_npi(index), seed, index, resolved_headers, resolved_values), index
        )
        for index in range(nppes_count)
    ]
    nppes_records = [
        generate_record_from_cdf(cdf, index, seed) for index, cdf in enumerate(cdf_records)
    ]
    cdf_records.extend(
        _cdf_record(
            _unmatched_npi(index), seed, nppes_count + index, resolved_headers, resolved_values
        )
        for index in range(additional_cdf_count)
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "provider_nppes": output_directory / "provider_nppes.jsonl",
        "provider_cdf": output_directory / "provider_cdf.jsonl",
    }
    _write_jsonl(paths["provider_nppes"], nppes_records)
    _write_jsonl(paths["provider_cdf"], cdf_records)
    return paths


def generate_nppes_file(output_path: Path, count: int, seed: int) -> Path:
    """Generate one configured NPPES JSONL file from the code-defined entity."""
    records = generate_records(count, seed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_path, records)
    return output_path


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


def _set_linked_record_type(record: dict[str, object], index: int) -> dict[str, object]:
    """Ensure linked fixtures exercise both individual and organizational paths."""
    individual = index % 2 == 0
    record["CP_PROVIDER_RECORD_TYPE"] = "1" if individual else "2"
    if individual:
        first = str(record.get("CP_PROVIDER_FIRST_NAME", "")).strip() or f"TEST{index}"
        last = str(record.get("CP_PROVIDER_LAST_NAME", "")).strip() or f"PROVIDER{index}"
        middle = str(record.get("CP_PROVIDER_MIDDLE_NAME", "")).strip()
        record["CP_PROVIDER_FIRST_NAME"] = first
        record["CP_PROVIDER_MIDDLE_NAME"] = middle
        record["CP_PROVIDER_LAST_NAME"] = last
        record["CP_PROVIDER_FULL_NAME"] = " ".join(part for part in (first, middle, last) if part)
    else:
        organization = str(record.get("CP_PROVIDER_LAST_NAME", "")).strip() or f"TEST GROUP {index}"
        record["CP_PROVIDER_FIRST_NAME"] = ""
        record["CP_PROVIDER_MIDDLE_NAME"] = ""
        record["CP_PROVIDER_LAST_NAME"] = organization
        record["CP_PROVIDER_FULL_NAME"] = organization
    return record


def _apply_nppes_update(
    record: dict[str, object], nppes: dict[str, object] | None
) -> dict[str, object]:
    """Apply populated NPPES values only to fields already present in the CDF row."""
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
    _set_existing(updated, "CP_PROVIDER_FULL_NAME", _nppes_full_name(nppes))
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
