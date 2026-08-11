"""Load checked-in client header profiles for generated source records.

Client profiles keep tenant-specific envelope values out of entity generators.
Adding a client is intentionally data-only: add a JSON profile that supplies
header overrides for each supported output stream.
"""

import json
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping

from healthcare_test_data.layouts import load_layout

_ENTITY_PROFILES = {
    "provider": "provider",
    "member": "member",
    "claim_professional": "claim-professional",
    "claim_institutional": "claim-institutional",
}


def available_clients() -> frozenset[str]:
    """Return every checked-in client profile identifier.

    Returns:
        Immutable identifiers that may be selected by ``generator.config.json``.
    """
    return frozenset(
        resource.name.removesuffix(".json")
        for resource in files(__package__).iterdir()
        if resource.name.endswith(".json")
    )


def load_client_headers(client: str, entity: str) -> Mapping[str, object]:
    """Load immutable envelope-header values for one client/entity stream.

    Args:
        client: Checked-in client profile identifier.
        entity: Internal stream name such as ``provider`` or
            ``claim_professional``.

    Returns:
        Immutable client-specific values that entity generators merge into
        their deterministic, record-specific envelope values.

    Raises:
        ValueError: If the profile or its requested entity stream is unknown.
    """
    if client not in available_clients():
        raise ValueError(f"Unknown client profile {client!r}")
    resource = files(__package__).joinpath(f"{client}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        headers = raw["headers"][entity]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Malformed client profile {client!r}") from error
    if not isinstance(headers, dict) or not all(isinstance(key, str) for key in headers):
        raise ValueError(f"Malformed client profile {client!r}")
    return MappingProxyType(dict(headers))


def load_client_values(client: str, entity: str) -> Mapping[str, object]:
    """Load immutable non-header generation values for one client stream.

    Values configure client-owned body data such as network identifiers. They
    are kept separately from emitted envelope headers so they cannot appear as
    accidental JSON root fields.

    Args:
        client: Checked-in client profile identifier.
        entity: Internal output stream name.

    Returns:
        Immutable client-specific generation values for the selected stream.

    Raises:
        ValueError: If the profile or requested entity section is malformed.
    """
    if client not in available_clients():
        raise ValueError(f"Unknown client profile {client!r}")
    resource = files(__package__).joinpath(f"{client}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        values = raw["values"][entity]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Malformed client profile {client!r}") from error
    if not isinstance(values, dict) or not all(isinstance(key, str) for key in values):
        raise ValueError(f"Malformed client profile {client!r}")
    return MappingProxyType(dict(values))


def resolve_client_headers(
    entity: str, supplied_headers: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Return supplied headers without selecting an implicit client profile.

    Engine calls provide the profile chosen in the public configuration. Direct
    library callers receive neutral values unless they explicitly supply a
    mapping, so this helper cannot silently bind a record to CHC.

    Args:
        entity: Internal output stream name, retained for the common API.
        supplied_headers: Optional resolved header values from ``RunConfig``.

    Returns:
        Mutable merged header values ready to add to a generated record.
    """
    del entity
    return dict(supplied_headers or {})


def record_header_values(entity: str, headers: Mapping[str, object]) -> dict[str, object]:
    """Return only values that belong at the generated record's root level.

    Client profiles may also carry values for nested envelope objects. Those
    dotted path entries are consumed by their owning builder and must not be
    serialized as literal root keys.

    Args:
        entity: Internal output stream name.
        headers: Resolved client profile values for one output stream.

    Returns:
        Header values safe to merge into the generated record root.
    """
    declared, containers = _declared_header_paths(entity)
    return {
        key: value
        for key, value in headers.items()
        if key in declared and key.split(".", 1)[0] not in containers
    }


def nested_header_values(
    entity: str, headers: Mapping[str, object], container: str
) -> dict[str, object]:
    """Return declared values for one nested header object.

    Args:
        entity: Internal output stream name.
        headers: Resolved client profile header values.
        container: Declared root object that owns dotted profile paths.

    Returns:
        Relative nested keys and values that are declared by the layout.
    """
    declared, containers = _declared_header_paths(entity)
    prefix = f"{container}."
    if container not in containers:
        return {}
    return {
        key.removeprefix(prefix): value
        for key, value in headers.items()
        if key.startswith(prefix) and key in declared
    }


def _declared_header_paths(entity: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return declared header paths and nested-object container names.

    Args:
        entity: Internal output stream name.

    Returns:
        Allowed full header paths and root names that own nested paths.

    Raises:
        ValueError: If an unsupported internal entity is requested.
    """
    try:
        layout = load_layout(_ENTITY_PROFILES[entity])
    except KeyError as error:
        raise ValueError(f"Unknown client entity {entity!r}") from error
    declared = frozenset(field.name for field in (*layout.headers, *layout.root))
    containers = frozenset(
        name.split(".", 1)[0]
        for name in declared
        if "." in name and name.split(".", 1)[0] in declared
    )
    return declared, containers
