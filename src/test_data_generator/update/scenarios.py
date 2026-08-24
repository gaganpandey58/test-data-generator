"""Scenario resolution and deterministic record mutation."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from random import Random
from typing import Mapping

from faker import Faker

from test_data_generator.update.rules import EntityRules
from test_data_generator.update.synchronization import synchronize_record


class OperationType(StrEnum):
    """Generic record mutation operations."""

    UPDATE = "UPDATE"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    INVALID = "INVALID"
    WEIGHT_CHANGE = "WEIGHT_CHANGE"


def load_invalid_values(path: Path) -> dict[str, tuple[object, ...]]:
    """Load the field-name keyed invalid-value catalog."""
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


def resolve_fields(
    request: UpdateRequest, rules: EntityRules, seed: int = 0, index: int = 0
) -> tuple[str, ...]:
    """Resolve explicit fields or select eligible fields from the rule catalog."""
    known = rules.fields
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
        selected = [name for name in known if name not in rules.keys]
        if request.operation == OperationType.MISSING:
            required = [name for name in selected if known[name].required]
            selected = required or selected
        selected = [Random(seed * 1_000_003 + index).choice(selected)] if selected else []
    else:
        selected = [name for name in known if name not in rules.keys]
    excluded = set(_normalize_fields(request.exclude, known))
    selected = [field for field in selected if field not in excluded]
    if any(field not in known for field in selected):
        unknown = next(field for field in selected if field not in known)
        raise ValueError(f"Update selection contains an unknown field {unknown!r}")
    if (
        any(field in rules.keys for field in selected)
        and request.operation != OperationType.INVALID
    ):
        matching = next(field for field in selected if field in rules.keys)
        raise ValueError(f"Matching key {matching!r} may only be selected by INVALID operation")
    if not selected:
        raise ValueError("Operation resolved no fields")
    return tuple(dict.fromkeys(selected))


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
    selected = resolve_fields(request, rules, seed, index)
    operation = request.operation
    if operation == OperationType.WEIGHT_CHANGE and not request.fields and not request.include:
        selection_threshold = request.threshold or rules.methods[0].needed_weight
        condition = request.condition
        if condition is None:
            raise ValueError("WEIGHT_CHANGE requires BELOW_LIMIT, AT_LIMIT, or ABOVE_LIMIT")
        selected = _select_weight_fields(rules, selection_threshold, condition)
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
                values = catalog.get(field)
                if not values:
                    raise ValueError(f"INVALID field {field!r} has no invalid-value catalog entry")
                _replace_field(result, field, randomizer.choice(values))
                changed.append(field)
                if field in rules.keys:
                    invalidated.append(field)
        else:
            for field in selected:
                old = _find_field(result, field)
                new: object = _changed_value(old, field, randomizer)
                _replace_field(result, field, new)
                if new != old:
                    changed.append(field)
    synchronized = synchronize_record(original, result, tuple(changed))
    total = sum((rules.fields[field].weight for field in changed), Decimal("0"))
    threshold = request.threshold
    if threshold is None:
        method = next(
            (method for method in rules.methods if method.name == request.matching_method),
            rules.methods[0],
        )
        threshold = method.needed_weight
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


def _changed_value(value: object, field: str, randomizer: Random) -> object:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, int):
            int_candidate = randomizer.randrange(max(0, value - 100), value + 101)
            return int_candidate if int_candidate != value else value + 1
        float_candidate = round(randomizer.uniform(max(0, value - 100), value + 100), 2)
        return float_candidate if float_candidate != value else round(value + 1, 2)
    if isinstance(value, str):
        faker = Faker("en_US")
        faker.seed_instance(randomizer.randrange(1, 2**31 - 1))
        upper_field = field.upper()
        candidate: object
        if "FIRST_NAME" in upper_field:
            candidate = faker.first_name().upper()
        elif "LAST_NAME" in upper_field:
            candidate = faker.last_name().upper()
        elif "MIDDLE_NAME" in upper_field:
            candidate = faker.first_name()[0].upper()
        elif "GENDER" in upper_field:
            candidate = randomizer.choice(("F", "M", "X"))
        elif "CITY" in upper_field:
            candidate = faker.city().upper()
        elif "STATE" in upper_field:
            candidate = faker.state_abbr()
        elif "ZIP" in upper_field:
            candidate = faker.postcode()[:5]
        elif "DATE" in upper_field and len(value) == 8 and value.isdigit():
            candidate = faker.date_between(start_date="-10y", end_date="today").strftime("%Y%m%d")
        elif "SSN" in upper_field:
            candidate = faker.ssn().replace("-", "")
        elif "ID" in upper_field or "NUMBER" in upper_field:
            candidate = faker.bothify("??????????").upper()
        else:
            candidate = faker.word().upper()
        if candidate == value and "GENDER" in upper_field:
            candidate = next(option for option in ("F", "M", "X") if option != value)
        elif candidate == value:
            candidate = f"{faker.word().upper()}X"
        return candidate
    return randomizer.randrange(1000, 9999)


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


def _replace_field(record: dict[str, object], field: str, value: object) -> bool:
    if field in record:
        record[field] = value
        return True
    for current in record.values():
        if isinstance(current, list):
            for item in current:
                if isinstance(item, dict) and _replace_field(item, field, value):
                    return True
        elif isinstance(current, dict) and _replace_field(current, field, value):
            return True
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


def _select_weight_fields(
    rules: EntityRules, threshold: Decimal, condition: str
) -> tuple[str, ...]:
    """Choose the smallest deterministic field combination for a weight boundary."""
    candidates = tuple(name for name in rules.fields if name not in rules.keys)
    wanted = {"BELOW_LIMIT": "below", "AT_LIMIT": "equal", "ABOVE_LIMIT": "above"}.get(condition)
    if wanted is None:
        raise ValueError(f"Unknown WEIGHT_CHANGE condition {condition!r}")
    for size in range(1, len(candidates) + 1):
        for combination in combinations(candidates, size):
            total = sum((rules.fields[name].weight for name in combination), Decimal("0"))
            if _relation(total, threshold) == wanted:
                return combination
    raise ValueError(f"No field combination can produce a {wanted}-threshold update")
