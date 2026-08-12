"""Load synthetic defaults from checked-in source-data type patterns.

``sample_shapes.json`` is generated from the supplied provider, member, claim,
and payment examples. It records field names, nesting, and JSON types only;
it intentionally stores no source values. Entity builders use this module to
fill optional fields with safe synthetic blanks while retaining the values they
generate deliberately.
"""

import json
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from typing import cast


def available_sources() -> frozenset[str]:
    """Return the supplied sample types represented by the packaged patterns.

    Returns:
        Immutable source identifiers for provider, member, professional and
        institutional claims, and their corresponding payment examples.
    """
    return frozenset(_sources())


def blank_record(source: str) -> dict[str, object]:
    """Create a source-shaped record with safe synthetic blank values.

    Args:
        source: Packaged sample-pattern identifier.

    Returns:
        New mutable record containing every source field with an empty value
        of the same JSON kind.
    """
    return cast(dict[str, object], _blank(_root(source)))


def complete_record(record: Mapping[str, object], *sources: str) -> dict[str, object]:
    """Fill and type-normalize a record against one or more source patterns.

    Earlier sources are authoritative for fields they share with later ones;
    later sources only add missing fields. This lets a claim builder use its
    claim sample first and its matching payment sample second without letting
    a payment header silently change a claim field's JSON kind.

    Args:
        record: Generated values to retain wherever their JSON type matches.
        sources: One or more packaged source-pattern identifiers.

    Returns:
        A source-complete synthetic record. Source values are never read or
        copied; only field names, nesting, and JSON kinds are used.
    """
    descriptor: dict[str, object] = {}
    for source in sources:
        descriptor = _add_missing_descriptors(descriptor, _root(source))
    return _merge(descriptor, {}, record)


@lru_cache(maxsize=1)
def _sources() -> Mapping[str, object]:
    """Read and validate the one packaged sample-pattern document."""
    try:
        raw = json.loads(files("healthcare_test_data").joinpath("sample_shapes.json").read_text())
        sources = raw["sources"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("Could not load packaged sample_shapes.json") from error
    if not isinstance(sources, dict) or not all(isinstance(name, str) for name in sources):
        raise RuntimeError("sample_shapes.json must contain named source patterns")
    return cast(Mapping[str, object], sources)


def _root(source: str) -> Mapping[str, object]:
    """Return the validated root descriptor for one sample source."""
    try:
        descriptor = _sources()[source]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Unknown sample source {source!r}") from error
    if not isinstance(descriptor, dict):
        raise RuntimeError(f"Malformed sample pattern for {source!r}")
    return cast(Mapping[str, object], descriptor)


def _merge(
    descriptor: Mapping[str, object],
    existing: Mapping[str, object],
    generated: Mapping[str, object],
) -> dict[str, object]:
    """Merge generated values into source fields and normalize their JSON kinds."""
    result: dict[str, object] = {}
    for name, child in descriptor.items():
        if not isinstance(name, str):
            raise RuntimeError("Sample pattern field names must be strings")
        value = generated.get(name, existing.get(name, _blank(child)))
        result[name] = _coerce(value, child)
    for name, value in existing.items():
        result.setdefault(name, value)
    for name, value in generated.items():
        result.setdefault(name, value)
    return result


def _add_missing_descriptors(
    authoritative: Mapping[str, object],
    additional: Mapping[str, object],
) -> dict[str, object]:
    """Add only missing sample descriptors without replacing earlier kinds.

    Nested objects are merged recursively so a secondary source can contribute
    its unique child fields. Lists and scalar descriptors stay owned by the
    first source because their JSON structure/type is already authoritative.

    Args:
        authoritative: Descriptor mapping selected by an earlier source.
        additional: Descriptor mapping from a later supplemental source.

    Returns:
        Combined descriptor mapping retaining every earlier descriptor.
    """
    combined = dict(authoritative)
    for name, descriptor in additional.items():
        current = combined.get(name)
        if isinstance(current, Mapping) and isinstance(descriptor, Mapping):
            combined[name] = _add_missing_descriptors(current, descriptor)
        else:
            combined.setdefault(name, descriptor)
    return combined


def _blank(descriptor: object) -> object:
    """Return a safe empty value matching one JSON-kind descriptor."""
    if descriptor == "s":
        return ""
    if descriptor == "i":
        return 0
    if descriptor == "n":
        return 0.0
    if descriptor == "b":
        return False
    if descriptor == "z":
        return None
    if isinstance(descriptor, Mapping):
        return {str(name): _blank(value) for name, value in descriptor.items()}
    if isinstance(descriptor, list) and len(descriptor) == 1:
        return []
    raise RuntimeError(f"Unknown sample JSON kind {descriptor!r}")


def _coerce(value: object, descriptor: object) -> object:
    """Represent a generated value with the source pattern's JSON kind."""
    if descriptor == "s":
        return str(value)
    if descriptor == "i":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float):
            return int(round(value))
        return int(value) if isinstance(value, str) and value.isdigit() else 0
    if descriptor == "n":
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
        )
    if descriptor == "b":
        return bool(value)
    if descriptor == "z":
        return None
    if isinstance(descriptor, Mapping):
        return _merge(descriptor, {}, value) if isinstance(value, Mapping) else _blank(descriptor)
    if isinstance(descriptor, list) and len(descriptor) == 1:
        return [_coerce(item, descriptor[0]) for item in value] if isinstance(value, list) else []
    raise RuntimeError(f"Unknown sample JSON kind {descriptor!r}")
