"""Load immutable, checked-in GDF source-layout metadata.

Layout JSON files capture the supported root fields and nested field groups for
each healthcare entity profile.  They are package resources rather than user
configuration so generated data remains constrained to reviewed source layouts.
"""

import json
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class LayoutField:
    """Describe one canonical source field from a GDF layout.

    Attributes:
        name: Field name used in the generated source-shaped record.
        type: GDF field type label retained for layout consumers.
        max_length: Maximum allowed field length from the source layout.
    """

    name: str
    type: str
    max_length: int


@dataclass(frozen=True)
class LayoutProfile:
    """Describe one source-shaped record profile and its nested field groups.

    Attributes:
        profile: Stable profile identifier used by configuration and generators.
        headers: Ordered transport fields acknowledged by the profile.
        root: Ordered fields emitted at the root record level.
        groups: Immutable mapping of nested group names to their ordered fields.
        required_parent_fields: Parent fields that a nested group must retain
            as structural source references.
    """

    profile: str
    headers: tuple[LayoutField, ...]
    root: tuple[LayoutField, ...]
    groups: Mapping[str, tuple[LayoutField, ...]]
    required_parent_fields: Mapping[str, frozenset[str]]


def available_profiles() -> frozenset[str]:
    """Return identifiers for every checked-in GDF layout profile.

    The package-resource scan keeps the supported-profile list synchronized
    with the versioned registry without exposing arbitrary filesystem paths.

    Returns:
        Immutable set of supported layout profile names.
    """
    return frozenset(
        resource.name.removesuffix(".json")
        for resource in files(__package__).iterdir()
        if resource.name.endswith(".json")
    )


def load_layout(profile: str) -> LayoutProfile:
    """Load one layout profile from the packaged GDF registry.

    Args:
        profile: Checked-in profile identifier.

    Returns:
        Immutable normalized field and nested-group metadata. Nested mappings
        are wrapped to prevent callers from mutating loaded registry state.

    Raises:
        ValueError: If the profile is unknown or malformed.
    """
    if profile not in available_profiles():
        raise ValueError(f"Unknown layout profile {profile!r}")
    resource = files(__package__).joinpath(f"{profile}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        headers = tuple(_field(value) for value in raw.get("headers", []))
        root = tuple(_field(value) for value in raw["root"])
        groups = MappingProxyType(
            {
                name: tuple(_field(value) for value in values)
                for name, values in raw["groups"].items()
            }
        )
        required_parent_fields = MappingProxyType(
            {
                group: frozenset(values)
                for group, values in raw.get("required_parent_fields", {}).items()
            }
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"Malformed layout profile {profile!r}") from error
    if raw.get("profile") != profile:
        raise ValueError(f"Malformed layout profile {profile!r}")
    return LayoutProfile(
        profile=profile,
        headers=headers,
        root=root,
        groups=groups,
        required_parent_fields=required_parent_fields,
    )


def project_record(record: Mapping[str, object], profile: str) -> dict[str, object]:
    """Project one generated record to the fields declared by its layout.

    Layout metadata is the single output-selection contract: root and header
    fields are retained only when declared, and every nested group is projected
    to its declared child fields. This generic operation lets future profiles
    alter output shape without changing entity generator code.

    Args:
        record: Complete candidate record produced by an entity module.
        profile: Checked-in source-layout profile identifier.

    Returns:
        A record containing only the selected declared fields.
    """
    layout = load_layout(profile)
    allowed = {field.name for field in (*layout.headers, *layout.root)}
    projected = {key: value for key, value in record.items() if key in allowed}
    for group_name, fields in layout.groups.items():
        group = record.get(group_name)
        if not isinstance(group, list):
            continue
        allowed_group = {field.name for field in fields}
        projected[group_name] = [
            deduplicate_nested_fields(
                {key: value for key, value in item.items() if key in allowed_group},
                projected,
                layout.required_parent_fields.get(group_name, frozenset()),
            )
            for item in group
            if isinstance(item, Mapping)
        ]
    return projected


def deduplicate_nested_fields(
    nested: Mapping[str, object],
    parent: Mapping[str, object],
    required_parent_fields: frozenset[str],
) -> dict[str, object]:
    """Remove redundant parent fields from one nested object declaratively.

    Args:
        nested: Candidate nested object after layout field selection.
        parent: Projected parent record supplying values to compare.
        required_parent_fields: Layout-declared relationship references that
            must remain in the nested object even when duplicated at the root.

    Returns:
        Nested data without redundant parent fields, retaining explicit
        structural relationship references.
    """
    return {
        key: value
        for key, value in nested.items()
        if key not in parent or key in required_parent_fields
    }


def _field(value: object) -> LayoutField:
    """Normalize one registry field definition.

    Args:
        value: Decoded JSON field definition.

    Returns:
        Immutable layout field with the three canonical metadata attributes.

    Raises:
        ValueError: If a definition does not carry canonical field metadata.
    """
    if not isinstance(value, dict):
        raise ValueError("layout field must be an object")
    name = value.get("name")
    field_type = value.get("type")
    max_length = value.get("max_length")
    if (
        not isinstance(name, str)
        or not isinstance(field_type, str)
        or not isinstance(max_length, int)
    ):
        raise ValueError("layout field must have name, type, and max_length")
    return LayoutField(name=name, type=field_type, max_length=max_length)
