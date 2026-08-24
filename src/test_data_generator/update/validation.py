"""Contract checks for generated update fixtures."""

from collections.abc import Mapping

from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import OperationType, ResolvedUpdate, UpdateRequest


def validate_update_contract(
    base: Mapping[str, object],
    updated: Mapping[str, object],
    request: UpdateRequest,
    resolved: ResolvedUpdate,
    rules: EntityRules,
) -> None:
    """Ensure an update changes only what its operation allows."""
    if request.operation not in {OperationType.INVALID, OperationType.MISSING}:
        for key in rules.keys:
            if base.get(key) != updated.get(key):
                raise ValueError(f"Update changed protected matching key {key!r}")
    if request.operation == OperationType.MISSING:
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
