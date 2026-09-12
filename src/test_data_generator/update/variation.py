"""Safe automatic variation for paired matching fixtures.

Variation is deliberately narrower than a normal UPDATE operation.  It only
touches populated scalar fields that are outside every matching method and all
known identity, relationship, structural, financial, temporal, and derived
field families.  Every candidate is schema-checked before it is accepted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from random import Random
from typing import Mapping

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import (
    _changed_value,
    _load_profile_schema,
    _normalize_fields,
)
from test_data_generator.update.synchronization import (
    synchronization_field_closure,
    synchronize_record,
)

_MISSING = object()

_ENVELOPE_FIELDS = frozenset(
    {
        "INGESTION_DATE",
        "INGESTION_EPOCH",
        "ROWID",
        "PAYER",
        "PAYER_PLATFORM",
        "PRODUCT",
        "GDF_VERSION",
        "FILE_TYPE",
        "DATA_CATEGORY",
        "LOB",
        "PUBLISHER_NAME",
        "CLIENT_DATA_PLATFORM",
    }
)

_IDENTITY_MARKERS = (
    "_ID",
    "ID_",
    "NPI",
    "SSN",
    "TIN",
    "EIN",
    "HICN",
    "MEDICARE",
    "MEDICAID",
    "LICENSE_NUMBER",
    "CONTROL_NUMBER",
    "REFERENCE_IDENTIFICATION",
    "NUMBER",
    "SERIAL",
    "INDEX",
    "_DEA_",
)

_BUSINESS_SENSITIVE_MARKERS = (
    "AMOUNT",
    "DATE",
    "EPOCH",
    "TIMESTAMP",
    "PRODUCED_AT",
    "_AT",
    "_CODE",
    "_TYPE",
    "_STATUS",
    "_INDICATOR",
    "_QUALIFIER",
    "_FLAG",
    "_METHOD",
    "_CATEGORY",
    "_CLASSIFICATION",
    "_COUNT",
    "NUMBER_OF_",
    "_UNITS",
    "_WEIGHT",
    "_SCORE",
    "_SIZE",
    "_PREMIUM",
    "_RATE",
    "_PERCENT",
    "PERCENTAGE",
    "GENDER",
    "BIRTH",
    "DEATH",
    "DIAGNOSIS_POINTER",
)

_SAFE_VALUE_MARKERS = (
    "FIRST_NAME",
    "MIDDLE_NAME",
    "LAST_NAME",
    "ORGANIZATION_NAME",
    "GROUP_NAME",
    "FACILITY_NAME",
    "OTHER_PAYER_NAME",
    "ADDRESS",
    "CITY",
    "STATE",
    "ZIP",
    "POSTAL",
    "COUNTY",
    "REGION",
    "COUNTRY",
    "PHONE",
    "FAX",
    "EMAIL",
    "TITLE",
    "POSITION",
    "CREDENTIAL",
    "NOTE_TEXT",
    "DESCRIPTION",
    "CUSTOM_FIELD",
)


@dataclass(frozen=True)
class VariationResult:
    """One varied record plus the direct and synchronized fields it changed."""

    record: dict[str, object]
    applied_fields: tuple[str, ...]
    synchronized_fields: tuple[str, ...]


def apply_safe_variation(
    record: Mapping[str, object],
    rules: EntityRules,
    requested_count: int,
    seed: int,
    protected_fields: set[str],
) -> VariationResult:
    """Apply exactly ``requested_count`` safe, schema-preserving field changes.

    Selection is random but reproducible from the run/case seed.  Explicit
    scenario fields and their synchronization relationship closure are always
    protected.  A shortfall is an error because silently applying fewer fields
    would make the emitted fixture metadata misleading.
    """
    current = deepcopy(dict(record))
    if requested_count == 0:
        return VariationResult(current, (), ())

    matching_fields = {field for method in rules.methods for field in method.fields}
    normalized_protected = set(_normalize_fields(tuple(protected_fields), rules.fields))
    protected = synchronization_field_closure(
        current,
        normalized_protected.union(matching_fields, rules.keys),
    )
    candidates = [
        field
        for field in sorted(_scalar_field_names(current))
        if _eligible_field(current, field, protected)
        and not synchronization_field_closure(current, {field}).intersection(protected)
    ]
    Random(seed).shuffle(candidates)

    baseline_errors = _schema_errors(rules, current)
    applied: list[str] = []
    synchronized: list[str] = []
    for field in candidates:
        if len(applied) == requested_count:
            break
        previous = current
        candidate = deepcopy(previous)
        old_value = _lookup(candidate, field)
        if old_value is _MISSING:
            continue
        try:
            new_value = _changed_value(old_value, field, Random(0), rules.profile)
        except (TypeError, ValueError):
            continue
        if new_value == old_value or not _replace(candidate, field, new_value):
            continue
        candidate_synchronized = synchronize_record(previous, candidate, (field,))
        if any(_values(previous, name) != _values(candidate, name) for name in protected):
            continue
        if _schema_errors(rules, candidate) != baseline_errors:
            continue
        current = candidate
        applied.append(field)
        synchronized.extend(candidate_synchronized)

    if len(applied) != requested_count:
        raise ValueError(
            f"Safe variation requested {requested_count} field(s) for entity "
            f"{rules.entity!r}, but only {len(applied)} schema-safe non-matching "
            "field(s) could be changed"
        )
    return VariationResult(
        current,
        tuple(applied),
        tuple(dict.fromkeys(synchronized)),
    )


def _eligible_field(record: Mapping[str, object], field: str, protected: set[str]) -> bool:
    """Return whether one emitted scalar is safe for incidental variation."""
    if field in protected or field in _ENVELOPE_FIELDS or field.startswith("cotiviti."):
        return False
    upper = field.upper()
    if upper.endswith("_FULL_NAME") or upper == "ENTITY_TYPE_DESCRIPTION":
        return False
    if any(marker in upper for marker in _IDENTITY_MARKERS):
        return False
    if any(marker in upper for marker in _BUSINESS_SENSITIVE_MARKERS):
        return False
    if upper.startswith("IS_") or not any(marker in upper for marker in _SAFE_VALUE_MARKERS):
        return False
    value = _lookup(record, field)
    return (
        value is not _MISSING
        and value is not None
        and not isinstance(value, (Mapping, list))
        and (not isinstance(value, str) or bool(value.strip()))
    )


def _scalar_field_names(record: Mapping[str, object]) -> set[str]:
    result: set[str] = set()
    for field, value in record.items():
        if isinstance(value, Mapping):
            result.update(_scalar_field_names(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    result.update(_scalar_field_names(item))
        else:
            result.add(field)
    return result


def _lookup(record: Mapping[str, object], field: str) -> object:
    if field in record:
        return record[field]
    for value in record.values():
        if isinstance(value, Mapping):
            found = _lookup(value, field)
            if found is not _MISSING:
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    found = _lookup(item, field)
                    if found is not _MISSING:
                        return found
    return _MISSING


def _replace(record: dict[str, object], field: str, replacement: object) -> bool:
    if field in record:
        record[field] = replacement
        return True
    for value in record.values():
        if isinstance(value, dict) and _replace(value, field, replacement):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _replace(item, field, replacement):
                    return True
    return False


def _values(record: Mapping[str, object], field: str) -> tuple[object, ...]:
    result: list[object] = []
    for name, value in record.items():
        if name == field:
            result.append(value)
        if isinstance(value, Mapping):
            result.extend(_values(value, field))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    result.extend(_values(item, field))
    return tuple(result)


def _schema_errors(
    rules: EntityRules, record: Mapping[str, object]
) -> frozenset[tuple[object, ...]]:
    """Return stable schema-error identities for before/after comparison."""
    # NPPES intentionally has two source shapes and no project JSON Schema.
    # Its rules reuse the Provider profile only for value-generation helpers,
    # not for CDF schema validation.
    if rules.allow_absent_fields:
        return frozenset()
    schema = _load_profile_schema(rules.profile)
    if schema is None:
        return frozenset()
    validator = Draft202012Validator(schema)
    return frozenset(
        (
            tuple(error.absolute_path),
            tuple(error.absolute_schema_path),
            error.validator,
        )
        for error in validator.iter_errors(record)
    )
