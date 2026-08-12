"""Load client-specific headers and values from one checked-in JSON file.

The generator has one small profile file because client differences are data,
not generator code. Add a client under ``client_profiles.json`` when a source
uses different headers or client-owned values.
"""

import json
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping

from healthcare_test_data.layouts import load_layout

_ENTITY_LAYOUTS = {
    "provider": "provider",
    "member": "member",
    "claim_professional": "claim-professional",
    "claim_institutional": "claim-institutional",
}


def available_clients() -> frozenset[str]:
    """Return the client identifiers stored in the profile file."""
    return frozenset(_profiles())


def load_client_headers(client: str, entity: str) -> Mapping[str, object]:
    """Return immutable output-header values for one client and stream.

    Args:
        client: Configured client identifier.
        entity: Internal stream name such as ``member``.

    Returns:
        Header values to merge into the generated record.

    Raises:
        ValueError: If the selected client or stream has no header mapping.
    """
    return MappingProxyType(dict(_section(client, "headers", entity)))


def load_client_values(client: str, entity: str) -> Mapping[str, object]:
    """Return immutable client-owned body values for one output stream.

    Args:
        client: Configured client identifier.
        entity: Internal stream name such as ``provider``.

    Returns:
        Values used by the appropriate entity generator.

    Raises:
        ValueError: If the selected client or stream has no value mapping.
    """
    return MappingProxyType(dict(_section(client, "values", entity)))


def record_header_values(entity: str, headers: Mapping[str, object]) -> dict[str, object]:
    """Keep only client header keys declared at an entity layout's root."""
    declared = {field.name for field in (*load_layout(_ENTITY_LAYOUTS[entity]).headers,)}
    return {
        key: value
        for key, value in headers.items()
        if key in declared and not key.startswith("otherAttributes.")
    }


def nested_header_values(
    entity: str, headers: Mapping[str, object], container: str
) -> dict[str, object]:
    """Return declared dotted header values for a nested output object."""
    declared = {field.name for field in load_layout(_ENTITY_LAYOUTS[entity]).headers}
    prefix = f"{container}."
    return {
        key.removeprefix(prefix): value
        for key, value in headers.items()
        if key.startswith(prefix) and key in declared
    }


def _profiles() -> dict[str, object]:
    """Read and validate the package-level client profile document."""
    try:
        resource = files("healthcare_test_data").joinpath("client_profiles.json")
        value = json.loads(resource.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not load client_profiles.json") from error
    if not isinstance(value, dict):
        raise RuntimeError("client_profiles.json must contain a JSON object")
    # Support the original single-profile document while making a second client
    # a simple top-level addition: {"chc": {"headers": ..., "values": ...}}.
    return {"chc": value} if "headers" in value else value


def _section(client: str, section: str, entity: str) -> Mapping[str, object]:
    """Get one validated client/entity mapping from the profile document."""
    profiles = _profiles()
    try:
        profile = profiles[client]
        value = profile[section][entity]  # type: ignore[index]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Unknown or incomplete client profile {client!r}") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Malformed client profile {client!r}")
    return value
