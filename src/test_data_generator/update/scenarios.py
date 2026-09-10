"""Scenario resolution and deterministic record mutation."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from random import Random
from typing import Mapping

from faker import Faker

from test_data_generator.core.identifiers import valid_ein, valid_npi, valid_phone_number, valid_ssn
from test_data_generator.update.rules import EntityRules
from test_data_generator.update.synchronization import synchronize_record


class OperationType(StrEnum):
    """Generic record mutation operations."""

    UPDATE = "UPDATE"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    INVALID = "INVALID"
    WEIGHT_CHANGE = "WEIGHT_CHANGE"
    DUPLICATE = "DUPLICATE"


class ExpectedOutcome(StrEnum):
    """Expected result of evaluating an incoming record against a method."""

    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"


class FailureMode(StrEnum):
    """Controlled way for a negative matching fixture to fail."""

    MANDATORY_BREAK_EXACT = "MANDATORY_BREAK_EXACT"
    MANDATORY_BREAK_BOUNDARY = "MANDATORY_BREAK_BOUNDARY"
    INVALID_VALUE = "INVALID_VALUE"
    MISSING_VALUE = "MISSING_VALUE"
    WEIGHT_MISS = "WEIGHT_MISS"
    CROSS_METHOD_COLLISION = "CROSS_METHOD_COLLISION"


_UPDATE_PROTECTED_FIELDS = frozenset(
    {
        "CH_CLAIM_TYPE",
        "FILE_TYPE",
        "cotiviti.source_format",
    }
)

# The Claim GDF declares this field as an integer even when a source fixture
# happens to serialize the source tax identifier as text.  Payment uses the
# same logical field name but its 835 schema requires a string, so the rule is
# profile-specific rather than a global field-name exception.
_INTEGER_IDENTIFIER_FIELDS_BY_PROFILE = {
    "claim-professional": frozenset({"CH_RENDERING_PROVIDER_FEDERAL_TAX_ID"}),
    "claim-institutional": frozenset({"CH_RENDERING_PROVIDER_FEDERAL_TAX_ID"}),
}
_STATE_CODES = (
    "AK",
    "AL",
    "AR",
    "AZ",
    "CA",
    "CO",
    "CT",
    "DC",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
)

# These are source-domain code sets, not arbitrary text fallbacks.  The GDF
# schemas intentionally leave many clinical values as ``string`` because they
# are validated by the receiving application.  Updates must nevertheless keep
# those values in their healthcare code domains.
_ICD_DIAGNOSIS_CODES = (
    "I10",
    "E119",
    "E785",
    "J189",
    "M5450",
    "R0602",
    "S93401A",
)
_ICD_EXTERNAL_CAUSE_CODES = ("V892XXA", "W010XXA", "W19XXXA", "X580XXA", "Y92009")
_PROCEDURE_CODES = ("36415", "93000", "99213", "99214", "99223")
_REVENUE_CODES = ("0250", "0300", "0450", "0510", "0521")
_PROCEDURE_MODIFIERS = ("25", "59", "GP", "KX", "LT", "RT")
_TAXONOMY_CODES = ("207Q00000X", "208D00000X", "261QM2500X", "282N00000X")
_ADJUSTMENT_GROUP_CODES = ("CO", "CR", "OA", "PI", "PR")
_ADJUSTMENT_REASON_CODES = ("1", "2", "3", "45", "97")

_PROFILE_SCHEMA_PATHS = {
    "member": "member/member.schema.json",
    "provider": "provider/provider.schema.json",
    "claim-professional": "claim/claim.schema.json",
    "claim-institutional": "claim/claim.schema.json",
    "payment-professional": "payment/payment.schema.json",
    "payment-institutional": "payment/payment.schema.json",
}


def load_invalid_values(path: Path) -> dict[str, tuple[object, ...]]:
    """Load the shared field-name and field-type invalid-value catalog."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read invalid-value catalog {path}") from error
    values = raw.get("invalid_values") if isinstance(raw, dict) else None
    if not isinstance(values, dict):
        raise ValueError("Invalid-value catalog must contain an invalid_values object")
    return {
        str(field): tuple(items)
        for field, items in values.items()
        if isinstance(items, list) and items
    }


def _invalid_values_for(
    catalog: Mapping[str, tuple[object, ...]], field: str, profile: str
) -> tuple[object, ...]:
    """Resolve a catalog-only invalid value for a field.

    Exact field entries take precedence. When a field has no bespoke entry,
    a documented field-type entry in ``invalid-values.json`` is used. There
    is deliberately no inline/synthetic invalid-value fallback.
    """
    for key in (field, *_invalid_catalog_keys(field, profile)):
        values = catalog.get(key)
        if values:
            return tuple(
                value.replace("{field}", field) if isinstance(value, str) else value
                for value in values
            )
    raise ValueError(f"INVALID field {field!r} has no invalid-value catalog entry")


def _invalid_catalog_keys(field: str, profile: str) -> tuple[str, ...]:
    """Return catalog type keys ordered from business type to schema type."""
    upper = field.upper()
    keys: list[str] = []
    for marker, catalog_key in (
        ("NPI", "NPI"),
        ("SSN", "SSN"),
        ("HICN", "HICN"),
        ("EMAIL", "EMAIL"),
        ("PHONE", "PHONE"),
        ("ZIP", "ZIP"),
        ("DATE", "DATE"),
        ("AMOUNT", "AMOUNT"),
    ):
        if marker in upper:
            keys.append(catalog_key)
    if any(marker in upper for marker in ("CODE", "TYPE", "STATUS", "INDICATOR", "QUALIFIER")):
        keys.append("CODE")
    keys.extend(schema_type.upper() for schema_type in _schema_field_types(profile, field))
    keys.append("DEFAULT")
    return tuple(dict.fromkeys(keys))


@dataclass(frozen=True)
class UpdateRequest:
    """One normalized update request."""

    operation: OperationType
    fields: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    matching_method: str | None = None
    threshold: Decimal | None = None
    condition: str | None = None
    invalid_values: Mapping[str, tuple[object, ...]] | None = None
    expected_outcome: ExpectedOutcome | None = None
    failure_mode: FailureMode | None = None
    failure_field: str | None = None
    collision_method: str | None = None
    elasticity_boundary: str | None = None
    modifications: tuple["FieldModification", ...] = ()


@dataclass(frozen=True)
class FieldModification:
    """One explicitly scoped independent field operation."""

    operation: OperationType
    fields: tuple[str, ...]
    condition: str | None = None


@dataclass(frozen=True)
class ResolvedUpdate:
    """An update record and its explainable diff."""

    record: dict[str, object]
    changed_fields: tuple[str, ...]
    removed_fields: tuple[str, ...]
    invalidated_keys: tuple[str, ...]
    total_weight: Decimal
    threshold_relation: str
    expected_match: bool
    expected_apply: bool
    synchronized_fields: tuple[str, ...] = ()
    method_id: str | None = None
    expected_outcome: ExpectedOutcome | None = None
    failure_mode: FailureMode | None = None
    matched_methods: tuple[str, ...] = ()
    unexpected_methods: tuple[str, ...] = ()
    modification_plan: tuple[FieldModification, ...] = ()


def may_violate_schema(request: UpdateRequest) -> bool:
    """Return whether an explicit negative operation may break JSON Schema.

    Missing a required field, emptying a constrained value, or deliberately
    injecting an invalid value is the point of these QA fixtures.  The normal
    schema validator must not suppress that requested negative output.
    """
    operations = (request.operation, *(item.operation for item in request.modifications))
    return any(
        operation in {OperationType.MISSING, OperationType.EMPTY, OperationType.INVALID}
        for operation in operations
    )


def resolve_fields(
    request: UpdateRequest,
    rules: EntityRules,
    seed: int = 0,
    index: int = 0,
    available_fields: set[str] | None = None,
) -> tuple[str, ...]:
    """Resolve fields from the catalog and the selected output shape.

    The survivorship catalog can contain fields that are applicable to another
    profile of the same entity. An update may only mutate fields present in the
    actual generated record; otherwise a missing field could be added to an
    unrelated nested object and make the result fail schema validation.
    """
    known = rules.fields
    explicit_selection = bool(request.fields or request.include)
    if request.fields:
        selected = _normalize_fields(request.fields, known)
    elif request.include:
        selected = [field for field in _normalize_fields(request.include, known) if field in known]
    elif request.operation in {
        OperationType.UPDATE,
        OperationType.MISSING,
        OperationType.EMPTY,
        OperationType.INVALID,
    }:
        selected = [
            name
            for name in known
            if name not in rules.keys
            and _is_mutable_for_operation(name, request.operation)
            and (available_fields is None or name in available_fields)
        ]
        if request.operation == OperationType.MISSING:
            required = [name for name in selected if known[name].required]
            selected = required or selected
        selected = [Random(seed * 1_000_003 + index).choice(selected)] if selected else []
    else:
        selected = [
            name
            for name in known
            if name not in rules.keys and _is_mutable_for_operation(name, request.operation)
        ]
    if available_fields is not None:
        unavailable = [field for field in selected if field not in available_fields]
        if unavailable and explicit_selection:
            raise ValueError(f"Update field {unavailable[0]!r} is not present in generated record")
        selected = [field for field in selected if field in available_fields]
    excluded = set(_normalize_fields(request.exclude, known))
    selected = [field for field in selected if field not in excluded]
    if any(field not in known for field in selected):
        unknown = next(field for field in selected if field not in known)
        raise ValueError(f"Update selection contains an unknown field {unknown!r}")
    if any(field in rules.keys for field in selected) and not (
        request.operation in {OperationType.INVALID, OperationType.MISSING}
        or (request.operation == OperationType.UPDATE and explicit_selection)
    ):
        matching = next(field for field in selected if field in rules.keys)
        raise ValueError(
            f"Matching key {matching!r} requires an explicit UPDATE, INVALID, or MISSING operation"
        )
    if request.operation not in {OperationType.INVALID, OperationType.MISSING}:
        protected = next((field for field in selected if field in _UPDATE_PROTECTED_FIELDS), None)
        if protected is not None:
            raise ValueError(
                f"Structural discriminator {protected!r} may only be selected by INVALID "
                "or MISSING operation"
            )
    if not selected:
        raise ValueError("Operation resolved no fields")
    return tuple(dict.fromkeys(selected))


def _is_mutable_for_operation(field: str, operation: OperationType) -> bool:
    """Return whether a non-key field is safe for the requested mutation."""
    return (
        operation in {OperationType.INVALID, OperationType.MISSING}
        or field not in _UPDATE_PROTECTED_FIELDS
    )


def _normalize_fields(fields: tuple[str, ...], known: Mapping[str, object]) -> list[str]:
    """Split field lists and resolve human-entered aliases to catalog names."""
    result: list[str] = []
    canonical = {name.upper(): name for name in known}
    aliases = {
        "PROVIDER_NPI": "CP_PROVIDER_NPI",
        "RECORD_TYPE": "CP_PROVIDER_RECORD_TYPE",
    }
    for value in fields:
        for item in value.split(","):
            raw = item.strip()
            if not raw:
                continue
            direct = canonical.get(raw.upper())
            if direct is not None:
                result.append(direct)
                continue
            normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
            resolved = canonical.get(normalized)
            if resolved is None:
                alias = aliases.get(normalized)
                resolved = canonical.get(alias.upper()) if alias else None
                if resolved is None and alias is not None:
                    normalized = alias
            result.append(resolved or normalized)
    return result


def resolve_update(
    base: Mapping[str, object], request: UpdateRequest, rules: EntityRules, seed: int, index: int
) -> ResolvedUpdate:
    """Create one deterministic update from one base record."""
    if request.expected_outcome is not None:
        from test_data_generator.update.matching import resolve_match_fixture

        return resolve_match_fixture(base, request, rules, seed, index)
    if request.modifications:
        return _resolve_modification_plan(base, request, rules, seed, index)
    if request.matching_method is not None and request.operation in {
        OperationType.INVALID,
        OperationType.MISSING,
        OperationType.EMPTY,
    }:
        method = next(
            (item for item in rules.methods if item.name == request.matching_method),
            None,
        )
        if method is None:
            raise ValueError(f"Unknown matching method {request.matching_method!r}")
        selected_fields = resolve_fields(request, rules, seed, index, _field_names(base))
        protected = set(selected_fields).intersection(method.mandatory_fields)
        if protected:
            raise ValueError(
                f"{request.operation} on mandatory anchor {sorted(protected)[0]!r} "
                "requires expected_outcome=NO_MATCH"
            )
    selected = resolve_fields(request, rules, seed, index, _field_names(base))
    operation = request.operation
    if operation == OperationType.WEIGHT_CHANGE and not request.fields and not request.include:
        selection_threshold = _weight_threshold(request, rules)
        condition = request.condition
        if condition is None:
            raise ValueError("WEIGHT_CHANGE requires BELOW_LIMIT, AT_LIMIT, or ABOVE_LIMIT")
        selected = _select_weight_fields(
            rules,
            selection_threshold,
            condition,
            _field_names(base),
            request.matching_method,
        )
    original = deepcopy(dict(base))
    result = deepcopy(original)
    changed: list[str] = []
    removed: list[str] = []
    invalidated: list[str] = []
    randomizer = Random(seed * 1_000_003 + index * 97 + 41)
    if operation == OperationType.MISSING:
        for field in selected:
            if _remove_field(result, field):
                removed.append(field)
                if field in rules.keys:
                    invalidated.append(field)
    else:
        if operation == OperationType.EMPTY:
            for field in selected:
                old = _find_field(result, field)
                empty_value: object = None if old is None else "" if isinstance(old, str) else 0
                _replace_field(result, field, empty_value)
                if empty_value != old:
                    changed.append(field)
        elif operation == OperationType.INVALID:
            catalog = request.invalid_values or {}
            for field in selected:
                values = _invalid_values_for(catalog, field, rules.profile)
                _replace_field(result, field, randomizer.choice(values))
                changed.append(field)
                if field in rules.keys:
                    invalidated.append(field)
        elif operation != OperationType.DUPLICATE:
            for field in selected:
                old = _find_field(result, field)
                new: object = _changed_value(old, field, randomizer, rules.profile)
                _replace_field(result, field, new)
                if new != old:
                    changed.append(field)
    synchronized = synchronize_record(original, result, tuple(changed + removed))
    total = sum((rules.fields[field].weight for field in changed), Decimal("0"))
    threshold = _weight_threshold(request, rules)
    relation = _relation(total, threshold)
    condition = request.condition
    if (
        operation == OperationType.WEIGHT_CHANGE
        and condition == "BELOW_LIMIT"
        and relation != "below"
    ):
        raise ValueError("Selected fields do not produce a below-threshold update")
    if operation == OperationType.WEIGHT_CHANGE and condition == "AT_LIMIT" and relation != "equal":
        raise ValueError("Selected fields do not produce an at-threshold update")
    if (
        operation == OperationType.WEIGHT_CHANGE
        and condition == "ABOVE_LIMIT"
        and relation != "above"
    ):
        raise ValueError("Selected fields do not produce an above-threshold update")
    return ResolvedUpdate(
        record=result,
        changed_fields=tuple(changed),
        removed_fields=tuple(removed),
        invalidated_keys=tuple(invalidated),
        total_weight=total,
        threshold_relation=relation,
        expected_match=not invalidated,
        expected_apply=not (operation == OperationType.WEIGHT_CHANGE and condition == "ABOVE_LIMIT")
        and not invalidated,
        synchronized_fields=synchronized,
    )


def _resolve_modification_plan(
    base: Mapping[str, object],
    request: UpdateRequest,
    rules: EntityRules,
    seed: int,
    index: int,
) -> ResolvedUpdate:
    """Apply an ordered, explicit field-operation plan to one derived record.

    Direct entity scenarios can deliberately combine UPDATE, INVALID, MISSING,
    EMPTY, and WEIGHT_CHANGE without declaring a matching assertion.  Each
    operation is resolved through the existing schema-aware mutation path, so
    updates retain their normal format, synchronization, and invalid catalog
    behavior.
    """
    original = deepcopy(dict(base))
    current: Mapping[str, object] = original
    changed: list[str] = []
    removed: list[str] = []
    invalidated: list[str] = []
    synchronized: list[str] = []
    total = Decimal("0")
    relation = "below"
    expected_apply = True
    for position, modification in enumerate(request.modifications, start=1):
        nested_request = UpdateRequest(
            operation=modification.operation,
            fields=modification.fields,
            include=request.include,
            exclude=request.exclude,
            matching_method=request.matching_method,
            threshold=request.threshold,
            condition=modification.condition,
            invalid_values=request.invalid_values,
        )
        resolved = resolve_update(current, nested_request, rules, seed, index * 10_000 + position)
        current = resolved.record
        changed.extend(resolved.changed_fields)
        removed.extend(resolved.removed_fields)
        invalidated.extend(resolved.invalidated_keys)
        synchronized.extend(resolved.synchronized_fields)
        total += resolved.total_weight
        relation = resolved.threshold_relation
        expected_apply = expected_apply and resolved.expected_apply
    return ResolvedUpdate(
        record=dict(current),
        changed_fields=tuple(dict.fromkeys(changed)),
        removed_fields=tuple(dict.fromkeys(removed)),
        invalidated_keys=tuple(dict.fromkeys(invalidated)),
        total_weight=total,
        threshold_relation=relation,
        expected_match=not invalidated,
        expected_apply=expected_apply and not invalidated,
        synchronized_fields=tuple(dict.fromkeys(synchronized)),
        modification_plan=request.modifications,
    )


def _changed_value(value: object, field: str, randomizer: Random, profile: str = "") -> object:
    """Return a changed value without leaving the field's value domain.

    The update engine is used for every layout field, including fields that
    the JSON Schema represents only as a string.  Schema enums are therefore
    consulted first, then GDF/835 semantic code families are handled through
    shared code sets.  Only genuine free text reaches Faker's word fallback.
    """
    upper_field = field.upper()
    # The source schemas accept X for compatibility, but the configured
    # generator's realistic-data contract intentionally emits only M/F.
    if "GENDER" in upper_field and isinstance(value, str):
        values = tuple(candidate for candidate in ("F", "M") if candidate != value)
        return randomizer.choice(values)
    enum_candidate = _schema_enum_candidate(profile, field, value, randomizer)
    if enum_candidate is not None:
        return enum_candidate
    if upper_field in _INTEGER_IDENTIFIER_FIELDS_BY_PROFILE.get(profile, frozenset()):
        return int(valid_ein(randomizer))
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        code_candidate = _semantic_code_candidate(upper_field, str(value), randomizer)
        if code_candidate is not None and code_candidate.isdigit():
            return int(code_candidate) if isinstance(value, int) else float(code_candidate)
        if isinstance(value, int):
            int_candidate = randomizer.randrange(max(0, value - 100), value + 101)
            return int_candidate if int_candidate != value else value + 1
        float_candidate = round(randomizer.uniform(max(0, value - 100), value + 100), 2)
        return float_candidate if float_candidate != value else round(value + 1, 2)
    if isinstance(value, str):
        faker = Faker("en_US")
        faker.seed_instance(randomizer.randrange(1, 2**31 - 1))
        candidate: object
        if "NPI" in upper_field:
            candidate = valid_npi(randomizer)
        elif "SSN" in upper_field:
            candidate = valid_ssn(randomizer)
        elif "FEDERAL_TAX_ID" in upper_field or upper_field.endswith("_EIN"):
            candidate = valid_ein(randomizer)
        elif "PHONE" in upper_field or "FAX" in upper_field:
            candidate = valid_phone_number(randomizer)
        elif "EMAIL" in upper_field:
            candidate = faker.email()
        elif "FIRST_NAME" in upper_field:
            candidate = faker.first_name().upper()
        elif "LAST_NAME" in upper_field:
            candidate = faker.last_name().upper()
        elif "MIDDLE_NAME" in upper_field:
            candidate = faker.first_name()[0].upper()
        elif "FULL_NAME" in upper_field:
            candidate = faker.name().upper()
        elif upper_field.endswith("CLIENT_ROOT_CLAIM_ID"):
            candidate = _changed_root_claim_id(value, randomizer)
        elif (
            coded_candidate := _semantic_code_candidate(upper_field, value, randomizer)
        ) is not None:
            candidate = coded_candidate
        elif "PLACE_OF_SERVICE_CODE" in upper_field:
            candidate = randomizer.choice(
                tuple(code for code in ("11", "21", "22", "23") if code != value)
            )
        elif "INDICATOR" in upper_field and value in {"Y", "N"}:
            candidate = "N" if value == "Y" else "Y"
        elif "GENDER" in upper_field:
            candidate = randomizer.choice(("F", "M"))
        elif upper_field.endswith("CLAIM_FREQUENCY_CODE"):
            candidate = randomizer.choice(tuple(code for code in ("1", "7", "8") if code != value))
        elif "CITY" in upper_field:
            candidate = faker.city().upper()
        elif "STATE" in upper_field:
            candidate = randomizer.choice(tuple(code for code in _STATE_CODES if code != value))
        elif "ZIP_PLUS_FOUR" in upper_field:
            candidate = f"{randomizer.randrange(10_000):04d}"
        elif "ZIP" in upper_field:
            candidate = faker.postcode()[:5]
        elif _is_compact_date_field(upper_field, value):
            candidate = _changed_compact_date(value, randomizer)
        elif _is_timestamp_field(upper_field, value):
            candidate = _changed_timestamp(value, randomizer)
        elif "ID" in upper_field or "NUMBER" in upper_field:
            candidate = _same_shape_identifier(value, randomizer)
        else:
            candidate = faker.word().upper()
        if candidate == value and "GENDER" in upper_field:
            candidate = next(option for option in ("F", "M") if option != value)
        elif candidate == value and _is_constrained_field(upper_field):
            candidate = _semantic_code_candidate(upper_field, value, randomizer, force_change=True)
        elif candidate == value:
            candidate = f"{faker.word().upper()}X"
        return candidate
    return randomizer.randrange(1000, 9999)


def _semantic_code_candidate(
    field: str, value: str, randomizer: Random, force_change: bool = False
) -> str | None:
    """Choose a valid-looking GDF/healthcare code for a constrained field.

    The schemas do not carry every external code system.  This classifier is
    intentionally based on stable semantic suffixes/prefixes rather than a
    list of individual layout fields so new Member, Provider, Claim, History,
    and Payment fields inherit the safe behavior automatically.
    """
    values: tuple[str, ...] | None = None
    if field.endswith("_POA"):
        values = ("Y", "N", "U", "W")
    elif "EXTERNAL_CAUSE_OF_INJURY_CODE" in field:
        values = _ICD_EXTERNAL_CAUSE_CODES
    elif "DIAGNOSIS_CODE" in field or "REASON_FOR_VISIT_CODE" in field:
        values = _ICD_DIAGNOSIS_CODES
    elif "ICD_VERSION_CODE" in field:
        values = ("0", "9", "10")
    elif "DIAGNOSIS_POINTER" in field:
        values = tuple(str(number) for number in range(1, 13))
    elif "PROCEDURE_MODIFIER" in field:
        values = _PROCEDURE_MODIFIERS
    elif "PROCEDURE_CODE_QUALIFIER" in field:
        values = ("HCPCS", "CPT")
    elif "PROCEDURE_CODE" in field:
        values = _PROCEDURE_CODES
    elif "REVENUE_CODE" in field:
        values = _REVENUE_CODES
    elif "ADJUSTMENT_GROUP_CODE" in field or "REMITTANCE_ADVICE_GROUP_CODE" in field:
        values = _ADJUSTMENT_GROUP_CODES
    elif "ADJUSTMENT_REASON_CODE" in field or "REMITTANCE_ADVICE_REASON_CODE" in field:
        values = _ADJUSTMENT_REASON_CODES
    elif "TAXONOMY_CODE" in field or "SPECIALTY_CODE" in field:
        values = _TAXONOMY_CODES
    elif "STATE" in field:
        values = _STATE_CODES
    elif "ADDRESS_TYPE" in field:
        values = ("HOME", "MAIL", "WORK")
    elif "LINE_OF_BUSINESS_CODE" in field:
        values = ("COM", "MCD", "MED")
    elif "PLACE_OF_SERVICE_CODE" in field:
        values = ("11", "21", "22", "23", "31")
    elif "TYPE_OF_BILL_CODE" in field:
        values = ("111", "131", "851")
    elif field.endswith("CLAIM_FREQUENCY_CODE"):
        values = ("1", "7", "8")
    elif "CMS_CLAIM_ADJUSTMENT_TYPE_CODE" in field:
        values = ("0", "1", "2")
    elif "CMS_CLAIM_QUERY_CODE" in field:
        values = ("0", "3", "5")
    elif "FILING_INDICATOR_CODE" in field:
        values = ("CI", "MC", "MB")
    elif "CREDIT_DEBIT_FLAG_CODE" in field:
        values = ("C", "D")
    elif "PAYEE_ID_QUALIFIER" in field:
        values = ("XX", "FI", "MI")
    elif "QUALIFIER" in field:
        values = ("ZZ", "XX")
    elif "PAYMENT_METHOD" in field:
        values = ("CHK", "EFT")
    elif "DISCHARGE_STATUS_CODE" in field:
        values = ("01", "02", "20")
    elif "PAYMENT_STATUS" in field or field.endswith("STATUS_CODE"):
        values = ("PAID", "PENDED", "VOID") if not field.endswith("_CODE") else ("1", "2", "22")
    elif "ENTITY_TYPE" in field:
        values = ("P", "E")
    elif field.endswith("RECORD_TYPE"):
        # CDF Provider uses P/F while NPPES uses numeric entity codes.  Keep
        # the stream's established representation instead of crossing them.
        values = ("P", "F") if value in {"P", "F"} else ("1", "2")
    elif "RELATIONSHIP" in field:
        values = ("18", "19")
    elif "ADMISSION_TYPE" in field:
        values = ("1", "2", "3")
    elif "ADMISSION_SOURCE_CODE" in field:
        values = ("1", "2", "7")
    elif field.endswith("_HOUR"):
        values = tuple(f"{hour:02d}" for hour in range(24))
    elif field.endswith("_MINUTE"):
        values = tuple(f"{minute:02d}" for minute in range(60))
    elif field.endswith("_DAYS"):
        values = tuple(str(day) for day in range(1, 31))
    elif "UNITS_TYPE" in field:
        values = ("UN", "ML", "GR")
    elif "PAYER_ORDER_OF_BENEFITS" in field:
        values = ("1", "2", "3")
    elif "INDICATOR" in field or field.endswith("_FLAG"):
        values = ("Y", "N")
    elif "GENDER" in field:
        values = ("F", "M", "X")
    elif _is_constrained_field(field):
        # The source documentation identifies this as a code/classification,
        # but does not provide a code list.  A numeric code token is safer than
        # inventing prose and is the documented default for such fields.
        values = ("0", "1", "2", "9")

    if values is None:
        return None
    candidates = tuple(candidate for candidate in values if candidate != value)
    if candidates:
        return randomizer.choice(candidates)
    if force_change:
        if len(values) > 1:
            return next(candidate for candidate in values if candidate != value)
        return "0"
    return value


def _is_constrained_field(field: str) -> bool:
    """Return whether a field name denotes a coded/constrained value domain."""
    return any(
        token in field
        for token in (
            "_CODE",
            "_TYPE",
            "_STATUS",
            "_INDICATOR",
            "_QUALIFIER",
            "_CLASSIFICATION",
            "_CATEGORY",
            "_FLAG",
            "_METHOD",
            "_POA",
        )
    )


def _is_compact_date_field(field: str, value: str) -> bool:
    """Return whether a string is an emitted compact GDF date."""
    return ("DATE" in field or field.endswith("_AT")) and len(value) == 8 and value.isdigit()


def _changed_compact_date(value: str, randomizer: Random) -> str:
    """Move a valid compact date by a deterministic non-zero number of days."""
    try:
        original = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        original = date(2020, 1, 1) + timedelta(days=randomizer.randrange(3_650))
    offset = randomizer.choice(tuple(range(-365, 0)) + tuple(range(1, 366)))
    return (original + timedelta(days=offset)).strftime("%Y%m%d")


def _is_timestamp_field(field: str, value: str) -> bool:
    """Return whether a field carries an ISO-like timestamp rather than free text."""
    return (field.endswith("_AT") or "TIMESTAMP" in field or "PRODUCED_AT" in field) and bool(value)


def _changed_timestamp(value: str, randomizer: Random) -> str:
    """Move an ISO timestamp while retaining its offset and wire representation."""
    try:
        original = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _changed_compact_date("", randomizer)
    offset = randomizer.choice(tuple(range(-365, 0)) + tuple(range(1, 366)))
    updated = original + timedelta(days=offset)
    return updated.isoformat().replace("+00:00", "Z")


def _schema_enum_candidate(
    profile: str, field: str, value: object, randomizer: Random
) -> object | None:
    """Return a different JSON Schema enum member when the schema defines one."""
    allowed = _schema_enum_values(profile, field)
    candidates = tuple(candidate for candidate in allowed if candidate != value)
    return randomizer.choice(candidates) if candidates else None


@lru_cache(maxsize=None)
def _schema_enum_values(profile: str, field: str) -> tuple[object, ...]:
    """Read enum values for a field from the installed JSON Schema, if any."""
    schema = _load_profile_schema(profile)
    if schema is None:
        return ()

    values: list[object] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                definition = properties.get(field)
                if isinstance(definition, Mapping):
                    enum = definition.get("enum")
                    if isinstance(enum, list):
                        values.extend(
                            item for item in enum if isinstance(item, str | int | float | bool)
                        )
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return tuple(dict.fromkeys(values))


def _schema_field_types(profile: str, field: str) -> tuple[str, ...]:
    """Read declared JSON-Schema types for a field to select catalog values."""
    schema = _load_profile_schema(profile)
    if schema is None:
        return ()
    values: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                definition = properties.get(field)
                if isinstance(definition, Mapping):
                    declared = definition.get("type")
                    if isinstance(declared, str):
                        values.append(declared)
                    elif isinstance(declared, list):
                        values.extend(item for item in declared if isinstance(item, str))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return tuple(dict.fromkeys(values))


@lru_cache(maxsize=None)
def _load_profile_schema(profile: str) -> Mapping[str, object] | None:
    """Load one installed schema once for the update value resolver."""
    schema_path = _PROFILE_SCHEMA_PATHS.get(profile)
    if schema_path is None:
        return None
    try:
        resource = files("test_data_generator").joinpath("schema", "json", schema_path)
        schema = json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError):
        path = Path(__file__).resolve().parents[3] / "schema" / "json" / schema_path
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return schema if isinstance(schema, Mapping) else None


def _same_shape_identifier(value: str, randomizer: Random) -> str:
    """Return a changed identifier while retaining its observed source format."""
    result: list[str] = []
    for character in value:
        if character.isdigit():
            result.append(str(randomizer.randrange(10)))
        elif character.isupper():
            result.append(chr(randomizer.randrange(ord("A"), ord("Z") + 1)))
        elif character.islower():
            result.append(chr(randomizer.randrange(ord("a"), ord("z") + 1)))
        else:
            result.append(character)
    candidate = "".join(result)
    if candidate == value and value:
        last = value[-1]
        replacement = "1" if last != "1" and last.isdigit() else "A" if last != "A" else "B"
        candidate = value[:-1] + replacement
    return candidate


def _changed_root_claim_id(value: str, randomizer: Random) -> str:
    """Change a Claim root ID while retaining its P/I root identifier contract."""
    match = re.fullmatch(r"([PI]ROOT)([0-9]{8})", value)
    if match is None:
        return _same_shape_identifier(value, randomizer)
    original = match.group(2)
    replacement = f"{randomizer.randrange(100_000_000):08d}"
    if replacement == original:
        replacement = f"{(int(original) + 1) % 100_000_000:08d}"
    return match.group(1) + replacement


def _find_field(record: Mapping[str, object], field: str) -> object:
    if field in record:
        return record[field]
    for value in record.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    found = _find_field(item, field)
                    if found != "":
                        return found
        elif isinstance(value, Mapping):
            found = _find_field(value, field)
            if found != "":
                return found
    return ""


def _field_names(record: Mapping[str, object]) -> set[str]:
    """Return field names present in a root or nested generated record."""
    names: set[str] = set()
    for field, value in record.items():
        names.add(field)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    names.update(_field_names(item))
        elif isinstance(value, Mapping):
            names.update(_field_names(value))
    return names


def _replace_field(
    record: dict[str, object], field: str, value: object, *, create: bool = True
) -> bool:
    if field in record:
        record[field] = value
        return True
    for current in record.values():
        if isinstance(current, list):
            for item in current:
                if isinstance(item, dict) and _replace_field(item, field, value, create=False):
                    return True
        elif isinstance(current, dict) and _replace_field(current, field, value, create=False):
            return True
    if create:
        record[field] = value
    return False


def _remove_field(record: dict[str, object], field: str) -> bool:
    if field in record:
        del record[field]
        return True
    for current in record.values():
        if isinstance(current, list):
            for item in current:
                if isinstance(item, dict) and _remove_field(item, field):
                    return True
        elif isinstance(current, dict) and _remove_field(current, field):
            return True
    return False


def _relation(total: Decimal, threshold: Decimal) -> str:
    if total < threshold:
        return "below"
    if total == threshold:
        return "equal"
    return "above"


def _weight_threshold(request: UpdateRequest, rules: EntityRules) -> Decimal:
    """Resolve a usable threshold, including a default below-limit boundary."""
    if request.threshold is not None:
        return request.threshold
    method = next(
        (method for method in rules.methods if method.name == request.matching_method),
        rules.methods[0],
    )
    if request.operation == OperationType.WEIGHT_CHANGE and request.condition == "BELOW_LIMIT":
        weights = [
            rules.fields[name].weight
            for name in method.fields
            if name not in rules.keys and rules.fields[name].weight > 0
        ]
        if weights:
            return method.needed_weight + min(weights)
    return method.needed_weight


def _select_weight_fields(
    rules: EntityRules,
    threshold: Decimal,
    condition: str,
    available_fields: set[str] | None = None,
    matching_method: str | None = None,
) -> tuple[str, ...]:
    """Choose the smallest deterministic field combination for a weight boundary."""
    method = next(
        (method for method in rules.methods if method.name == matching_method),
        rules.methods[0],
    )
    preferred_candidates = tuple(
        name
        for name in method.fields
        if name not in rules.keys
        and name not in _UPDATE_PROTECTED_FIELDS
        and (available_fields is None or name in available_fields)
    )
    wanted = {"BELOW_LIMIT": "below", "AT_LIMIT": "equal", "ABOVE_LIMIT": "above"}.get(condition)
    if wanted is None:
        raise ValueError(f"Unknown WEIGHT_CHANGE condition {condition!r}")

    preferred = _weight_combination(rules, preferred_candidates, threshold, wanted)
    if preferred is not None:
        return preferred

    # The matching method determines the applicable threshold, but some source
    # methods contain only matching keys or too few non-key fields to reach all
    # boundaries. Fall back to other weighted survivorship fields while still
    # protecting every matching key so the fixture remains post-match.
    candidates = tuple(
        name
        for name in rules.fields
        if name not in rules.keys
        and name not in _UPDATE_PROTECTED_FIELDS
        and (available_fields is None or name in available_fields)
    )
    fallback = _weight_combination(rules, candidates, threshold, wanted)
    if fallback is not None:
        return fallback
    raise ValueError(f"No field combination can produce a {wanted}-threshold update")


def _weight_combination(
    rules: EntityRules,
    candidates: tuple[str, ...],
    threshold: Decimal,
    wanted: str,
) -> tuple[str, ...] | None:
    """Return a deterministic combination without exhaustive subset enumeration.

    Weight-boundary fixtures run against large Claim and Payment layouts, where
    trying every subset is exponential.  All catalog weights are non-negative:
    an empty selection is always below a positive threshold, and the fewest
    fields that can exceed a threshold are the highest-weight fields.  Exact
    boundaries use a bounded dynamic-programming table that retains only the
    best plan for each reachable total at or below the threshold.
    """
    if _relation(Decimal("0"), threshold) == wanted:
        return ()
    positive = tuple(name for name in candidates if rules.fields[name].weight > 0)
    if wanted == "above":
        selected: list[str] = []
        total = Decimal("0")
        ordered = sorted(
            enumerate(positive), key=lambda item: (-rules.fields[item[1]].weight, item[0])
        )
        for _, name in ordered:
            selected.append(name)
            total += rules.fields[name].weight
            if _relation(total, threshold) == "above":
                return tuple(selected)
        return None
    if wanted == "equal":
        plans: dict[Decimal, tuple[str, ...]] = {Decimal("0"): ()}
        for name in positive:
            weight = rules.fields[name].weight
            for total, plan in tuple(plans.items()):
                candidate_total = total + weight
                if candidate_total > threshold:
                    continue
                candidate_plan = (*plan, name)
                existing_plan = plans.get(candidate_total)
                if existing_plan is None or _is_preferred_weight_plan(
                    candidate_plan, existing_plan, candidates
                ):
                    plans[candidate_total] = candidate_plan
        return plans.get(threshold)
    return None


def _is_preferred_weight_plan(
    candidate: tuple[str, ...], current: tuple[str, ...], order: tuple[str, ...]
) -> bool:
    """Prefer fewer fields, then preserve catalog field order deterministically."""
    if len(candidate) != len(current):
        return len(candidate) < len(current)
    positions = {name: index for index, name in enumerate(order)}
    return tuple(positions[name] for name in candidate) < tuple(positions[name] for name in current)
