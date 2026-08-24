"""NPPES sample profiles used by the linked provider fixture generator.

The checked-in reference contains one individual (entity type ``1``) and one
organizational (entity type ``2``) record.  Keeping the profiles separate
prevents an organizational record from accidentally using an individual
template, or vice versa, while retaining the single public NPPES output file.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

INDIVIDUAL = "individual"
ORGANIZATIONAL = "organizational"


def profile_name(entity_type_code: object) -> str:
    """Return the profile name for an NPPES entity type code."""
    return INDIVIDUAL if str(entity_type_code) == "1" else ORGANIZATIONAL


def split_templates(templates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Partition sample records without merging fields between record types."""
    profiles: dict[str, list[dict[str, Any]]] = {INDIVIDUAL: [], ORGANIZATIONAL: []}
    for template in templates:
        profiles[profile_name(template.get("ENTITY_TYPE_CODE"))].append(deepcopy(template))
    if not profiles[INDIVIDUAL] or not profiles[ORGANIZATIONAL]:
        raise ValueError("NPPES sample must contain both entity type 1 and entity type 2 records")
    return profiles


def template_for(
    templates_by_profile: Mapping[str, list[dict[str, Any]]], entity_type_code: object, index: int
) -> dict[str, Any]:
    """Select a type-specific sample template deterministically."""
    profile = profile_name(entity_type_code)
    templates = templates_by_profile.get(profile, [])
    if not templates:
        raise ValueError(f"NPPES sample has no {profile} template")
    return deepcopy(templates[index % len(templates)])
