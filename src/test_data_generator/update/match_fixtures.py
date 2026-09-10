"""Emit per-record, per-matchCode JSON fixture matrices.

The normal generator writes production-shaped JSONL streams.  This module is
an opt-in QA fixture layer: it derives every case from an already-generated
base record and delegates all field semantics to the existing rules, mutation,
matching, invalid-catalog, and synchronization engines.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    FieldModification,
    OperationType,
    ResolvedUpdate,
    UpdateRequest,
    load_invalid_values,
    resolve_update,
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
    """Write one ``<matching-method>.json`` array beneath each source folder.

    Every array entry has both the original record and one derived fixture,
    together with enough metadata to independently verify the selected method,
    operation plan, changed fields, matching result, and weight relation.
    """
    paths: list[Path] = []
    invalid_values = (
        load_invalid_values(invalid_values_catalog) if invalid_values_catalog is not None else {}
    )
    for entity_config in fixture_entities:
        records = generated_records.get(entity_config.entity)
        if records is None:
            raise ValueError(
                f"Match-fixture entity {entity_config.entity!r} has no generated source records"
            )
        rules = rules_catalog.get(entity_config.entity)
        if rules is None:
            raise ValueError(
                f"Match-fixture entity {entity_config.entity!r} has no update rule catalog"
            )
        for record_index, base in enumerate(records, start=1):
            entity_directory = output_directory / f"{entity_config.entity}{record_index}"
            entity_directory.mkdir(parents=True, exist_ok=True)
            for match_code in entity_config.match_codes:
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
    return tuple(paths)


def _build_cases(
    base: Mapping[str, object],
    entity_name: str,
    record_index: int,
    match_code: MatchFixtureCodeConfig,
    rules: EntityRules,
    mandatory_fields: tuple[str, ...],
    seed: int,
    invalid_values: Mapping[str, tuple[object, ...]],
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
            ),
            rules,
            seed,
            index,
        )
    if operation in _ELASTICITY_OPERATIONS:
        method = _matching_method(rules, matching_method)
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
