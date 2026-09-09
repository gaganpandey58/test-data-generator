"""Configuration-driven paired-record matching fixtures."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from random import Random
from typing import Mapping

from test_data_generator.update.rules import EntityRules, MatchingMethod
from test_data_generator.update.scenarios import (
    ExpectedOutcome,
    FailureMode,
    FieldModification,
    OperationType,
    ResolvedUpdate,
    UpdateRequest,
    _changed_value,
    _field_names,
    _generic_invalid_value,
    _normalize_fields,
)
from test_data_generator.update.synchronization import synchronize_record

_MISSING = object()


@dataclass(frozen=True)
class MatchAssessment:
    """Explain whether one incoming record satisfies one configured method."""

    method_id: str
    matched: bool
    matched_fields: tuple[str, ...]
    failed_mandatory_fields: tuple[str, ...]
    matched_weight: Decimal


def assess_match(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
    rules: EntityRules,
    method: MatchingMethod,
) -> MatchAssessment:
    """Evaluate one method using its configured anchors, weights, and elasticity."""
    matched_fields = tuple(
        field
        for field in method.fields
        if _field_matches(
            _lookup(existing, field),
            _lookup(incoming, field),
            method.elasticity_for(field, rules.fields[field].elasticity),
        )
    )
    failed_mandatory = tuple(
        field for field in method.mandatory_fields if field not in matched_fields
    )
    matched_weight = sum((rules.fields[field].weight for field in matched_fields), Decimal("0"))
    return MatchAssessment(
        method.name,
        not failed_mandatory and matched_weight >= method.needed_weight,
        matched_fields,
        failed_mandatory,
        matched_weight,
    )


def resolve_match_fixture(
    base: Mapping[str, object], request: UpdateRequest, rules: EntityRules, seed: int, index: int
) -> ResolvedUpdate:
    """Build and verify a configured positive or negative matching fixture."""
    method = _method(rules, request.matching_method)
    outcome = request.expected_outcome
    assert outcome is not None
    if outcome == ExpectedOutcome.NO_MATCH and request.failure_mode is None:
        raise ValueError("NO_MATCH requires a failure_mode")
    if outcome == ExpectedOutcome.MATCH and request.failure_mode is not None:
        raise ValueError("MATCH cannot declare a failure_mode")

    original = deepcopy(dict(base))
    result = deepcopy(original)
    randomizer = Random(seed * 1_000_003 + index * 97 + 733)
    plan = request.modifications or _legacy_plan(request)
    changed: list[str] = []
    removed: list[str] = []
    _apply_modifications(
        result,
        plan,
        method,
        outcome,
        request.invalid_values or {},
        randomizer,
        rules.profile,
        changed,
        removed,
    )
    _apply_optional_selection(
        result,
        original,
        request,
        rules,
        method,
        randomizer,
        changed,
    )
    if outcome == ExpectedOutcome.NO_MATCH:
        _apply_failure(result, original, request, rules, method, randomizer, changed, removed)
    elif request.elasticity_boundary is not None:
        _apply_match_boundary(result, original, request, rules, method, changed)
    _break_higher_priority_matches(result, original, rules, method, randomizer, changed)

    synchronized = synchronize_record(original, result, tuple(dict.fromkeys(changed + removed)))
    target = assess_match(original, result, rules, method)
    if target.matched != (outcome == ExpectedOutcome.MATCH):
        raise ValueError(
            f"Generated fixture did not produce {outcome} for matching method {method.name!r}"
        )
    assessments = tuple(
        assess_match(original, result, rules, candidate) for candidate in rules.methods
    )
    matched_methods = tuple(
        assessment.method_id for assessment in assessments if assessment.matched
    )
    unexpected = tuple(name for name in matched_methods if name != method.name)
    if outcome == ExpectedOutcome.MATCH:
        higher = set(method.higher_priority_methods)
        collision = next((name for name in unexpected if name in higher), None)
        if collision is not None:
            raise ValueError(
                f"Target method {method.name!r} unexpectedly satisfies "
                f"higher-priority method {collision!r}"
            )
    elif request.failure_mode == FailureMode.CROSS_METHOD_COLLISION:
        if request.collision_method not in unexpected:
            raise ValueError("CROSS_METHOD_COLLISION did not match the configured collision_method")
    total = sum(
        (rules.fields[field].weight for field in changed if field in rules.fields), Decimal("0")
    )
    applied_plan = plan
    if changed:
        applied_plan += (FieldModification(OperationType.UPDATE, tuple(dict.fromkeys(changed))),)
    if removed:
        applied_plan += (FieldModification(OperationType.MISSING, tuple(dict.fromkeys(removed))),)
    return ResolvedUpdate(
        record=result,
        changed_fields=tuple(dict.fromkeys(changed)),
        removed_fields=tuple(dict.fromkeys(removed)),
        invalidated_keys=tuple(
            field for field in dict.fromkeys(changed + removed) if field in rules.keys
        ),
        total_weight=total,
        threshold_relation="match" if target.matched else "no_match",
        expected_match=target.matched,
        expected_apply=target.matched,
        synchronized_fields=synchronized,
        method_id=method.name,
        expected_outcome=outcome,
        failure_mode=request.failure_mode,
        matched_methods=matched_methods,
        unexpected_methods=unexpected,
        modification_plan=applied_plan,
    )


def _method(rules: EntityRules, name: str | None) -> MatchingMethod:
    if name is None:
        raise ValueError("Match fixtures require matching_method")
    try:
        return next(method for method in rules.methods if method.name == name)
    except StopIteration as error:
        raise ValueError(f"Unknown matching method {name!r}") from error


def _legacy_plan(request: UpdateRequest) -> tuple[FieldModification, ...]:
    if request.operation == OperationType.DUPLICATE and not request.fields:
        return ()
    return (FieldModification(request.operation, request.fields),)


def _apply_modifications(
    result: dict[str, object],
    plan: tuple[FieldModification, ...],
    method: MatchingMethod,
    outcome: ExpectedOutcome,
    invalid_values: Mapping[str, tuple[object, ...]],
    randomizer: Random,
    profile: str,
    changed: list[str],
    removed: list[str],
) -> None:
    available = _field_names(result)
    for modification in plan:
        fields = _normalize_fields(modification.fields, {field: object() for field in available})
        if not fields and modification.operation != OperationType.DUPLICATE:
            raise ValueError("Each field modification needs at least one field")
        for field in fields:
            if (
                field in method.mandatory_fields
                and modification.operation != OperationType.DUPLICATE
                and outcome != ExpectedOutcome.NO_MATCH
            ):
                raise ValueError(
                    f"{modification.operation} cannot target mandatory anchor {field!r} for MATCH"
                )
            if modification.operation == OperationType.DUPLICATE:
                continue
            if modification.operation == OperationType.MISSING:
                if _remove(result, field):
                    removed.append(field)
                continue
            old = _lookup(result, field)
            if old is _MISSING:
                raise ValueError(f"Modification field {field!r} is not present in generated record")
            if modification.operation == OperationType.INVALID:
                value = randomizer.choice(
                    invalid_values.get(field, (_generic_invalid_value(field),))
                )
            elif modification.operation == OperationType.EMPTY:
                value = "" if isinstance(old, str) else 0
            else:
                value = _changed_value(old, field, randomizer, profile)
            _replace(result, field, value)
            if value != old:
                changed.append(field)


def _apply_failure(
    result: dict[str, object],
    original: Mapping[str, object],
    request: UpdateRequest,
    rules: EntityRules,
    method: MatchingMethod,
    randomizer: Random,
    changed: list[str],
    removed: list[str],
) -> None:
    mode = request.failure_mode
    assert mode is not None
    if mode == FailureMode.WEIGHT_MISS:
        if not method.optional_fields:
            raise ValueError(
                f"Matching method {method.name!r} has no optional fields for WEIGHT_MISS"
            )
        for field in method.optional_fields:
            _replace(
                result,
                field,
                _changed_value(_lookup(original, field), field, randomizer, rules.profile),
            )
            changed.append(field)
        return
    if mode == FailureMode.CROSS_METHOD_COLLISION:
        collision = _method(rules, request.collision_method)
        failure_field = _first_breakable_field(method, collision.mandatory_fields)
        if failure_field is None:
            raise ValueError(
                "CROSS_METHOD_COLLISION needs a target anchor outside collision_method"
            )
    else:
        failure_field = request.failure_field or (
            method.mandatory_fields[0] if method.mandatory_fields else None
        )
    if failure_field is None or failure_field not in method.mandatory_fields:
        raise ValueError("failure_field must be a mandatory anchor for the target method")
    old = _lookup(original, failure_field)
    if old is _MISSING:
        raise ValueError(f"failure_field {failure_field!r} is not present in the generated record")
    if mode == FailureMode.MISSING_VALUE:
        _remove(result, failure_field)
        removed.append(failure_field)
    elif mode == FailureMode.INVALID_VALUE:
        _replace(
            result,
            failure_field,
            randomizer.choice(
                (request.invalid_values or {}).get(
                    failure_field, (_generic_invalid_value(failure_field),)
                )
            ),
        )
        changed.append(failure_field)
    elif mode == FailureMode.MANDATORY_BREAK_BOUNDARY:
        _replace(
            result,
            failure_field,
            _elastic_value(
                old,
                method.elasticity_for(failure_field, rules.fields[failure_field].elasticity),
                "OUTSIDE",
            ),
        )
        changed.append(failure_field)
    else:
        _replace(
            result,
            failure_field,
            _changed_value(old, failure_field, randomizer, rules.profile),
        )
        changed.append(failure_field)


def _apply_optional_selection(
    result: dict[str, object],
    original: Mapping[str, object],
    request: UpdateRequest,
    rules: EntityRules,
    method: MatchingMethod,
    randomizer: Random,
    changed: list[str],
) -> None:
    """Apply include/exclude decisions to optional anchors before evaluation."""
    if not method.optional_fields:
        return
    included = (
        set(_normalize_fields(request.include, rules.fields))
        if request.include
        else set(method.optional_fields)
    )
    excluded = set(_normalize_fields(request.exclude, rules.fields))
    for field in method.optional_fields:
        if field in included and field not in excluded:
            continue
        old = _lookup(original, field)
        if old is _MISSING:
            continue
        _replace(result, field, _changed_value(old, field, randomizer, rules.profile))
        changed.append(field)


def _apply_match_boundary(
    result: dict[str, object],
    original: Mapping[str, object],
    request: UpdateRequest,
    rules: EntityRules,
    method: MatchingMethod,
    changed: list[str],
) -> None:
    field = request.failure_field
    if field is None or field not in method.mandatory_fields:
        raise ValueError(
            "elasticity_boundary requires failure_field to identify a mandatory anchor"
        )
    old = _lookup(original, field)
    _replace(
        result,
        field,
        _elastic_value(
            old,
            method.elasticity_for(field, rules.fields[field].elasticity),
            request.elasticity_boundary,
        ),
    )
    changed.append(field)


def _break_higher_priority_matches(
    result: dict[str, object],
    original: Mapping[str, object],
    rules: EntityRules,
    target: MatchingMethod,
    randomizer: Random,
    changed: list[str],
) -> None:
    method_by_name = {method.name: method for method in rules.methods}
    for higher in sorted(
        (method_by_name[name] for name in target.higher_priority_methods),
        key=lambda method: method.priority,
    ):
        if not assess_match(original, result, rules, higher).matched:
            continue
        field = _first_breakable_field(higher, target.mandatory_fields)
        if field is None:
            raise ValueError(
                f"Target method {target.name!r} cannot avoid higher-priority method {higher.name!r}"
            )
        old = _lookup(result, field)
        _replace(result, field, _changed_value(old, field, randomizer, rules.profile))
        changed.append(field)


def _first_breakable_field(method: MatchingMethod, protected: tuple[str, ...]) -> str | None:
    # Prefer the final exclusive anchor. Claim/payment amount fields often
    # synchronize to a corresponding detail field, so changing them can also
    # alter an otherwise protected target anchor.
    return next(
        (field for field in reversed(method.mandatory_fields) if field not in protected), None
    )


def _field_matches(existing: object, incoming: object, elasticity: str) -> bool:
    if existing is _MISSING or incoming is _MISSING or existing is None or incoming is None:
        return False
    if str(elasticity).strip().lower() in {"", "0", "exact"}:
        return existing == incoming
    tolerance = _elasticity_days(elasticity)
    if tolerance is not None:
        left = _parse_date(existing)
        right = _parse_date(incoming)
        return left is not None and right is not None and abs((left - right).days) <= tolerance
    try:
        return abs(Decimal(str(existing)) - Decimal(str(incoming))) <= Decimal(str(elasticity))
    except (InvalidOperation, ValueError):
        return existing == incoming


def _elasticity_days(elasticity: str) -> int | None:
    normalized = str(elasticity).strip().lower().replace("_", " ")
    if normalized in {"less than a month", "month", "30d", "30 days"}:
        return 30
    if normalized.endswith(" days") and normalized[:-5].isdigit():
        return int(normalized[:-5])
    return None


def _elastic_value(value: object, elasticity: str, boundary: str | None) -> object:
    tolerance = _elasticity_days(elasticity)
    if tolerance is None:
        raise ValueError(f"Field elasticity {elasticity!r} does not support date boundaries")
    parsed = _parse_date(value)
    if parsed is None:
        raise ValueError("Elasticity boundary fields must contain a date")
    normalized = str(boundary).upper()
    offset = {"INSIDE": max(0, tolerance - 1), "AT": tolerance, "OUTSIDE": tolerance + 1}.get(
        normalized
    )
    if offset is None:
        raise ValueError("elasticity_boundary must be INSIDE, AT, or OUTSIDE")
    return (parsed + timedelta(days=offset)).strftime("%Y%m%d")


def _parse_date(value: object) -> date | None:
    text = str(value)
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _lookup(record: Mapping[str, object], field: str) -> object:
    if field in record:
        return record[field]
    for value in record.values():
        if isinstance(value, Mapping):
            found = _lookup(value, field)
        elif isinstance(value, list):
            found = next(
                (
                    candidate
                    for item in value
                    if isinstance(item, Mapping)
                    and (candidate := _lookup(item, field)) is not _MISSING
                ),
                _MISSING,
            )
        else:
            continue
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


def _remove(record: dict[str, object], field: str) -> bool:
    if field in record:
        del record[field]
        return True
    for value in record.values():
        if isinstance(value, dict) and _remove(value, field):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _remove(item, field):
                    return True
    return False
