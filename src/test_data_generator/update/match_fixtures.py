"""Emit rule-backed matching fixture matrices.

The normal generator writes production-shaped JSONL streams.  This module is
an opt-in QA fixture layer: it derives every case from an already-generated
base record and delegates all field semantics to the existing rules, mutation,
matching, invalid-catalog, and synchronization engines.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from random import Random
from typing import cast

from test_data_generator.configuration.config import (
    MatchFixtureCodeConfig,
    MatchFixtureEntityConfig,
)
from test_data_generator.update.matching import assess_match, resolve_match_fixture
from test_data_generator.update.rules import EntityRules, MatchingMethod
from test_data_generator.update.scenarios import (
    ExpectedOutcome,
    FailureMode,
    FieldModification,
    OperationType,
    ResolvedUpdate,
    UpdateRequest,
    load_invalid_values,
    resolve_update,
    supports_invalid_value,
)

_WEIGHT_OPERATIONS = {
    "WEIGHT_BELOW_LIMIT": "BELOW_LIMIT",
    "WEIGHT_AT_LIMIT": "AT_LIMIT",
    "WEIGHT_ABOVE_LIMIT": "ABOVE_LIMIT",
}
_ELASTICITY_OPERATIONS = {
    "ELASTICITY_INSIDE": "INSIDE",
    "ELASTICITY_AT_LIMIT": "AT",
    "ELASTICITY_OUTSIDE": "OUTSIDE",
}


def generate_match_fixture_matrix(
    fixture_entities: tuple[MatchFixtureEntityConfig, ...],
    generated_records: Mapping[str, tuple[Mapping[str, object], ...]],
    rules_catalog: Mapping[str, EntityRules],
    seed: int,
    output_directory: Path,
    invalid_values_catalog: Path | None,
) -> tuple[Path, ...]:
    """Write exact-count unified fixtures while preserving legacy output."""
    paths: list[Path] = []
    invalid_values = (
        load_invalid_values(invalid_values_catalog) if invalid_values_catalog is not None else {}
    )
    for entity_config in fixture_entities:
        records = generated_records.get(entity_config.entity)
        if not records:
            raise ValueError(
                f"Match-fixture entity {entity_config.entity!r} has no generated source records"
            )
        rules = rules_catalog.get(entity_config.entity)
        if rules is None:
            raise ValueError(
                f"Match-fixture entity {entity_config.entity!r} has no update rule catalog"
            )
        for match_code in entity_config.match_codes:
            if match_code.legacy_per_record:
                paths.extend(
                    _write_legacy_fixtures(
                        entity_config,
                        match_code,
                        records,
                        rules,
                        seed,
                        invalid_values,
                        output_directory,
                    )
                )
                continue
            method = _matching_method(rules, match_code.matching_method)
            collision_method = _resolve_collision_method(
                match_code,
                entity_config.match_codes,
                rules,
                method,
            )
            cases = _build_exact_cases(
                records,
                entity_config.entity,
                match_code,
                rules,
                method,
                collision_method,
                seed,
                invalid_values,
                entity_config.variation.requested_count,
                entity_config.variation.protected_fields,
            )
            grouped: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
            for case in cases:
                grouped[str(case["operation"])].append(case)
            for operation, operation_cases in grouped.items():
                operation_directory = (
                    output_directory
                    / "match-fixtures"
                    / operation.lower().replace("_", "-")
                    / entity_config.entity
                )
                operation_directory.mkdir(parents=True, exist_ok=True)
                suffix = (
                    f"__against__{collision_method}"
                    if operation == "COLLISION" and collision_method is not None
                    else ""
                )
                output_path = operation_directory / f"{method.name}{suffix}.json"
                output_path.write_text(
                    json.dumps(operation_cases, indent=2, default=_json_default) + "\n",
                    encoding="utf-8",
                )
                paths.append(output_path)
    return tuple(paths)


def _write_legacy_fixtures(
    entity_config: MatchFixtureEntityConfig,
    match_code: MatchFixtureCodeConfig,
    records: Sequence[Mapping[str, object]],
    rules: EntityRules,
    seed: int,
    invalid_values: Mapping[str, tuple[object, ...]],
    output_directory: Path,
) -> list[Path]:
    """Retain the previous per-source-record files for legacy configurations."""
    paths: list[Path] = []
    for record_index, base in enumerate(records, start=1):
        entity_directory = output_directory / f"{entity_config.entity}{record_index}"
        entity_directory.mkdir(parents=True, exist_ok=True)
        method = _matching_method(rules, match_code.matching_method)
        cases = _build_cases(
            base,
            entity_config.entity,
            record_index,
            match_code,
            rules,
            method.mandatory_fields,
            seed,
            invalid_values,
            entity_config.variation.requested_count,
            entity_config.variation.protected_fields,
        )
        output_name = (
            match_code.matching_method
            if match_code.name == match_code.matching_method
            else f"{match_code.matching_method}__{match_code.name}"
        )
        output_path = entity_directory / f"{output_name}.json"
        output_path.write_text(
            json.dumps(cases, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        paths.append(output_path)
    return paths


def _build_exact_cases(
    records: Sequence[Mapping[str, object]],
    entity_name: str,
    match_code: MatchFixtureCodeConfig,
    rules: EntityRules,
    method: MatchingMethod,
    collision_method: str | None,
    seed: int,
    invalid_values: Mapping[str, tuple[object, ...]],
    variation_count: int,
    variation_protected_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    """Build exact total counts using deterministic catalog-order selection."""
    applicable = tuple(
        (index, record)
        for index, record in enumerate(records, start=1)
        if all(_field_present(record, field) for field in method.mandatory_fields)
    )
    if not applicable:
        raise ValueError(
            f"Matching method {method.name!r} has no applicable {entity_name!r} source record"
        )
    result: list[dict[str, object]] = []
    method_cursor = 0
    source_cursor = 0

    def next_source(*required_methods: MatchingMethod) -> tuple[int, Mapping[str, object]]:
        nonlocal source_cursor
        for offset in range(len(applicable)):
            candidate_index = (source_cursor + offset) % len(applicable)
            record_index, record = applicable[candidate_index]
            if all(
                all(_field_present(record, field) for field in candidate.mandatory_fields)
                for candidate in required_methods
            ):
                source_cursor = candidate_index + 1
                return record_index, record
        names = ", ".join(candidate.name for candidate in required_methods)
        raise ValueError(f"No {entity_name!r} source record supports matching method(s) {names}")

    for operation in _ordered_operations(match_code.operation_counts):
        for _ in range(match_code.operation_counts[operation]):
            record_index, base = next_source(method)
            modifications: tuple[FieldModification, ...] = ()
            expected_outcome: ExpectedOutcome | None = None
            if operation in _ELASTICITY_OPERATIONS:
                field, method_cursor = _select_elastic_field(base, method, rules, method_cursor)
                modifications = (FieldModification(OperationType.UPDATE, (field,)),)
                expected_outcome = (
                    ExpectedOutcome.NO_MATCH
                    if operation == "ELASTICITY_OUTSIDE"
                    else ExpectedOutcome.MATCH
                )
            elif operation not in _WEIGHT_OPERATIONS:
                if operation == "DUPLICATE":
                    modifications = (FieldModification(OperationType.DUPLICATE, ()),)
                else:
                    field, method_cursor = _select_operation_field(
                        base,
                        method,
                        operation,
                        rules.profile,
                        invalid_values,
                        method_cursor,
                    )
                    modifications = (FieldModification(OperationType(operation), (field,)),)
                expected_outcome = _inferred_outcome(modifications, method.mandatory_fields)
            case_seed = _case_seed(seed, entity_name, method.name, len(result) + 1)
            if operation in _WEIGHT_OPERATIONS:
                resolved = _resolve_exact_weight_case(
                    base,
                    operation,
                    method,
                    rules,
                    seed,
                    case_seed,
                    invalid_values,
                    variation_count,
                    variation_protected_fields,
                )
                expected_outcome = (
                    ExpectedOutcome.NO_MATCH
                    if operation == "WEIGHT_BELOW_LIMIT"
                    else ExpectedOutcome.MATCH
                )
            else:
                resolved = _resolve_case(
                    base,
                    operation,
                    modifications,
                    expected_outcome,
                    method.name,
                    rules,
                    seed,
                    case_seed,
                    invalid_values,
                    variation_count=variation_count,
                    variation_protected_fields=variation_protected_fields,
                )
            result.append(
                _case_document(
                    base,
                    entity_name,
                    record_index,
                    len(result) + 1,
                    match_code,
                    operation,
                    expected_outcome,
                    resolved,
                    rules,
                    method,
                )
            )

    if match_code.collision_count:
        assert collision_method is not None
        collision = _matching_method(rules, collision_method)
        for _ in range(match_code.collision_count):
            record_index, base = next_source(method, collision)
            resolved = _resolve_case(
                base,
                "COLLISION",
                (),
                ExpectedOutcome.NO_MATCH,
                method.name,
                rules,
                seed,
                _case_seed(seed, entity_name, method.name, "collision", len(result) + 1),
                invalid_values,
                collision_method,
                variation_count,
                variation_protected_fields,
            )
            result.append(
                _case_document(
                    base,
                    entity_name,
                    record_index,
                    len(result) + 1,
                    match_code,
                    "COLLISION",
                    ExpectedOutcome.NO_MATCH,
                    resolved,
                    rules,
                    method,
                    collision_method=collision_method,
                )
            )

    for configured_case in match_code.deterministic_cases:
        raw_modifications = cast(list[Mapping[str, object]], configured_case["modifications"])
        modifications = tuple(
            FieldModification(
                OperationType(str(definition["type"]).upper()),
                tuple(str(field).strip() for field in cast(list[str], definition["fields"])),
                str(definition["condition"]) if "condition" in definition else None,
                cast(Mapping[str, object], definition.get("values"))
                if isinstance(definition.get("values"), Mapping)
                else None,
            )
            for definition in raw_modifications
        )
        expected_value = configured_case.get("expected_outcome")
        expected_outcome = (
            ExpectedOutcome(str(expected_value))
            if expected_value is not None
            else _inferred_outcome(modifications, method.mandatory_fields)
        )
        for _ in range(cast(int, configured_case.get("count", 1))):
            record_index, base = next_source(method)
            resolved = _resolve_case(
                base,
                "CUSTOM",
                modifications,
                expected_outcome,
                method.name,
                rules,
                seed,
                _case_seed(seed, entity_name, method.name, "custom", len(result) + 1),
                invalid_values,
                variation_count=variation_count,
                variation_protected_fields=variation_protected_fields,
            )
            result.append(
                _case_document(
                    base,
                    entity_name,
                    record_index,
                    len(result) + 1,
                    match_code,
                    "CUSTOM",
                    expected_outcome,
                    resolved,
                    rules,
                    method,
                    case_name=(str(configured_case["name"]) if "name" in configured_case else None),
                )
            )
    return result


def _ordered_operations(counts: Mapping[str, int]) -> tuple[str, ...]:
    """Return a stable operation order independent of JSON member ordering."""
    order = (
        "UPDATE",
        "INVALID",
        "MISSING",
        "EMPTY",
        "DUPLICATE",
        "WEIGHT_BELOW_LIMIT",
        "WEIGHT_AT_LIMIT",
        "WEIGHT_ABOVE_LIMIT",
        "ELASTICITY_INSIDE",
        "ELASTICITY_AT_LIMIT",
        "ELASTICITY_OUTSIDE",
    )
    return tuple(operation for operation in order if counts.get(operation, 0) > 0)


def _select_operation_field(
    base: Mapping[str, object],
    method: MatchingMethod,
    operation: str,
    profile: str,
    invalid_values: Mapping[str, tuple[object, ...]],
    cursor: int,
) -> tuple[str, int]:
    """Select the next operation-capable method field in catalog order."""
    for offset in range(len(method.fields)):
        index = (cursor + offset) % len(method.fields)
        field = method.fields[index]
        if not _field_present(base, field):
            continue
        if operation == "INVALID" and not supports_invalid_value(invalid_values, field, profile):
            continue
        return field, index + 1
    raise ValueError(f"Matching method {method.name!r} has no field eligible for {operation}")


def _select_elastic_field(
    base: Mapping[str, object],
    method: MatchingMethod,
    rules: EntityRules,
    cursor: int,
) -> tuple[str, int]:
    """Select the next mandatory anchor with a supported non-zero elasticity."""
    for offset in range(len(method.fields)):
        index = (cursor + offset) % len(method.fields)
        field = method.fields[index]
        elasticity = method.elasticity_for(field, rules.fields[field].elasticity)
        if (
            field in method.mandatory_fields
            and _field_present(base, field)
            and elasticity not in {"", "0", "exact"}
        ):
            return field, index + 1
    raise ValueError(f"Matching method {method.name!r} has no elastic mandatory field")


def _resolve_exact_weight_case(
    base: Mapping[str, object],
    operation: str,
    method: MatchingMethod,
    rules: EntityRules,
    seed: int,
    index: int,
    invalid_values: Mapping[str, tuple[object, ...]],
    variation_count: int,
    variation_protected_fields: tuple[str, ...],
) -> ResolvedUpdate:
    """Create a matching-score boundary fixture for the selected method."""
    relation = _WEIGHT_OPERATIONS[operation]
    included, excluded, score = _weight_selection(base, method, rules, relation)
    if relation == "BELOW_LIMIT" and not excluded:
        raise ValueError(
            f"Matching method {method.name!r} cannot generate BELOW_LIMIT "
            "without a populated optional field to diverge"
        )
    expected_outcome = (
        ExpectedOutcome.NO_MATCH if relation == "BELOW_LIMIT" else ExpectedOutcome.MATCH
    )
    modifications = tuple(FieldModification(OperationType.UPDATE, (field,)) for field in excluded)
    resolved = resolve_match_fixture(
        base,
        UpdateRequest(
            operation=OperationType.DUPLICATE,
            matching_method=method.name,
            expected_outcome=expected_outcome,
            invalid_values=invalid_values,
            include=included,
            exclude=excluded,
            modifications=modifications,
            variation_count=variation_count,
            variation_protected_fields=variation_protected_fields,
        ),
        rules,
        seed,
        index,
    )
    assessment = assess_match(base, resolved.record, rules, method)
    if assessment.matched_weight != score:
        raise ValueError(
            f"Matching method {method.name!r} produced weight "
            f"{assessment.matched_weight} instead of planned weight {score}"
        )
    return resolved


def _weight_selection(
    base: Mapping[str, object],
    method: MatchingMethod,
    rules: EntityRules,
    relation: str,
) -> tuple[tuple[str, ...], tuple[str, ...], Decimal]:
    """Select a deterministic optional-field subset for one score boundary."""
    available_optional = tuple(
        field for field in method.optional_fields if _field_present(base, field)
    )
    mandatory_weight = sum(
        (rules.fields[field].weight for field in method.mandatory_fields), Decimal("0")
    )
    combinations: dict[Decimal, tuple[str, ...]] = {mandatory_weight: ()}
    for field in available_optional:
        field_weight = rules.fields[field].weight
        additions = {
            weight + field_weight: selected + (field,) for weight, selected in combinations.items()
        }
        for weight, selected in additions.items():
            combinations.setdefault(weight, selected)
    if relation == "BELOW_LIMIT":
        candidates = tuple(weight for weight in combinations if weight < method.needed_weight)
        score = max(candidates) if candidates else None
    elif relation == "AT_LIMIT":
        score = method.needed_weight if method.needed_weight in combinations else None
    else:
        candidates = tuple(weight for weight in combinations if weight > method.needed_weight)
        score = min(candidates) if candidates else None
    if score is None:
        raise ValueError(
            f"Matching method {method.name!r} cannot generate {relation}: "
            f"required weight is {method.needed_weight}"
        )
    included = combinations[score]
    excluded = tuple(field for field in available_optional if field not in included)
    return included, excluded, score


def _resolve_collision_method(
    match_code: MatchFixtureCodeConfig,
    configured_codes: Sequence[MatchFixtureCodeConfig],
    rules: EntityRules,
    target: MatchingMethod,
) -> str | None:
    """Resolve and validate one explicit or deterministic automatic collision target."""
    if match_code.collision_count == 0:
        return None
    candidates: tuple[MatchingMethod, ...]
    if match_code.collision_method is not None:
        candidates = (_matching_method(rules, match_code.collision_method),)
    else:
        configured_names = {
            item.matching_method for item in configured_codes if item.matching_method != target.name
        }
        candidates = tuple(
            method
            for method in sorted(rules.methods, key=lambda item: item.priority)
            if method.name in configured_names
        )
    for candidate in candidates:
        if candidate.name == target.name:
            continue
        if any(field not in candidate.mandatory_fields for field in target.mandatory_fields):
            return candidate.name
    requested = match_code.collision_method or "another configured method"
    raise ValueError(
        f"Matching method {target.name!r} cannot produce a collision against {requested!r}"
    )


def _case_document(
    base: Mapping[str, object],
    entity_name: str,
    record_index: int,
    case_index: int,
    match_code: MatchFixtureCodeConfig,
    operation: str,
    expected_outcome: ExpectedOutcome | None,
    resolved: ResolvedUpdate,
    rules: EntityRules,
    method: MatchingMethod,
    *,
    collision_method: str | None = None,
    case_name: str | None = None,
) -> dict[str, object]:
    """Create one self-describing existing/incoming fixture envelope."""
    assessment = assess_match(base, resolved.record, rules, method)
    all_assessments = tuple(
        assess_match(base, resolved.record, rules, candidate) for candidate in rules.methods
    )
    threshold_relation = resolved.threshold_relation
    if operation in _WEIGHT_OPERATIONS:
        threshold_relation = _WEIGHT_OPERATIONS[operation].lower().removesuffix("_limit")
    result: dict[str, object] = {
        "case_id": case_index,
        "entity": entity_name,
        "entity_record": record_index,
        "match_code": match_code.matching_method,
        "matching_method": match_code.matching_method,
        "operation": operation,
        "expected_outcome": expected_outcome.value if expected_outcome is not None else None,
        "actual_match": assessment.matched,
        "matched_methods": [item.method_id for item in all_assessments if item.matched],
        "unexpected_methods": list(resolved.unexpected_methods),
        "changed_fields": list(resolved.changed_fields),
        "removed_fields": list(resolved.removed_fields),
        "synchronized_fields": list(resolved.synchronized_fields),
        "total_weight": str(resolved.total_weight),
        "match_weight": str(assessment.matched_weight),
        "required_weight": str(method.needed_weight),
        "threshold_relation": threshold_relation,
        "expected_apply": resolved.expected_apply,
        "modification_plan": [_plan_item(item) for item in resolved.modification_plan],
        "variation": {
            "requested_count": resolved.variation_requested_count,
            "applied_fields": list(resolved.variation_fields),
        },
        "existing": dict(base),
        "record": resolved.record,
    }
    if collision_method is not None:
        result["collision_method"] = collision_method
    if case_name is not None:
        result["case_name"] = case_name
    return result


def _build_cases(
    base: Mapping[str, object],
    entity_name: str,
    record_index: int,
    match_code: MatchFixtureCodeConfig,
    rules: EntityRules,
    mandatory_fields: tuple[str, ...],
    seed: int,
    invalid_values: Mapping[str, tuple[object, ...]],
    variation_count: int,
    variation_protected_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    """Build randomized operation-count cases and explicit multi-field cases."""
    randomizer = Random(_case_seed(seed, entity_name, record_index, match_code.name))
    plans: list[tuple[str, tuple[FieldModification, ...], ExpectedOutcome | None]] = []
    available = tuple(field for field in rules.fields if _field_present(base, field))
    method_fields = tuple(
        field
        for field in _matching_method(rules, match_code.matching_method).fields
        if field in available
    )
    if not method_fields:
        raise ValueError(
            f"matchCode {match_code.name!r} has no fields present in generated "
            f"{entity_name!r} record"
        )
    for operation, count in match_code.operation_counts.items():
        for _ in range(count):
            if operation in _WEIGHT_OPERATIONS:
                plans.append((operation, (), None))
                continue
            if operation in _ELASTICITY_OPERATIONS:
                outcome = (
                    ExpectedOutcome.NO_MATCH
                    if operation == "ELASTICITY_OUTSIDE"
                    else ExpectedOutcome.MATCH
                )
                plans.append((operation, (), outcome))
                continue
            field = randomizer.choice(method_fields)
            modification = FieldModification(OperationType(operation), (field,))
            plans.append(
                (
                    operation,
                    (modification,),
                    _inferred_outcome((modification,), mandatory_fields),
                )
            )
    randomizer.shuffle(plans)
    for configured_case in match_code.deterministic_cases:
        raw_modifications = cast(list[Mapping[str, object]], configured_case["modifications"])
        modifications = tuple(
            FieldModification(
                OperationType(str(definition["type"]).upper()),
                tuple(str(field).strip() for field in cast(list[str], definition["fields"])),
                str(definition["condition"]) if "condition" in definition else None,
            )
            for definition in raw_modifications
        )
        expected = configured_case.get("expected_outcome")
        outcome = (
            ExpectedOutcome(str(expected))
            if expected is not None
            else _inferred_outcome(modifications, mandatory_fields)
        )
        case_count = cast(int, configured_case.get("count", 1))
        for _ in range(case_count):
            plans.append(("DETERMINISTIC", modifications, outcome))

    result: list[dict[str, object]] = []
    for case_index, (operation, modifications, expected_outcome) in enumerate(plans, start=1):
        resolved = _resolve_case(
            base,
            operation,
            modifications,
            expected_outcome,
            match_code.matching_method,
            rules,
            seed,
            _case_seed(seed, entity_name, record_index, match_code.name, case_index),
            invalid_values,
            variation_count=variation_count,
            variation_protected_fields=variation_protected_fields,
        )
        assessment = assess_match(
            base,
            resolved.record,
            rules,
            _matching_method(rules, match_code.matching_method),
        )
        all_assessments = tuple(
            assess_match(base, resolved.record, rules, method) for method in rules.methods
        )
        result.append(
            {
                "case_id": case_index,
                "entity": entity_name,
                "entity_record": record_index,
                "match_code": match_code.name,
                "matching_method": match_code.matching_method,
                "operation": operation,
                "expected_outcome": (
                    expected_outcome.value if expected_outcome is not None else None
                ),
                "actual_match": assessment.matched,
                "matched_methods": [item.method_id for item in all_assessments if item.matched],
                "unexpected_methods": list(resolved.unexpected_methods),
                "changed_fields": list(resolved.changed_fields),
                "removed_fields": list(resolved.removed_fields),
                "synchronized_fields": list(resolved.synchronized_fields),
                "total_weight": str(resolved.total_weight),
                "threshold_relation": resolved.threshold_relation,
                "expected_apply": resolved.expected_apply,
                "modification_plan": [_plan_item(item) for item in resolved.modification_plan],
                "variation": {
                    "requested_count": resolved.variation_requested_count,
                    "applied_fields": list(resolved.variation_fields),
                },
                "existing": dict(base),
                "record": resolved.record,
            }
        )
    return result


def _resolve_case(
    base: Mapping[str, object],
    operation: str,
    modifications: tuple[FieldModification, ...],
    expected_outcome: ExpectedOutcome | None,
    matching_method: str,
    rules: EntityRules,
    seed: int,
    index: int,
    invalid_values: Mapping[str, tuple[object, ...]],
    collision_method: str | None = None,
    variation_count: int = 0,
    variation_protected_fields: tuple[str, ...] = (),
) -> ResolvedUpdate:
    """Use match verification for field plans and native selection for weights."""
    if operation in _WEIGHT_OPERATIONS:
        return resolve_update(
            base,
            UpdateRequest(
                operation=OperationType.WEIGHT_CHANGE,
                matching_method=matching_method,
                condition=_WEIGHT_OPERATIONS[operation],
                invalid_values=invalid_values,
                variation_count=variation_count,
                variation_protected_fields=variation_protected_fields,
            ),
            rules,
            seed,
            index,
        )
    if operation in _ELASTICITY_OPERATIONS:
        method = _matching_method(rules, matching_method)
        field = modifications[0].fields[0] if modifications else None
        if field is None:
            field = next(
                (
                    candidate
                    for candidate in method.mandatory_fields
                    if method.elasticity_for(candidate, rules.fields[candidate].elasticity)
                    not in {"", "0", "exact"}
                ),
                None,
            )
        if field is None:
            raise ValueError(f"Matching method {matching_method!r} has no elastic mandatory field")
        assert expected_outcome is not None
        return resolve_match_fixture(
            base,
            UpdateRequest(
                operation=OperationType.DUPLICATE,
                matching_method=matching_method,
                expected_outcome=expected_outcome,
                invalid_values=invalid_values,
                failure_field=field,
                elasticity_boundary=_ELASTICITY_OPERATIONS[operation],
                variation_count=variation_count,
                variation_protected_fields=variation_protected_fields,
            ),
            rules,
            seed,
            index,
        )
    if operation == "COLLISION":
        if collision_method is None:
            raise ValueError("COLLISION requires a collision matching method")
        return resolve_match_fixture(
            base,
            UpdateRequest(
                operation=OperationType.DUPLICATE,
                matching_method=matching_method,
                expected_outcome=ExpectedOutcome.NO_MATCH,
                failure_mode=FailureMode.CROSS_METHOD_COLLISION,
                collision_method=collision_method,
                invalid_values=invalid_values,
                variation_count=variation_count,
                variation_protected_fields=variation_protected_fields,
            ),
            rules,
            seed,
            index,
        )
    assert expected_outcome is not None
    return resolve_match_fixture(
        base,
        UpdateRequest(
            operation=OperationType.DUPLICATE,
            matching_method=matching_method,
            expected_outcome=expected_outcome,
            invalid_values=invalid_values,
            modifications=modifications,
            variation_count=variation_count,
            variation_protected_fields=variation_protected_fields,
        ),
        rules,
        seed,
        index,
    )


def _inferred_outcome(
    modifications: tuple[FieldModification, ...], mandatory_fields: tuple[str, ...]
) -> ExpectedOutcome:
    """Keep positive fixtures positive unless a selected anchor must diverge."""
    if any(
        operation.operation != OperationType.DUPLICATE and field in mandatory_fields
        for operation in modifications
        for field in operation.fields
    ):
        return ExpectedOutcome.NO_MATCH
    return ExpectedOutcome.MATCH


def _matching_method(rules: EntityRules, name: str) -> MatchingMethod:
    method = next((item for item in rules.methods if item.name == name), None)
    if method is None:
        raise ValueError(f"Unknown matching method {name!r} for entity {rules.entity!r}")
    return method


def _field_present(record: Mapping[str, object], field: str) -> bool:
    if field in record:
        return True
    for value in record.values():
        if isinstance(value, Mapping) and _field_present(value, field):
            return True
        if isinstance(value, list) and any(
            isinstance(item, Mapping) and _field_present(item, field) for item in value
        ):
            return True
    return False


def _case_seed(seed: int, *parts: object) -> int:
    """Derive a stable per-file/per-case seed without Python hash randomization."""
    text = "|".join(str(part) for part in parts)
    value = seed
    for character in text:
        value = (value * 131 + ord(character)) % (2**63 - 1)
    return value


def _plan_item(item: FieldModification) -> dict[str, object]:
    result: dict[str, object] = {"operation": item.operation.value, "fields": list(item.fields)}
    if item.condition is not None:
        result["condition"] = item.condition
    return result


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)
