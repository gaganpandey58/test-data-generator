"""Normalized matching and survivorship rules for update generation."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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
    field_rules: Mapping[str, "MethodFieldRule"] = dataclass_field(default_factory=dict)
    alternatives: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class MethodFieldRule:
    """Describe one field's behavior inside a specific matching method."""

    required: bool
    elasticity: str
    weight: Decimal


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
    """Load matching rules from a legacy catalog or per-entity directory.

    A directory is the normal configuration-driven form.  It merges the five
    entity documents and resolves a small ``inherits`` declaration so Claims
    History can reuse Claim field and matching definitions without copying a
    second large catalog.  A single legacy catalog remains accepted for
    existing callers and configuration files.
    """
    raw, catalog_version = _catalog_document(path)
    result: dict[str, EntityRules] = {}
    entities = raw.get("entities")
    if not isinstance(entities, dict):
        raise ConfigurationError("Update rule catalog must contain an entities object")
    for entity, value in entities.items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"Update rules for {entity!r} must be an object")
        value = _resolved_entity_definition(entity, value, entities)
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
        _add_layout_fields(fields, profile, _field_defaults(value.get("field_defaults"), entity))
        methods = _methods(value.get("matching_methods"), fields, entity)
        result[entity] = EntityRules(
            entity=entity,
            profile=profile,
            keys=keys,
            fields=fields,
            methods=methods,
            catalog_version=catalog_version,
        )
    return result


def _catalog_document(path: Path) -> tuple[dict[str, object], str]:
    """Read one legacy catalog or combine all checked-in entity catalogs."""
    if path.is_dir():
        documents: list[dict[str, object]] = []
        for document_path in sorted(path.glob("*.json")):
            try:
                document = json.loads(document_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ConfigurationError(
                    f"Could not read update entity catalog {document_path}"
                ) from error
            if not isinstance(document, dict) or not isinstance(document.get("entities"), dict):
                raise ConfigurationError(
                    f"Update entity catalog {document_path.name} must contain an entities object"
                )
            documents.append(document)
        if not documents:
            raise ConfigurationError(f"Update rule catalog directory {path} is empty")
        merged: dict[str, object] = {"entities": {}}
        merged_entities = merged["entities"]
        assert isinstance(merged_entities, dict)
        versions: set[str] = set()
        for document in documents:
            versions.add(str(document.get("catalog_version", "unknown")))
            raw_entities = document["entities"]
            assert isinstance(raw_entities, dict)
            for name, definition in raw_entities.items():
                if name in merged_entities:
                    raise ConfigurationError(f"Update entity {name!r} is defined more than once")
                merged_entities[name] = definition
        return merged, ",".join(sorted(versions))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Could not read update rule catalog {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("entities"), dict):
        raise ConfigurationError("Update rule catalog must contain an entities object")
    return raw, str(raw.get("catalog_version", "unknown"))


def _resolved_entity_definition(
    entity: str, value: dict[str, object], entities: object
) -> dict[str, object]:
    """Resolve a one-level-or-more rule inheritance chain safely."""
    inherited = value.get("inherits")
    if inherited is None:
        return value
    if not isinstance(inherited, str) or not isinstance(entities, dict):
        raise ConfigurationError(f"Update rules for {entity!r} have an invalid inherits value")
    seen = {entity}
    parent_name = inherited
    merged: dict[str, object] = {}
    while parent_name:
        if parent_name in seen:
            raise ConfigurationError(f"Update rules for {entity!r} have cyclic inheritance")
        seen.add(parent_name)
        parent = entities.get(parent_name)
        if not isinstance(parent, dict):
            raise ConfigurationError(
                f"Update rules for {entity!r} inherit unknown entity {parent_name!r}"
            )
        merged = {**parent, **merged}
        next_parent = parent.get("inherits")
        if next_parent is None:
            break
        if not isinstance(next_parent, str):
            raise ConfigurationError(
                f"Update rules for {parent_name!r} have an invalid inherits value"
            )
        parent_name = next_parent
    return {**merged, **value, "inherits": inherited}


def _add_layout_fields(fields: dict[str, FieldRule], profile: str, defaults: FieldRule) -> None:
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
                required=defaults.required,
                weight=defaults.weight,
                elasticity=defaults.elasticity,
                survivorship=defaults.survivorship,
            ),
        )


def _field_defaults(value: object, entity: str) -> FieldRule:
    """Load the fallback policy for layout fields not explicitly classified."""
    if value is None:
        # Legacy combined catalogs predate configuration-owned field defaults.
        return FieldRule(
            name="*",
            required=False,
            weight=Decimal("1"),
            elasticity="0",
            survivorship="layout_field",
        )
    if not isinstance(value, dict):
        raise ConfigurationError(f"Update rules for {entity!r} need field_defaults")
    requirement = str(value.get("requirement", "")).upper()
    if requirement not in {"MANDATORY", "OPTIONAL"}:
        raise ConfigurationError(
            f"Update rules for {entity!r} have an invalid default field requirement"
        )
    try:
        weight = Decimal(str(value["weight"]))
    except (KeyError, ValueError, ArithmeticError) as error:
        raise ConfigurationError(
            f"Update rules for {entity!r} have an invalid default field weight"
        ) from error
    if weight < 0:
        raise ConfigurationError(
            f"Update rules for {entity!r} have a negative default field weight"
        )
    return FieldRule(
        name="*",
        required=requirement == "MANDATORY",
        weight=weight,
        elasticity=str(value.get("elasticity", "0")),
        survivorship=str(value.get("survivorship", "layout_field")),
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
        try:
            needed = Decimal(str(definition["needed_weight"]))
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ConfigurationError(f"Matching method {entity}.{name} is incomplete") from error
        method_rules = _method_field_rules(
            definition.get("field_rules", {}),
            definition.get("field_defaults", {}),
            method_fields,
            fields,
            entity,
            name,
        )
        alternatives = _method_alternatives(
            definition.get("alternatives", []), method_fields, entity, name
        )
        result.append(MatchingMethod(name, needed, method_fields, method_rules, alternatives))
    return tuple(result)


def _method_field_rules(
    value: object,
    default_value: object,
    method_fields: tuple[str, ...],
    fields: Mapping[str, FieldRule],
    entity: str,
    method: str,
) -> Mapping[str, MethodFieldRule]:
    """Normalize method-specific requirement, elasticity, and weight rules."""
    if not isinstance(value, dict):
        raise ConfigurationError(f"Matching method {entity}.{method}.field_rules must be an object")
    if not isinstance(default_value, dict):
        raise ConfigurationError(
            f"Matching method {entity}.{method}.field_defaults must be an object"
        )
    unknown = set(value).difference(method_fields)
    if unknown:
        field = sorted(unknown)[0]
        raise ConfigurationError(
            f"Matching method {entity}.{method} has rules for unknown field {field!r}"
        )
    result: dict[str, MethodFieldRule] = {}
    for field in method_fields:
        field_override = value.get(field, {})
        if not isinstance(field_override, dict):
            raise ConfigurationError(
                f"Matching method rule {entity}.{method}.{field} must be an object"
            )
        override = {**default_value, **field_override}
        requirement = str(override.get("requirement", "")).upper()
        if requirement not in {"", "MANDATORY", "OPTIONAL"}:
            raise ConfigurationError(
                f"Matching method rule {entity}.{method}.{field} has invalid requirement"
            )
        base = fields[field]
        try:
            weight = Decimal(str(override.get("weight", base.weight)))
        except (ValueError, ArithmeticError) as error:
            raise ConfigurationError(
                f"Matching method rule {entity}.{method}.{field} has invalid weight"
            ) from error
        if weight < 0:
            raise ConfigurationError(
                f"Matching method rule {entity}.{method}.{field} has negative weight"
            )
        result[field] = MethodFieldRule(
            required=(requirement == "MANDATORY" if requirement else base.is_required_for(method)),
            elasticity=str(override.get("elasticity", base.elasticity)),
            weight=weight,
        )
    return result


def _method_alternatives(
    value: object, method_fields: tuple[str, ...], entity: str, method: str
) -> tuple[tuple[str, ...], ...]:
    """Normalize alternative field groups such as first-name OR organization-name."""
    if not isinstance(value, list):
        raise ConfigurationError(f"Matching method {entity}.{method}.alternatives must be an array")
    result: list[tuple[str, ...]] = []
    for index, group in enumerate(value):
        alternatives = _strings(group, f"{entity}.{method}.alternatives[{index}]")
        if not alternatives or any(field not in method_fields for field in alternatives):
            raise ConfigurationError(
                f"Matching method {entity}.{method} has an invalid alternative field group"
            )
        result.append(alternatives)
    return tuple(result)


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label} must be an array of field names")
    return tuple(value)
