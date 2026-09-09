"""Normalized matching and survivorship rules for update generation."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from test_data_generator.core.errors import ConfigurationError
from test_data_generator.layouts import load_layout


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
    priority: int = 100
    mandatory_fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    field_elasticity: tuple[tuple[str, str], ...] = ()
    higher_priority_methods: tuple[str, ...] = ()

    def elasticity_for(self, field: str, default: str) -> str:
        """Return a method-specific elasticity override when configured."""
        return dict(self.field_elasticity).get(field, default)


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
    """Load a legacy catalog or a manifest that composes domain catalogs."""
    raw = _load_catalog_document(path)
    if "domains" in raw:
        raw = _load_domain_catalogs(path, raw)
    return _parse_rule_catalog(raw)


def _load_catalog_document(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Could not read update rule catalog {path}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("Update rule catalog must be an object")
    return raw


def _load_domain_catalogs(manifest_path: Path, manifest: dict[str, object]) -> dict[str, object]:
    """Compose isolated domain catalogs without duplicating shared Claim rules."""
    domain_paths = _strings(manifest.get("domains"), "rule catalog manifest.domains")
    if not domain_paths:
        raise ConfigurationError("Rule catalog manifest needs at least one domain catalog")
    entities: dict[str, object] = {}
    aliases: dict[str, str] = {}
    catalog_version = str(manifest.get("catalog_version", "unknown"))
    for relative_path in domain_paths:
        domain_path = manifest_path.parent / relative_path
        raw = _load_catalog_document(domain_path)
        if str(raw.get("catalog_version", catalog_version)) != catalog_version:
            raise ConfigurationError(
                f"Domain catalog {domain_path} has a different catalog_version"
            )
        raw_entities = raw.get("entities", {})
        if not isinstance(raw_entities, dict):
            raise ConfigurationError(
                f"Domain catalog {domain_path} must contain an entities object"
            )
        for entity, definition in raw_entities.items():
            if entity in entities or entity in aliases:
                raise ConfigurationError(
                    f"Entity {entity!r} appears in more than one domain catalog"
                )
            entities[entity] = definition
        raw_aliases = raw.get("aliases", {})
        if not isinstance(raw_aliases, dict) or not all(
            isinstance(alias, str) and isinstance(target, str)
            for alias, target in raw_aliases.items()
        ):
            raise ConfigurationError(f"Domain catalog {domain_path} has invalid aliases")
        for alias, target in raw_aliases.items():
            if alias in entities or alias in aliases:
                raise ConfigurationError(f"Entity alias {alias!r} appears more than once")
            aliases[alias] = target
    return {
        "catalog_version": catalog_version,
        "entities": entities,
        "aliases": aliases,
    }


def _parse_rule_catalog(raw: dict[str, object]) -> dict[str, EntityRules]:
    """Validate raw entity definitions and resolve their explicit aliases."""
    raw_entities = raw.get("entities")
    if not isinstance(raw_entities, dict):
        raise ConfigurationError("Update rule catalog must contain an entities object")
    result: dict[str, EntityRules] = {}
    for entity, value in raw_entities.items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"Update rules for {entity!r} must be an object")
        fields = _fields(value.get("fields"), entity)
        keys = _strings(value.get("keys"), f"{entity}.keys")
        source_fields = _strings(value.get("source_fields", []), f"{entity}.source_fields")
        if not keys:
            raise ConfigurationError(f"Update rules for {entity!r} need at least one key")
        if any(field not in source_fields for field in fields):
            raise ConfigurationError(
                f"Update rules for {entity!r} contain a field missing from source_fields"
            )
        profile = str(value.get("profile", entity))
        _add_layout_fields(fields, profile)
        methods = _methods(value.get("matching_methods"), fields, entity)
        result[entity] = EntityRules(
            entity=entity,
            profile=profile,
            keys=keys,
            fields=fields,
            methods=methods,
            catalog_version=str(raw.get("catalog_version", "unknown")),
        )
    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ConfigurationError("Update rule catalog aliases must be an object")
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str) or target not in result:
            raise ConfigurationError(f"Update rule catalog alias {alias!r} has an unknown target")
        if alias in result:
            raise ConfigurationError(f"Update rule catalog alias {alias!r} duplicates an entity")
        source = result[target]
        result[alias] = EntityRules(
            entity=alias,
            profile=source.profile,
            keys=source.keys,
            fields=dict(source.fields),
            methods=source.methods,
            catalog_version=source.catalog_version,
        )
    return result


def _add_layout_fields(fields: dict[str, FieldRule], profile: str) -> None:
    """Make every emitted business field eligible for explicit update operations.

    The survivorship catalog remains authoritative for matching keys,
    requiredness, weights, and matching methods.  Layouts are authoritative
    for the complete emitted field surface.  Adding absent layout fields here
    prevents the catalog from becoming a partial allowlist whenever the GDF
    layout grows, while leaving all curated matching semantics intact.
    """
    try:
        layout = load_layout(profile)
    except ValueError as error:
        raise ConfigurationError(
            f"Update rules for profile {profile!r} have no valid layout"
        ) from error
    for field in (*layout.root, *(group for fields in layout.groups.values() for group in fields)):
        if field.name == "otherAttributes" or field.name.startswith("otherAttributes."):
            continue
        fields.setdefault(
            field.name,
            FieldRule(
                name=field.name,
                required=False,
                weight=Decimal("1"),
                survivorship="layout_field",
            ),
        )


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
        mandatory_fields = (
            _strings(definition["mandatory_fields"], f"{entity}.{name}.mandatory_fields")
            if "mandatory_fields" in definition
            else method_fields
        )
        optional_fields = _strings(
            definition.get("optional_fields", []),
            f"{entity}.{name}.optional_fields",
        )
        if set(mandatory_fields).intersection(optional_fields) or set(
            mandatory_fields + optional_fields
        ) != set(method_fields):
            raise ConfigurationError(
                f"Matching method {entity}.{name} must classify every field "
                "as mandatory or optional"
            )
        raw_elasticity = definition.get("field_elasticity", {})
        if not isinstance(raw_elasticity, dict) or any(
            field not in method_fields or not isinstance(value, str)
            for field, value in raw_elasticity.items()
        ):
            raise ConfigurationError(
                f"Matching method {entity}.{name} has invalid field_elasticity"
            )
        higher_priority_methods = _strings(
            definition.get("higher_priority_methods", []),
            f"{entity}.{name}.higher_priority_methods",
        )
        try:
            needed = Decimal(str(definition["needed_weight"]))
            priority = int(definition.get("priority", len(result) + 1))
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ConfigurationError(f"Matching method {entity}.{name} is incomplete") from error
        if priority < 1:
            raise ConfigurationError(f"Matching method {entity}.{name} has an invalid priority")
        result.append(
            MatchingMethod(
                name,
                needed,
                method_fields,
                priority,
                mandatory_fields,
                optional_fields,
                tuple((field, value) for field, value in raw_elasticity.items()),
                higher_priority_methods,
            )
        )
    priorities = [method.priority for method in result]
    if len(priorities) != len(set(priorities)):
        raise ConfigurationError(f"Matching methods for {entity!r} have duplicate priorities")
    names = {method.name for method in result}
    if any(
        method.name in method.higher_priority_methods
        or not set(method.higher_priority_methods).issubset(names)
        for method in result
    ):
        raise ConfigurationError(
            f"Matching methods for {entity!r} reference an unknown priority method"
        )
    return tuple(result)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must be an array of field names")
    return tuple(value)
