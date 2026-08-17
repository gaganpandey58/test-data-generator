"""Contract checks for generated update fixtures."""

from collections.abc import Mapping

from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import ResolvedUpdate, UpdateRequest, UpdateScenario


def validate_update_contract(
    base: Mapping[str, object],
    updated: Mapping[str, object],
    request: UpdateRequest,
    resolved: ResolvedUpdate,
    rules: EntityRules,
) -> None:
    """Ensure an update changes only what its scenario allows."""
    if request.scenario != UpdateScenario.INVALID_KEY:
        for key in rules.keys:
            if base.get(key) != updated.get(key):
                raise ValueError(f"Update changed protected matching key {key!r}")
    if request.scenario == UpdateScenario.UPDATE_SINGLE_FIELD and len(resolved.changed_fields) != 1:
        raise ValueError("UPDATE_SINGLE_FIELD did not change exactly one field")
    if request.scenario in {
        UpdateScenario.MISSING_REQUIRED_FIELD,
        UpdateScenario.MISSING_MULTIPLE_FIELDS,
        UpdateScenario.MISSING_SELECTED_FIELDS,
    }:
        for field in resolved.removed_fields:
            if _find_field(updated, field) is not _MISSING:
                raise ValueError(f"Selected field {field!r} was not removed")
    if resolved.expected_match and not resolved.invalidated_keys:
        for key in rules.keys:
            if _find_field(base, key) != _find_field(updated, key):
                raise ValueError(f"Update no longer preserves matching key {key!r}")


_MISSING = object()


def _find_field(record: Mapping[str, object], field: str) -> object:
    if field in record:
        return record[field]
    for value in record.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    found = _find_field(item, field)
                    if found is not _MISSING:
                        return found
        elif isinstance(value, Mapping):
            found = _find_field(value, field)
            if found is not _MISSING:
                return found
    return _MISSING
