"""Load checked-in GDF source-layout profiles."""

import json
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class LayoutField:
    """Describe one canonical GDF field."""

    name: str
    type: str
    max_length: int


@dataclass(frozen=True)
class LayoutProfile:
    """Describe a source-shaped record profile and its nested groups."""

    profile: str
    root: tuple[LayoutField, ...]
    groups: Mapping[str, tuple[LayoutField, ...]]


def available_profiles() -> frozenset[str]:
    """Return the checked-in profile identifiers.

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
        Immutable normalized field and nested-group metadata.

    Raises:
        ValueError: If the profile is unknown or malformed.
    """
    if profile not in available_profiles():
        raise ValueError(f"Unknown layout profile {profile!r}")
    resource = files(__package__).joinpath(f"{profile}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        root = tuple(_field(value) for value in raw["root"])
        groups = MappingProxyType(
            {
                name: tuple(_field(value) for value in values)
                for name, values in raw["groups"].items()
            }
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"Malformed layout profile {profile!r}") from error
    if raw.get("profile") != profile:
        raise ValueError(f"Malformed layout profile {profile!r}")
    return LayoutProfile(profile=profile, root=root, groups=groups)


def _field(value: object) -> LayoutField:
    """Normalize one registry field definition.

    Args:
        value: Decoded JSON field definition.

    Returns:
        Immutable layout field.

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
