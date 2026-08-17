"""Normalized matching and survivorship rules for update generation."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from test_data_generator.core.errors import ConfigurationError


@dataclass(frozen=True)
class FieldRule:
    """Describe one update-eligible field."""

    name: str
    required: bool
    weight: Decimal
    elasticity: str = "0"
    survivorship: str = ""
    required_in: tuple[str, ...] = ()
    optional_in: tuple[str, ...] = ()

    def is_required_for(self, context: str) -> bool:
        """Resolve requiredness for one matching or update context."""
        if context in self.required_in:
            return True
        if context in self.optional_in:
            return False
        return self.required


@dataclass(frozen=True)
class MatchingMethod:
    """Describe one matching method and its needed accumulated weight."""

    name: str
    needed_weight: Decimal
    fields: tuple[str, ...]


@dataclass(frozen=True)
class EntityRules:
    """Normalized rules for one emitted entity profile."""

    entity: str
    profile: str
    keys: tuple[str, ...]
    fields: dict[str, FieldRule]
    methods: tuple[MatchingMethod, ...]
    catalog_version: str = "unknown"


def load_rule_catalog(path: Path) -> dict[str, EntityRules]:
    """Load and validate the normalized catalog generated from the source DOCX."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Could not read update rule catalog {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("entities"), dict):
        raise ConfigurationError("Update rule catalog must contain an entities object")
    result: dict[str, EntityRules] = {}
    for entity, value in raw["entities"].items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"Update rules for {entity!r} must be an object")
        fields = _fields(value.get("fields"), entity)
        keys = _strings(value.get("keys"), f"{entity}.keys")
        source_fields = _strings(value.get("source_fields", []), f"{entity}.source_fields")
        methods = _methods(value.get("matching_methods"), fields, entity)
        if not keys:
            raise ConfigurationError(f"Update rules for {entity!r} need at least one key")
        if any(field not in source_fields for field in fields):
            raise ConfigurationError(
                f"Update rules for {entity!r} contain a field missing from source_fields"
            )
        result[entity] = EntityRules(
            entity=entity,
            profile=str(value.get("profile", entity)),
            keys=keys,
            fields=fields,
            methods=methods,
            catalog_version=str(raw.get("catalog_version", "unknown")),
        )
    return result


def _fields(value: object, entity: str) -> dict[str, FieldRule]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Update rules for {entity!r} need a fields object")
    result: dict[str, FieldRule] = {}
    for name, definition in value.items():
        if not isinstance(definition, dict):
            raise ConfigurationError(f"Field rule {entity}.{name} must be an object")
        try:
            weight = Decimal(str(definition["weight"]))
            required = bool(definition["required"])
            required_in = _strings(
                definition.get("required_in", []), f"{entity}.{name}.required_in"
            )
            optional_in = _strings(
                definition.get("optional_in", []), f"{entity}.{name}.optional_in"
            )
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ConfigurationError(f"Field rule {entity}.{name} is incomplete") from error
        if set(required_in).intersection(optional_in):
            raise ConfigurationError(
                f"Field rule {entity}.{name} cannot be required and optional in the same context"
            )
        if weight < 0:
            raise ConfigurationError(f"Field rule {entity}.{name} has a negative weight")
        result[name] = FieldRule(
            name=name,
            required=required,
            weight=weight,
            elasticity=str(definition.get("elasticity", "0")),
            survivorship=str(definition.get("survivorship", "")),
            required_in=required_in,
            optional_in=optional_in,
        )
    return result


def _methods(
    value: object, fields: dict[str, FieldRule], entity: str
) -> tuple[MatchingMethod, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"Update rules for {entity!r} need matching_methods")
    result: list[MatchingMethod] = []
    for definition in value:
        if not isinstance(definition, dict):
            raise ConfigurationError(f"Matching method for {entity!r} must be an object")
        name = str(definition.get("name", ""))
        method_fields = _strings(definition.get("fields"), f"{entity}.{name}.fields")
        if any(field not in fields for field in method_fields):
            raise ConfigurationError(f"Matching method {entity}.{name} references an unknown field")
        try:
            needed = Decimal(str(definition["needed_weight"]))
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ConfigurationError(f"Matching method {entity}.{name} is incomplete") from error
        result.append(MatchingMethod(name, needed, method_fields))
    return tuple(result)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must be an array of field names")
    return tuple(value)
