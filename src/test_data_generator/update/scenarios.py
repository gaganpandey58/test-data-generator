"""Scenario resolution and deterministic record mutation."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from random import Random
from typing import Mapping

from faker import Faker

from test_data_generator.update.rules import EntityRules


class UpdateScenario(StrEnum):
    """Supported update fixture intents."""

    UPDATE_SINGLE_FIELD = "UPDATE_SINGLE_FIELD"
    UPDATE_REQUIRED_FIELDS = "UPDATE_REQUIRED_FIELDS"
    UPDATE_OPTIONAL_FIELDS = "UPDATE_OPTIONAL_FIELDS"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    MISSING_MULTIPLE_FIELDS = "MISSING_MULTIPLE_FIELDS"
    MISSING_SELECTED_FIELDS = "MISSING_SELECTED_FIELDS"
    INVALID_KEY = "INVALID_KEY"
    CHANGE_WEIGHT_BELOW_LIMIT = "CHANGE_WEIGHT_BELOW_LIMIT"
    CHANGE_WEIGHT_AT_LIMIT = "CHANGE_WEIGHT_AT_LIMIT"
    POST_MATCH_WEIGHT_LIMIT_EXCEEDED = "POST_MATCH_WEIGHT_LIMIT_EXCEEDED"


@dataclass(frozen=True)
class UpdateRequest:
    """One normalized update request."""

    scenario: UpdateScenario
    fields: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    matching_method: str | None = None
    threshold: Decimal | None = None


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


def resolve_fields(request: UpdateRequest, rules: EntityRules) -> tuple[str, ...]:
    """Resolve explicit fields, include/exclude controls, and scenario defaults."""
    known = rules.fields
    context = request.scenario.value
    if request.fields:
        selected = list(request.fields)
    elif request.include:
        selected = [field for field in request.include if field in known]
    elif request.scenario in {
        UpdateScenario.UPDATE_REQUIRED_FIELDS,
        UpdateScenario.MISSING_MULTIPLE_FIELDS,
    }:
        selected = [
            name
            for name, rule in known.items()
            if rule.is_required_for(context) and name not in rules.keys
        ]
    elif request.scenario == UpdateScenario.MISSING_REQUIRED_FIELD:
        selected = [
            name
            for name, rule in known.items()
            if rule.is_required_for(context) and name not in rules.keys
        ][:1]
    elif request.scenario == UpdateScenario.UPDATE_OPTIONAL_FIELDS:
        selected = [
            name
            for name, rule in known.items()
            if not rule.is_required_for(context) and name not in rules.keys
        ]
    elif request.scenario == UpdateScenario.INVALID_KEY:
        selected = list(rules.keys)
    else:
        selected = [name for name in known if name not in rules.keys]
    if (
        request.scenario == UpdateScenario.UPDATE_SINGLE_FIELD
        and not request.fields
        and not request.include
    ):
        selected = selected[:1]
    selected = [field for field in selected if field not in request.exclude]
    if any(field not in known for field in selected):
        raise ValueError("Update selection contains an unknown field")
    if (
        any(field in rules.keys for field in selected)
        and request.scenario != UpdateScenario.INVALID_KEY
    ):
        raise ValueError("Matching keys may only be selected by INVALID_KEY")
    if request.scenario == UpdateScenario.UPDATE_SINGLE_FIELD and len(selected) != 1:
        raise ValueError("UPDATE_SINGLE_FIELD requires exactly one selected field")
    if request.scenario == UpdateScenario.MISSING_REQUIRED_FIELD and len(selected) != 1:
        raise ValueError("MISSING_REQUIRED_FIELD requires exactly one selected field")
    if request.scenario == UpdateScenario.MISSING_MULTIPLE_FIELDS and len(selected) < 2:
        raise ValueError("MISSING_MULTIPLE_FIELDS requires at least two selected fields")
    if not selected:
        raise ValueError("Update scenario resolved no fields")
    return tuple(dict.fromkeys(selected))


def resolve_update(
    base: Mapping[str, object], request: UpdateRequest, rules: EntityRules, seed: int, index: int
) -> ResolvedUpdate:
    """Create one deterministic update from one base record."""
    selected = resolve_fields(request, rules)
    if (
        request.scenario
        in {
            UpdateScenario.CHANGE_WEIGHT_BELOW_LIMIT,
            UpdateScenario.CHANGE_WEIGHT_AT_LIMIT,
            UpdateScenario.POST_MATCH_WEIGHT_LIMIT_EXCEEDED,
        }
        and not request.fields
        and not request.include
    ):
        selection_threshold = request.threshold or rules.methods[0].needed_weight
        selected = _select_weight_fields(rules, selection_threshold, request.scenario)
    result = dict(base)
    changed: list[str] = []
    removed: list[str] = []
    invalidated: list[str] = []
    randomizer = Random(seed * 1_000_003 + index * 97 + 41)
    if request.scenario in {
        UpdateScenario.MISSING_REQUIRED_FIELD,
        UpdateScenario.MISSING_MULTIPLE_FIELDS,
        UpdateScenario.MISSING_SELECTED_FIELDS,
    }:
        for field in selected:
            if _remove_field(result, field):
                removed.append(field)
    else:
        if request.scenario == UpdateScenario.INVALID_KEY:
            for field in selected:
                _replace_field(
                    result,
                    field,
                    _invalid_key_value(_find_field(result, field), field, index),
                )
                changed.append(field)
                invalidated.append(field)
        else:
            for field in selected:
                old = _find_field(result, field)
                new = _changed_value(old, field, randomizer)
                _replace_field(result, field, new)
                if new != old:
                    changed.append(field)
    total = sum((rules.fields[field].weight for field in changed), Decimal("0"))
    threshold = request.threshold
    if threshold is None:
        method = next(
            (method for method in rules.methods if method.name == request.matching_method),
            rules.methods[0],
        )
        threshold = method.needed_weight
    relation = _relation(total, threshold)
    if request.scenario == UpdateScenario.CHANGE_WEIGHT_BELOW_LIMIT and relation != "below":
        raise ValueError("Selected fields do not produce a below-threshold update")
    if request.scenario == UpdateScenario.CHANGE_WEIGHT_AT_LIMIT and relation != "equal":
        raise ValueError("Selected fields do not produce an at-threshold update")
    if request.scenario == UpdateScenario.POST_MATCH_WEIGHT_LIMIT_EXCEEDED and relation != "above":
        raise ValueError("Selected fields do not produce an above-threshold update")
    return ResolvedUpdate(
        record=result,
        changed_fields=tuple(changed),
        removed_fields=tuple(removed),
        invalidated_keys=tuple(invalidated),
        total_weight=total,
        threshold_relation=relation,
        expected_match=not invalidated,
        expected_apply=request.scenario != UpdateScenario.POST_MATCH_WEIGHT_LIMIT_EXCEEDED
        and not invalidated,
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


def _invalid_key_value(value: object, field: str, index: int) -> object:
    """Change one key character while preserving the source key shape."""
    if isinstance(value, int) and not isinstance(value, bool):
        candidate = value + 1
        if len(str(candidate)) == len(str(value)):
            return candidate
        return value - 1
    if isinstance(value, str) and value:
        characters = list(value)
        for position in range(len(characters) - 1, -1, -1):
            character = characters[position]
            if character.isdigit():
                characters[position] = str((int(character) + 1) % 10)
                return "".join(characters)
            if character.isalpha():
                characters[position] = "X" if character.upper() != "X" else "Y"
                return "".join(characters)
    return f"INVALID{index + 1:04d}"


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
    rules: EntityRules, threshold: Decimal, scenario: UpdateScenario
) -> tuple[str, ...]:
    """Choose the smallest deterministic field combination for a weight boundary."""
    candidates = tuple(name for name in rules.fields if name not in rules.keys)
    wanted = {
        UpdateScenario.CHANGE_WEIGHT_BELOW_LIMIT: "below",
        UpdateScenario.CHANGE_WEIGHT_AT_LIMIT: "equal",
        UpdateScenario.POST_MATCH_WEIGHT_LIMIT_EXCEEDED: "above",
    }[scenario]
    for size in range(1, len(candidates) + 1):
        for combination in combinations(candidates, size):
            total = sum((rules.fields[name].weight for name in combination), Decimal("0"))
            if _relation(total, threshold) == wanted:
                return combination
    raise ValueError(f"No field combination can produce a {wanted}-threshold update")
