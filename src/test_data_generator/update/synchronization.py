"""Relationship-aware synchronization for update records.

The synchronizer is deliberately conservative: it only changes a related
field when the corresponding field existed and was populated in the original
record. This preserves the distinction between populated, empty, null, and
missing source data while still keeping equivalent values consistent.
"""

from collections.abc import Iterator, Mapping
from typing import Any

Location = tuple[dict[str, object], str]


def synchronize_record(
    original: Mapping[str, object], updated: dict[str, object], changed_fields: tuple[str, ...]
) -> tuple[str, ...]:
    """Synchronize all configured relationships affected by an update."""
    changed = set(changed_fields)
    synchronized: set[str] = set()
    synchronized.update(_synchronize_names(original, updated, changed))
    synchronized.update(_synchronize_ch_cd_pairs(original, updated, changed))
    synchronized.update(
        _synchronize_equivalent_pair(
            original, updated, changed, "CP_PROVIDER_NPI", "CP_PRESCRIBING_PROVIDER_NPI"
        )
    )
    return tuple(sorted(synchronized))


def _synchronize_names(
    original: Mapping[str, object], updated: dict[str, object], changed: set[str]
) -> set[str]:
    result: set[str] = set()
    fields = _field_names(original)
    for field in fields:
        if not field.endswith(("_FIRST_NAME", "_MIDDLE_NAME", "_LAST_NAME")):
            continue
        prefix = field.rsplit("_", 2)[0]
        if not any(name in changed for name in fields if name.startswith(prefix + "_")):
            continue
        full_name = f"{prefix}_FULL_NAME"
        if full_name not in fields:
            continue
        original_targets = list(_locations(original, full_name))
        updated_targets = list(_locations(updated, full_name))
        for position, (parent, key) in enumerate(updated_targets):
            if position >= len(original_targets):
                continue
            original_value = original_targets[position][0][original_targets[position][1]]
            if not _populated(original_value):
                continue
            parts = []
            for component in ("FIRST_NAME", "MIDDLE_NAME", "LAST_NAME"):
                value = _value_at(updated, f"{prefix}_{component}", position)
                if _populated(value):
                    parts.append(str(value).strip())
            parent[key] = " ".join(parts)
            result.add(full_name)
    return result


def _synchronize_ch_cd_pairs(
    original: Mapping[str, object], updated: dict[str, object], changed: set[str]
) -> set[str]:
    result: set[str] = set()
    fields = _field_names(original)
    suffixes = {
        field[3:] for field in fields if field.startswith("CH_") and f"CD_{field[3:]}" in fields
    }
    for suffix in suffixes:
        result.update(
            _synchronize_equivalent_pair(original, updated, changed, f"CH_{suffix}", f"CD_{suffix}")
        )
    return result


def _synchronize_equivalent_pair(
    original: Mapping[str, object],
    updated: dict[str, object],
    changed: set[str],
    left: str,
    right: str,
) -> set[str]:
    if not changed.intersection((left, right)):
        return set()
    old_left = list(_values(original, left))
    old_right = list(_values(original, right))
    if not old_left or not old_right or not _populated(old_left[0]) or old_left[0] != old_right[0]:
        return set()
    source = left if left in changed else right
    new_values = list(_values(updated, source))
    if not new_values:
        return set()
    new_value = new_values[0]
    for target in (left, right):
        original_locations = list(_locations(original, target))
        for position, (parent, key) in enumerate(_locations(updated, target)):
            if position < len(original_locations):
                old_value = original_locations[position][0][original_locations[position][1]]
                if _populated(old_value):
                    parent[key] = new_value
    return {left, right}


def _field_names(record: Mapping[str, object]) -> set[str]:
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


def _locations(record: Mapping[str, object], field: str) -> Iterator[Location]:
    for current, value in record.items():
        if current == field:
            # A tiny holder keeps this iterator usable for read-only mappings;
            # callers only pass dictionaries when they intend to assign.
            yield record if isinstance(record, dict) else {current: value}, current
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    yield from _locations(item, field)
        elif isinstance(value, Mapping):
            yield from _locations(value, field)


def _values(record: Mapping[str, object], field: str) -> Iterator[object]:
    for parent, key in _locations(record, field):
        yield parent[key]


def _value_at(record: Mapping[str, object], field: str, position: int) -> object:
    values = list(_values(record, field))
    return values[position] if position < len(values) else None


def _populated(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))
