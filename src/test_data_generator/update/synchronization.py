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
    synchronized.update(_synchronize_self_patient_subscriber(original, updated, changed))
    synchronized.update(_synchronize_ch_cd_pairs(original, updated, changed))
    synchronized.update(
        _synchronize_provider_npi(
            original, updated, changed, "CP_PROVIDER_NPI", "CP_PRESCRIBING_PROVIDER_NPI"
        )
    )
    synchronized.update(_synchronize_nppes_entity_type(original, updated, changed))
    return tuple(sorted(synchronized))


def synchronization_field_closure(record: Mapping[str, object], fields: set[str]) -> set[str]:
    """Return fields connected through the synchronizer's dependency graph.

    Variation uses the undirected closure deliberately: if either side of a
    derived/equivalent relationship is protected, no incidental variation may
    alter the other side and flow back into the protected logical value.
    """
    available = _field_names(record)
    relationships: list[set[str]] = []

    for field in available:
        if not field.endswith(("_FIRST_NAME", "_MIDDLE_NAME", "_LAST_NAME")):
            continue
        prefix = field.rsplit("_", 2)[0]
        group = {
            name
            for name in (
                f"{prefix}_FIRST_NAME",
                f"{prefix}_MIDDLE_NAME",
                f"{prefix}_LAST_NAME",
                f"{prefix}_FULL_NAME",
            )
            if name in available
        }
        if len(group) > 1:
            relationships.append(group)

    for field in available:
        if field.startswith("CH_") and f"CD_{field[3:]}" in available:
            relationships.append({field, f"CD_{field[3:]}"})

    for relationship in (
        {"CP_PROVIDER_NPI", "CP_PRESCRIBING_PROVIDER_NPI"},
        {"ENTITY_TYPE_CODE", "ENTITY_TYPE_DESCRIPTION"},
    ):
        present = relationship.intersection(available)
        if len(present) > 1:
            relationships.append(present)

    if str(_value_at(record, "CH_PATIENT_RELATIONSHIP_TO_SUBSCRIBER", 0)) == "18":
        for patient, subscriber in _PATIENT_SUBSCRIBER_PAIRS:
            present = {patient, subscriber}.intersection(available)
            if len(present) > 1:
                relationships.append(present)

    result = set(fields)
    changed = True
    while changed:
        changed = False
        for relationship in relationships:
            if result.intersection(relationship) and not relationship.issubset(result):
                result.update(relationship)
                changed = True
    return result


def _synchronize_nppes_entity_type(
    original: Mapping[str, object], updated: dict[str, object], changed: set[str]
) -> set[str]:
    """Keep NPPES's human-readable type description derived from its code."""
    if "ENTITY_TYPE_CODE" not in changed:
        return set()
    original_description = list(_locations(original, "ENTITY_TYPE_DESCRIPTION"))
    for position, (parent, key) in enumerate(_locations(updated, "ENTITY_TYPE_DESCRIPTION")):
        if position >= len(original_description):
            continue
        old_value = original_description[position][0][original_description[position][1]]
        if not _populated(old_value):
            continue
        entity_type = _value_at(updated, "ENTITY_TYPE_CODE", position)
        if str(entity_type) == "1":
            parent[key] = "Individual"
        elif str(entity_type) == "2":
            parent[key] = "Organization"
        else:
            # INVALID fixtures deliberately preserve their supplied invalid
            # code; the derived description cannot truthfully be inferred.
            parent[key] = old_value
    return {"ENTITY_TYPE_DESCRIPTION"}


def _synchronize_names(
    original: Mapping[str, object], updated: dict[str, object], changed: set[str]
) -> set[str]:
    result: set[str] = set()
    fields = _field_names(original)
    prefixes = {
        field.rsplit("_", 2)[0]
        for field in fields
        if field.endswith(("_FIRST_NAME", "_MIDDLE_NAME", "_LAST_NAME", "_FULL_NAME"))
    }
    for prefix in prefixes:
        full_name = f"{prefix}_FULL_NAME"
        if full_name not in fields:
            continue
        components = tuple(
            f"{prefix}_{name}" for name in ("FIRST_NAME", "MIDDLE_NAME", "LAST_NAME")
        )
        component_changed = changed.intersection(components)
        full_changed = full_name in changed
        if not component_changed and not full_changed:
            continue
        original_targets = list(_locations(original, full_name))
        updated_targets = list(_locations(updated, full_name))
        for position, (parent, key) in enumerate(updated_targets):
            if position >= len(original_targets):
                continue
            original_value = original_targets[position][0][original_targets[position][1]]
            if not _populated(original_value):
                continue
            if component_changed:
                parts = []
                for component in components:
                    value = _value_at(updated, component, position)
                    if _populated(value):
                        parts.append(str(value).strip())
                parent[key] = " ".join(parts)
                result.add(full_name)
            elif full_changed:
                result.update(
                    _synchronize_components_from_full_name(
                        original, updated, prefix, position, parent[key]
                    )
                )
    return result


def _synchronize_components_from_full_name(
    original: Mapping[str, object],
    updated: dict[str, object],
    prefix: str,
    position: int,
    full_value: object,
) -> set[str]:
    """Project an explicitly changed full name into populated existing components."""
    components = tuple(f"{prefix}_{name}" for name in ("FIRST_NAME", "MIDDLE_NAME", "LAST_NAME"))
    populated = [field for field in components if _populated(_value_at(original, field, position))]
    if not populated:
        return set()
    parts = str(full_value).split() if _populated(full_value) else []
    replacements: dict[str, str] = {}
    first, middle, last = components
    if populated == [last]:
        replacements[last] = " ".join(parts)
    elif populated == [first]:
        replacements[first] = " ".join(parts)
    else:
        if first in populated:
            replacements[first] = parts[0] if parts else ""
        if last in populated:
            replacements[last] = parts[-1] if len(parts) > 1 else ""
        if middle in populated:
            replacements[middle] = " ".join(parts[1:-1]) if len(parts) > 2 else ""
    result: set[str] = set()
    for field, value in replacements.items():
        targets = list(_locations(updated, field))
        if position < len(targets):
            target, key = targets[position]
            target[key] = value
            result.add(field)
    return result


_PATIENT_SUBSCRIBER_PAIRS = tuple(
    (f"CH_PATIENT_{suffix}", f"CH_SUBSCRIBER_{suffix}")
    for suffix in (
        "FIRST_NAME",
        "MIDDLE_NAME",
        "LAST_NAME",
        "NAME_SUFFIX",
        "ADDRESS_01",
        "ADDRESS_02",
        "CITY",
        "STATE",
        "ZIP",
        "BIRTH_DATE",
        "GENDER",
    )
)


def _synchronize_self_patient_subscriber(
    original: Mapping[str, object], updated: dict[str, object], changed: set[str]
) -> set[str]:
    """Keep populated demographics aligned when the Patient is the Subscriber."""
    if str(_value_at(original, "CH_PATIENT_RELATIONSHIP_TO_SUBSCRIBER", 0)) != "18":
        return set()
    if str(_value_at(updated, "CH_PATIENT_RELATIONSHIP_TO_SUBSCRIBER", 0)) != "18":
        return set()
    result: set[str] = set()
    for patient, subscriber in _PATIENT_SUBSCRIBER_PAIRS:
        result.update(_synchronize_equivalent_pair(original, updated, changed, patient, subscriber))
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
    if (
        not old_left
        or not old_right
        or not _populated(old_left[0])
        or not _equivalent(old_left[0], old_right[0])
    ):
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
                    parent[key] = _coerce_like(new_value, old_value)
    return {left, right}


def _synchronize_provider_npi(
    original: Mapping[str, object],
    updated: dict[str, object],
    changed: set[str],
    provider_npi: str,
    prescribing_npi: str,
) -> set[str]:
    """Synchronize a matching prescribing NPI without mutating the provider key.

    ``CP_PROVIDER_NPI`` is a matching key.  It may drive the populated
    prescribing representation when the matching key itself is intentionally
    invalidated, but a normal update to the prescribing NPI must never flow
    backward and silently change the matching key.
    """
    if provider_npi not in changed:
        return set()
    return _synchronize_equivalent_pair(original, updated, changed, provider_npi, prescribing_npi)


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


def _equivalent(left: object, right: object) -> bool:
    """Compare populated logical values across string and numeric representations."""
    return str(left).strip() == str(right).strip()


def _coerce_like(value: object, example: object) -> object:
    """Keep a synchronized destination's existing JSON representation type."""
    try:
        if isinstance(example, str):
            return str(value)
        if isinstance(example, int) and not isinstance(example, bool):
            return int(str(value))
        if isinstance(example, float):
            return float(str(value))
    except (TypeError, ValueError):
        # INVALID fixtures deliberately violate the destination's normal JSON
        # representation. Keep the malformed value synchronized instead of
        # crashing while trying to preserve a type it cannot satisfy.
        return value
    return value
