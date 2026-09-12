"""Load, normalize, and validate the public generator configuration.

The external JSON config remains deliberately short: callers choose a client,
entity record counts, a seed, and an output directory. This
module expands those choices into immutable internal entity definitions with
hardcoded schema, module, profile, and filename defaults, then validates paths
and relationships before generation can begin.
"""

import json
import secrets
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from random import Random
from typing import Any, Mapping, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from test_data_generator.configuration.profiles import (
    available_clients,
    load_client_headers,
    load_client_values,
)
from test_data_generator.core.dates import current_ingestion_date
from test_data_generator.core.errors import ConfigurationError
from test_data_generator.layouts import available_profiles, load_layout

MAX_RECORD_COUNT = 1_000_000


@dataclass(frozen=True)
class EntityConfig:
    """Describe one fully resolved enabled-entity generation request.

    Attributes:
        name: Internal entity identifier, such as ``member`` or
            ``claim_professional``. Claim and Claims History stream names are
            derived from the public ``claims.professional`` and
            ``claims.institutional`` keys.
        count: Exact number of rows the entity must emit.
        client_headers: Immutable client-specific envelope values for this
            output stream.
        client_values: Immutable client-specific non-header generation values.
        profile: Source-layout profile applied to generated records.
        schema: Absolute JSON Schema path used to validate each output row.
        module: Dotted module path exposing the entity record generator.
        filename: Safe JSONL filename relative to the output directory.
    """

    name: str
    count: int
    client_headers: Mapping[str, object]
    client_values: Mapping[str, object]
    profile: str
    schema: Path
    module: str
    filename: str
    update: Mapping[str, object]
    header_order: str = "source"
    source_claims: Path | None = None
    scenarios: Mapping[str, int] = field(default_factory=dict)
    claim_lifecycles: tuple[tuple[str, int | None], ...] = ()
    ingestion_date: str = field(default_factory=current_ingestion_date)
    update_ingestion_date: str = field(default_factory=current_ingestion_date)
    source_entity: str | None = None
    file_type: str | None = None
    linked_to_claim: bool = False


@dataclass(frozen=True)
class IngestionDateConfig:
    """Resolve creation and update ingestion dates for configured entity groups."""

    existing_date: str
    update_relationship: str
    overrides: Mapping[str, "IngestionDateOverride"]


@dataclass(frozen=True)
class IngestionDateOverride:
    """Optional per-group creation date and incoming-date relationship."""

    existing_date: str | None = None
    update_relationship: str | None = None


@dataclass(frozen=True)
class MatchFixtureCodeConfig:
    """Describe one rule-backed match-fixture matrix.

    New configurations use the rule-catalog method id as the ``match_codes``
    key.  ``name`` remains separate only so the legacy label plus
    ``matching_method`` spelling can be read during migration.
    """

    name: str
    matching_method: str
    operation_counts: Mapping[str, int]
    deterministic_cases: tuple[Mapping[str, object], ...] = ()
    collision_count: int = 0
    collision_method: str | None = None
    legacy_per_record: bool = False


@dataclass(frozen=True)
class MatchVariationConfig:
    """Configure safe, automatic non-matching-field variation per fixture."""

    requested_count: int = 0
    protected_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchFixtureEntityConfig:
    """Describe fixture matrices emitted from records of one enabled entity."""

    entity: str
    match_codes: tuple[MatchFixtureCodeConfig, ...]
    variation: MatchVariationConfig = field(default_factory=MatchVariationConfig)


@dataclass(frozen=True)
class RunConfig:
    """Describe the resolved settings needed for one complete generator run.

    Attributes:
        client: Selected checked-in client header profile.
        seed: Per-run seed shared by every enabled entity generator. An explicit
            public seed makes a run reproducible; an omitted seed receives fresh
            entropy so independent runs vary naturally.
        output_directory: Absolute directory used for generated JSONL files.
        entities: Ordered, enabled entity requests ready for the engine.
        disabled_filenames: Known generated filenames to remove after success.
    """

    client: str
    seed: int
    output_directory: Path
    entities: tuple[EntityConfig, ...]
    disabled_filenames: tuple[str, ...]
    creation_directory: Path
    update_directory: Path
    rule_catalog: Path | None
    updates_enabled: bool
    update_defaults: Mapping[str, object]
    creation_enabled: bool = True
    invalid_values_catalog: Path | None = None
    nppes_count: int = 0
    nppes_individual_count: int = 0
    nppes_organizational_count: int = 0
    nppes_filename: str = "provider_nppes.jsonl"
    provider_linked: bool = False
    match_fixture_entities: tuple[MatchFixtureEntityConfig, ...] = ()


@dataclass(frozen=True)
class ExecutionConfig:
    """Describe a run request without embedding generation business rules."""

    config_path: Path
    entities: tuple[str, ...] = ()
    operations: tuple[str, ...] = ("creation", "updates")


_EXECUTION_ENTITY_GROUPS = {
    "provider": frozenset({"provider", "provider_nppes"}),
    "member": frozenset({"member", "member_mr"}),
    "claims": frozenset(
        {
            "claim_professional",
            "claim_history_professional",
            "claim_institutional",
            "claim_history_institutional",
        }
    ),
    "payments": frozenset({"payment_professional", "payment_institutional"}),
}
_EXECUTION_OPERATIONS = frozenset({"creation", "updates"})


def load_execution_config(path: Path) -> ExecutionConfig:
    r"""Load a focused ``runconfig.json`` execution request.

    The checked-in run configuration is both the execution request and the
    global generator configuration.  The former ``{\"config\": ...}`` wrapper
    is still accepted for a migration window, but no longer required.
    """
    request_path = path.resolve()
    raw = _load_json(request_path, "execution configuration")
    if "config" in raw:
        _validate_execution_schema(raw)
        config_value = raw["config"]
        assert isinstance(config_value, str)
        config_path = _resolve_path(config_value, request_path.parent)
        if config_path == request_path:
            raise ConfigurationError("Execution configuration cannot reference itself")
        return ExecutionConfig(
            config_path,
            tuple(raw.get("entities", ())),
            tuple(raw.get("operations", ("creation", "updates"))),
        )
    entities = raw.get("entities", [])
    operations = raw.get("operations", ["creation", "updates"])
    if not isinstance(entities, list) or not all(isinstance(value, str) for value in entities):
        raise ConfigurationError("runconfig entities must be an array of entity groups")
    if not isinstance(operations, list) or not all(isinstance(value, str) for value in operations):
        raise ConfigurationError("runconfig operations must be an array of phases")
    return ExecutionConfig(request_path, tuple(entities), tuple(operations))


def select_execution_entities(run_config: RunConfig, execution: ExecutionConfig) -> RunConfig:
    """Restrict one resolved run to requested high-level entity groups."""
    if not execution.entities:
        return run_config
    unknown = sorted(set(execution.entities).difference(_EXECUTION_ENTITY_GROUPS))
    if unknown:
        raise ConfigurationError(f"Unknown execution entity group {unknown[0]!r}")
    selected_names = set().union(*(_EXECUTION_ENTITY_GROUPS[name] for name in execution.entities))
    selected_fixtures = tuple(
        fixture for fixture in run_config.match_fixture_entities if fixture.entity in selected_names
    )
    return replace(
        run_config,
        entities=tuple(entity for entity in run_config.entities if entity.name in selected_names),
        # A selectively scoped run must never clean outputs owned by omitted domains.
        disabled_filenames=(),
        match_fixture_entities=selected_fixtures,
    )


def resolve_execution_mode(mode: str, execution: ExecutionConfig) -> str:
    """Apply runconfig phase restrictions to the requested CLI mode."""
    allowed = set(execution.operations)
    unknown = sorted(allowed.difference(_EXECUTION_OPERATIONS))
    if unknown:
        raise ConfigurationError(f"Unknown execution operation {unknown[0]!r}")
    if not allowed:
        raise ConfigurationError("Execution configuration must select an operation")
    if mode == "all":
        return "all" if allowed == _EXECUTION_OPERATIONS else next(iter(allowed))
    if mode not in allowed:
        raise ConfigurationError(f"Execution configuration does not permit requested mode {mode!r}")
    return mode


def load_config(path: Path) -> RunConfig:
    """Load one simple generation configuration from disk.

    Args:
        path: Path to the root JSON configuration.

    Returns:
        Resolved shared settings and enabled entity definitions. Paths are
        made absolute so later generation is independent of the working
        directory.

    Raises:
        ConfigurationError: If the configuration or an enabled schema is invalid.
    """
    config_path = path.resolve()
    raw_config = _load_json(config_path, "configuration")
    if "config" in raw_config:
        execution = load_execution_config(config_path)
        return select_execution_entities(load_config(execution.config_path), execution)
    _validate_schema(raw_config)
    if "entity_configs" in raw_config:
        raw_config = _compose_modular_config(raw_config, config_path)
        _validate_schema(raw_config)
    _normalize_entity_scenarios(raw_config)
    provider_linked = isinstance(raw_config.get("provider"), dict) and (
        isinstance(raw_config["provider"].get("nppes"), dict)
        or isinstance(raw_config["provider"].get("cdf"), dict)
    )
    raw_config = _normalize_config(raw_config)
    try:
        client = raw_config["client"]
        seed = raw_config["seed"]
        output_directory = _resolve_path(raw_config["output_directory"], config_path.parent)
        generation = raw_config["generation"]
        raw_entities = raw_config["entities"]
    except KeyError as error:
        raise ConfigurationError(f"Invalid configuration: missing {error.args[0]!r}") from error
    if client not in available_clients():
        raise ConfigurationError(f"Invalid configuration: unknown client profile {client!r}")

    generation_config = generation if isinstance(generation, dict) else {}
    ingestion_dates = _ingestion_date_config(generation_config)
    creation_config = generation_config.get("creation", {})
    creation_directory = _output_subdirectory(
        output_directory,
        creation_config.get("directory", "new-test-data"),
        "creation",
    )

    disabled_filenames: list[str] = []
    # Remove filenames emitted before the provider CDF naming contract changed.
    disabled_filenames.extend(
        (
            "providers.jsonl",
            "provider.jsonl",
            "provider_cdf_updated.jsonl",
            "professional-claims.jsonl",
            "institutional-claims.jsonl",
        )
    )
    nppes_config = raw_config.get("provider_nppes", {})
    nppes_count = _nppes_total(nppes_config)
    if isinstance(nppes_config, dict) and (
        "individual" in nppes_config or "organizational" in nppes_config
    ):
        nppes_individual_count = int(nppes_config.get("individual", 0))
        nppes_organizational_count = int(nppes_config.get("organizational", 0))
    else:
        nppes_individual_count = (nppes_count + 1) // 2
        nppes_organizational_count = nppes_count // 2
    if nppes_count == 0:
        disabled_filenames.append("provider_nppes.jsonl")

    entities: list[EntityConfig] = []
    for name, raw_entity in raw_entities.items():
        if not raw_entity["enabled"]:
            _validate_filename(name, raw_entity["filename"], output_directory)
            disabled_filenames.append(raw_entity["filename"])
            continue
        profile = raw_entity["profile"]
        _validate_profile(name, profile)
        schema = Path(str(raw_entity["schema"]))
        if not schema.is_file():
            raise ConfigurationError(
                f"Enabled entity {name!r} references missing schema file {schema}"
            )
        filename = raw_entity["filename"]
        _validate_filename(name, filename, output_directory)
        record_count = _effective_record_count(name, raw_entity, raw_entities)
        if record_count == 0:
            # A fixture-only stream stays internally enabled so its generator
            # and rule catalog are available, but it owns no creation file.
            disabled_filenames.append(filename)
        source_entity = (
            str(raw_entity["source_entity"])
            if isinstance(raw_entity.get("source_entity"), str)
            else None
        )
        profile_entity = source_entity or name
        entities.append(
            EntityConfig(
                name=name,
                count=record_count,
                client_headers=load_client_headers(client, profile_entity),
                client_values=load_client_values(client, profile_entity),
                profile=profile,
                schema=schema,
                module=raw_entity["module"],
                filename=filename,
                update=raw_entity.get("updates", {}),
                header_order=str(raw_entity.get("header_order", "source")),
                source_claims=_payment_source_path(
                    name,
                    raw_entity,
                    raw_entities,
                    config_path.parent,
                    creation_directory,
                ),
                scenarios=_scenario_counts(name, raw_entity),
                claim_lifecycles=_claim_lifecycles(
                    name, raw_entity, record_count, seed, raw_entities
                ),
                ingestion_date=_creation_ingestion_date(name, ingestion_dates),
                update_ingestion_date=_update_ingestion_date(name, ingestion_dates),
                source_entity=source_entity,
                file_type=(
                    str(raw_entity["file_type"])
                    if isinstance(raw_entity.get("file_type"), str)
                    else None
                ),
                linked_to_claim=bool(raw_entity.get("linked_to_claim", False)),
            )
        )
    _validate_unique_filenames(entities)
    update_config = generation_config.get("updates", {})
    update_directory = _output_subdirectory(
        output_directory,
        update_config.get("directory", "update-test-data"),
        "updates",
    )
    rule_catalog_value = update_config.get("rule_catalog")
    rule_catalog = (
        _resolve_path(str(rule_catalog_value), config_path.parent)
        if isinstance(rule_catalog_value, str)
        else Path(__file__).with_name("update-rule-catalog.json")
    )
    if not rule_catalog.is_file():
        raise ConfigurationError(f"Update rule catalog does not exist: {rule_catalog}")
    invalid_catalog_value = update_config.get("invalid_values_catalog")
    invalid_values_catalog = (
        _resolve_path(str(invalid_catalog_value), config_path.parent)
        if isinstance(invalid_catalog_value, str)
        else Path(__file__).with_name("invalid-values.json")
    )
    if not invalid_values_catalog.is_file():
        raise ConfigurationError(f"Invalid-value catalog does not exist: {invalid_values_catalog}")
    match_fixture_entities = _match_fixture_config(raw_entities)
    return RunConfig(
        client=client,
        seed=seed,
        output_directory=output_directory,
        entities=tuple(entities),
        disabled_filenames=tuple(disabled_filenames),
        creation_directory=creation_directory,
        update_directory=update_directory,
        rule_catalog=rule_catalog,
        updates_enabled=bool(update_config.get("enabled", False)),
        update_defaults={},
        creation_enabled=bool(creation_config.get("enabled", True)),
        invalid_values_catalog=invalid_values_catalog,
        nppes_count=nppes_count,
        nppes_individual_count=nppes_individual_count,
        nppes_organizational_count=nppes_organizational_count,
        provider_linked=provider_linked,
        match_fixture_entities=match_fixture_entities,
    )


_MATCH_FIXTURE_OPERATIONS = frozenset(
    {
        "UPDATE",
        "INVALID",
        "MISSING",
        "EMPTY",
        "DUPLICATE",
        "WEIGHT_BELOW_LIMIT",
        "WEIGHT_AT_LIMIT",
        "WEIGHT_ABOVE_LIMIT",
        "ELASTICITY_INSIDE",
        "ELASTICITY_AT_LIMIT",
        "ELASTICITY_OUTSIDE",
    }
)
_MATCH_FIXTURE_MUTATION_OPERATIONS = frozenset(
    {"UPDATE", "INVALID", "MISSING", "EMPTY", "DUPLICATE"}
)
_MATCH_FIXTURE_WEIGHT_BOUNDARIES = frozenset({"BELOW_LIMIT", "AT_LIMIT", "ABOVE_LIMIT"})
_MATCH_FIXTURE_ELASTICITY_BOUNDARIES = frozenset({"INSIDE", "AT_LIMIT", "OUTSIDE"})


def _match_fixture_config(
    raw_entities: Mapping[str, object],
) -> tuple[MatchFixtureEntityConfig, ...]:
    """Read per-entity match-code cases from the owning entity configuration.

    Match-code cases are update artifacts, so they intentionally share the
    normal updated output directory.  Keeping them on the entity itself means
    a QA author can define ordinary updates, deterministic plans, weighted
    cases, and elasticity cases in one configuration document.
    """
    result: list[MatchFixtureEntityConfig] = []
    for entity_name, source_entity in raw_entities.items():
        if not isinstance(source_entity, Mapping) or not source_entity.get("enabled"):
            continue
        variation = _match_variation_config(
            entity_name,
            source_entity.get("variation"),
            _configured_update_fields(source_entity.get("updates")),
        )
        codes = source_entity.get("match_codes")
        if codes is None:
            if variation.requested_count:
                raise ConfigurationError(f"Entity {entity_name!r}.variation requires match_codes")
            continue
        if not isinstance(codes, Mapping) or not codes:
            raise ConfigurationError(
                f"Entity {entity_name!r}.match_codes must be a non-empty object"
            )
        code_configs: list[MatchFixtureCodeConfig] = []
        for code_name, code_value in codes.items():
            if not isinstance(code_name, str) or not code_name.strip():
                raise ConfigurationError("Each matchCode name must be a non-empty string")
            _validate_fixture_filename(code_name, "matchCode")
            if not isinstance(code_value, Mapping):
                raise ConfigurationError(f"matchCode {code_name!r} must be an object")
            configured_method = code_value.get("matching_method")
            if configured_method is not None and (
                not isinstance(configured_method, str) or not configured_method.strip()
            ):
                raise ConfigurationError(
                    f"matchCode {code_name!r}.matching_method must be a non-empty string"
                )
            method = configured_method.strip() if isinstance(configured_method, str) else code_name
            operation_counts = _match_fixture_operation_counts(code_name, code_value)
            deterministic_cases = _match_fixture_cases(code_name, code_value)
            collision_count, collision_method = _match_fixture_collisions(code_name, code_value)
            if not operation_counts and not deterministic_cases and collision_count == 0:
                raise ConfigurationError(f"matchCode {code_name!r} needs generate or cases")
            code_configs.append(
                MatchFixtureCodeConfig(
                    code_name,
                    method,
                    operation_counts,
                    deterministic_cases,
                    collision_count,
                    collision_method,
                    "matching_method" in code_value or "operation_counts" in code_value,
                )
            )
        result.append(MatchFixtureEntityConfig(entity_name, tuple(code_configs), variation))
    return tuple(result)


def _match_variation_config(
    entity_name: str, value: object, protected_fields: tuple[str, ...]
) -> MatchVariationConfig:
    """Normalize the stream-level automatic variation request."""
    if value is None:
        return MatchVariationConfig(protected_fields=protected_fields)
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"Entity {entity_name!r}.variation must be an object")
    count = value.get("fields_per_record", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ConfigurationError(
            f"Entity {entity_name!r}.variation.fields_per_record must be a non-negative integer"
        )
    return MatchVariationConfig(count, protected_fields)


def _configured_update_fields(value: object) -> tuple[str, ...]:
    """Collect direct stream-operation fields that variation must not reuse."""
    if not isinstance(value, Mapping):
        return ()
    fields: list[str] = []
    direct = value.get("fields")
    if isinstance(direct, list):
        fields.extend(str(field).strip() for field in direct)
    operation = value.get("operation")
    if isinstance(operation, Mapping) and isinstance(operation.get("fields"), list):
        fields.extend(str(field).strip() for field in operation["fields"])
    modifications = value.get("modifications")
    if isinstance(modifications, list):
        for modification in modifications:
            if isinstance(modification, Mapping) and isinstance(modification.get("fields"), list):
                fields.extend(str(field).strip() for field in modification["fields"])
    return tuple(dict.fromkeys(field for field in fields if field))


def _match_fixture_operation_counts(
    code_name: str, configured: Mapping[str, object]
) -> Mapping[str, int]:
    """Normalize legacy counts and the unified ``generate`` categories."""
    if "operation_counts" in configured:
        value = configured.get("operation_counts", {})
        if not isinstance(value, Mapping):
            raise ConfigurationError(f"matchCode {code_name!r}.operation_counts must be an object")
        return _validated_match_fixture_counts(code_name, value)

    generate = configured.get("generate", {})
    if not isinstance(generate, Mapping):
        raise ConfigurationError(f"matchCode {code_name!r}.generate must be an object")
    result: dict[str, int] = {}
    operations = generate.get("operations", {})
    if not isinstance(operations, Mapping):
        raise ConfigurationError(f"matchCode {code_name!r}.generate.operations must be an object")
    standard = _validated_match_fixture_counts(code_name, operations)
    unsupported = set(standard).difference(_MATCH_FIXTURE_MUTATION_OPERATIONS)
    if unsupported:
        raise ConfigurationError(
            f"matchCode {code_name!r}.generate.operations has unsupported operation "
            f"{sorted(unsupported)[0]!r}"
        )
    result.update(standard)
    for operation, count in _boundary_counts(
        code_name,
        "weight",
        generate.get("weight", {}),
        _MATCH_FIXTURE_WEIGHT_BOUNDARIES,
    ).items():
        result[f"WEIGHT_{operation}"] = count
    for operation, count in _boundary_counts(
        code_name,
        "elasticity",
        generate.get("elasticity", {}),
        _MATCH_FIXTURE_ELASTICITY_BOUNDARIES,
    ).items():
        result[f"ELASTICITY_{operation}"] = count
    return result


def _validated_match_fixture_counts(
    code_name: str, value: Mapping[object, object]
) -> dict[str, int]:
    """Validate exact generated-case counts using the shared operation vocabulary."""
    result: dict[str, int] = {}
    for raw_operation, count in value.items():
        operation = _normalize_match_fixture_operation(raw_operation)
        if operation not in _MATCH_FIXTURE_OPERATIONS:
            raise ConfigurationError(
                f"matchCode {code_name!r} has unsupported operation {raw_operation!r}"
            )
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= MAX_RECORD_COUNT
        ):
            raise ConfigurationError(
                f"matchCode {code_name!r} count for {operation!r} must be a non-negative integer"
            )
        result[operation] = result.get(operation, 0) + count
    return result


def _boundary_counts(
    code_name: str,
    category: str,
    value: object,
    supported: frozenset[str],
) -> dict[str, int]:
    """Accept an exact-count object or the one-each array shorthand."""
    if value is None:
        return {}
    if isinstance(value, list):
        configured: Mapping[object, object] = {item: 1 for item in value}
    elif isinstance(value, Mapping):
        configured = value
    else:
        raise ConfigurationError(
            f"matchCode {code_name!r}.generate.{category} must be an object or array"
        )
    result: dict[str, int] = {}
    for raw_boundary, count in configured.items():
        boundary = str(raw_boundary).strip().upper().replace("-", "_")
        if boundary not in supported:
            raise ConfigurationError(
                f"matchCode {code_name!r}.generate.{category} has unsupported boundary "
                f"{raw_boundary!r}"
            )
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= MAX_RECORD_COUNT
        ):
            raise ConfigurationError(
                f"matchCode {code_name!r}.generate.{category}.{boundary} must be a "
                "non-negative integer"
            )
        result[boundary] = count
    return result


def _match_fixture_collisions(
    code_name: str, configured: Mapping[str, object]
) -> tuple[int, str | None]:
    """Read automatic or explicitly targeted collision requests."""
    generate = configured.get("generate", {})
    if not isinstance(generate, Mapping):
        return 0, None
    value = generate.get("collisions", 0)
    against: object | None = None
    if isinstance(value, Mapping):
        count = value.get("count", 0)
        against = value.get("against")
    else:
        count = value
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= MAX_RECORD_COUNT:
        raise ConfigurationError(
            f"matchCode {code_name!r}.generate.collisions count must be non-negative"
        )
    if against is not None and (not isinstance(against, str) or not against.strip()):
        raise ConfigurationError(
            f"matchCode {code_name!r}.generate.collisions.against must be a method id"
        )
    if count == 0 and against is not None:
        raise ConfigurationError(
            f"matchCode {code_name!r}.generate.collisions.against requires a positive count"
        )
    return count, against.strip() if isinstance(against, str) else None


def _match_fixture_cases(
    code_name: str, configured: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    """Validate deterministic multi-field cases while preserving their field plan."""
    value = configured.get("cases", configured.get("deterministic_cases", []))
    if not isinstance(value, list):
        raise ConfigurationError(f"matchCode {code_name!r}.deterministic_cases must be an array")
    result: list[Mapping[str, object]] = []
    for case_index, case in enumerate(value, start=1):
        if not isinstance(case, Mapping):
            raise ConfigurationError(
                f"matchCode {code_name!r} deterministic case {case_index} must be an object"
            )
        case_name = case.get("name")
        if case_name is not None and (not isinstance(case_name, str) or not case_name.strip()):
            raise ConfigurationError(
                f"matchCode {code_name!r} deterministic case {case_index} name must be "
                "a non-empty string"
            )
        count = case.get("count", 1)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= MAX_RECORD_COUNT
        ):
            raise ConfigurationError(
                f"matchCode {code_name!r} deterministic case {case_index} count must be positive"
            )
        modifications = case.get("modifications")
        if not isinstance(modifications, list) or not modifications:
            raise ConfigurationError(
                f"matchCode {code_name!r} deterministic case {case_index} needs modifications"
            )
        for modification in modifications:
            if not isinstance(modification, Mapping):
                raise ConfigurationError(
                    f"matchCode {code_name!r} deterministic case {case_index} "
                    "has an invalid modification"
                )
            operation = _normalize_match_fixture_operation(modification.get("type"))
            if operation not in _MATCH_FIXTURE_MUTATION_OPERATIONS:
                raise ConfigurationError(
                    f"matchCode {code_name!r} deterministic case {case_index} has unsupported "
                    f"mutation {modification.get('type')!r}"
                )
            fields = modification.get("fields")
            if (
                not isinstance(fields, list)
                or not fields
                or not all(isinstance(field, str) and field.strip() for field in fields)
            ):
                raise ConfigurationError(
                    f"matchCode {code_name!r} deterministic case {case_index} mutation "
                    "fields must be a non-empty array of field names"
                )
        expected = case.get("expected_outcome")
        if expected is not None and expected not in {"MATCH", "NO_MATCH"}:
            raise ConfigurationError(
                f"matchCode {code_name!r} deterministic case {case_index} "
                "has invalid expected_outcome"
            )
        result.append(deepcopy(dict(case)))
    return tuple(result)


def _normalize_match_fixture_operation(value: object) -> str:
    """Accept clear snake/kebab aliases while retaining one internal vocabulary."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip().upper().replace("-", "_")
    aliases = {
        "BELOW_LIMIT": "WEIGHT_BELOW_LIMIT",
        "AT_LIMIT": "WEIGHT_AT_LIMIT",
        "ABOVE_LIMIT": "WEIGHT_ABOVE_LIMIT",
        "WEIGHT_CHANGE_BELOW_LIMIT": "WEIGHT_BELOW_LIMIT",
        "WEIGHT_CHANGE_AT_LIMIT": "WEIGHT_AT_LIMIT",
        "WEIGHT_CHANGE_ABOVE_LIMIT": "WEIGHT_ABOVE_LIMIT",
    }
    return aliases.get(normalized, normalized)


def _validate_fixture_filename(value: str, label: str) -> None:
    """Prevent matchCode labels from escaping their entity fixture directory."""
    path = Path(value)
    if path.name != value or path.suffix or value in {".", ".."}:
        raise ConfigurationError(f"{label} {value!r} is not a safe JSON filename")


def _compose_modular_config(global_config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Resolve the run configuration and one scenario document per domain."""
    entity_references = global_config.get("entity_configs")
    if not isinstance(entity_references, Mapping):
        raise ConfigurationError("Modular configuration requires an entity_configs object")
    expected_entities = ("provider", "member", "claims", "payments")
    if set(entity_references) != set(expected_entities):
        raise ConfigurationError(
            "entity_configs must contain provider, member, claims, and payments references"
        )
    composed: dict[str, Any] = {
        key: deepcopy(value) for key, value in global_config.items() if key != "entity_configs"
    }
    for entity_name in expected_entities:
        reference = entity_references[entity_name]
        if not isinstance(reference, str):
            raise ConfigurationError(f"entity_configs.{entity_name} must be a path string")
        entity_path = _resolve_path(reference, config_path.parent)
        document = _load_json(entity_path, f"{entity_name} entity configuration")
        if set(document) != {entity_name} or not isinstance(document[entity_name], dict):
            raise ConfigurationError(f"{entity_path} must contain only a {entity_name!r} object")
        composed[entity_name] = _expand_domain_defaults(entity_name, document[entity_name])
    _normalize_entity_scenarios(composed)
    return composed


def _expand_domain_defaults(entity_name: str, document: Mapping[str, object]) -> dict[str, object]:
    """Apply optional shared Professional/Institutional domain settings once."""
    result = deepcopy(dict(document))
    if entity_name not in {"claims", "payments"}:
        return result
    defaults = result.pop("defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigurationError(f"{entity_name}.defaults must be an object")
    # ``defaults`` remains a compatibility alias.  New configurations put
    # common operations directly under their data-domain section, for example
    # ``claims.operations``.  Resolve both forms before the individual
    # professional/institutional streams are normalized.
    domain_defaults = _merge_configuration(dict(defaults), _pop_domain_scenario_values(result))
    for stream in ("professional", "institutional"):
        configured = result.get(stream)
        if configured is None:
            continue
        if not isinstance(configured, Mapping):
            raise ConfigurationError(f"{entity_name}.{stream} must be an object")
        result[stream] = _merge_configuration(dict(domain_defaults), dict(configured))
    if entity_name == "claims":
        _expand_claim_history_domain(result)
    return result


def _merge_configuration(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Merge nested config values while keeping stream-specific overrides explicit."""
    merged = deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_configuration(dict(current), dict(value))
        else:
            merged[key] = deepcopy(value)
    return merged


_DOMAIN_SCENARIO_ATTRIBUTES = frozenset(
    {
        "operations",
        "modifications",
        "updates",
        "matching_method",
        "threshold",
        "expected_outcome",
        "failure_mode",
        "failure_field",
        "collision_method",
        "elasticity_boundary",
        "include",
        "exclude",
        "linked",
        "variation",
    }
)


def _pop_domain_scenario_values(domain: dict[str, object]) -> dict[str, object]:
    """Remove and return scenario values shared by sibling stream sections."""
    return {key: deepcopy(domain.pop(key)) for key in _DOMAIN_SCENARIO_ATTRIBUTES if key in domain}


def _inherit_domain_scenario_values(domain: dict[str, object], label: str) -> None:
    """Apply direct domain scenarios to Professional and Institutional streams."""
    defaults = _pop_domain_scenario_values(domain)
    if not defaults:
        return
    for stream in ("professional", "institutional"):
        configured = domain.get(stream)
        if configured is None:
            continue
        if not isinstance(configured, Mapping):
            raise ConfigurationError(f"{label}.{stream} must be an object")
        domain[stream] = _merge_configuration(dict(defaults), dict(configured))


def _expand_claim_history_domain(claims: dict[str, object]) -> None:
    """Expand common Claims History settings without coupling them to 837 rules."""
    history = claims.get("claims_history")
    if history is None:
        return
    if not isinstance(history, dict):
        raise ConfigurationError("claims.claims_history must be an object")
    _inherit_domain_scenario_values(history, "claims.claims_history")


_SCENARIO_ATTRIBUTES = frozenset(
    {
        "matching_method",
        "threshold",
        "expected_outcome",
        "failure_mode",
        "failure_field",
        "collision_method",
        "elasticity_boundary",
        "include",
        "exclude",
    }
)


def _normalize_entity_scenarios(config: dict[str, Any]) -> None:
    """Turn direct entity ``operations`` into the existing update request shape.

    The public model deliberately keeps all scenario values beside the entity
    count.  ``updates`` remains accepted as a compatibility alias, while new
    configurations use ``operations`` (or ``modifications``) directly.
    """
    for entity_name in ("provider", "member"):
        entity = config.get(entity_name)
        if not isinstance(entity, dict):
            continue
        _normalize_entity_scenario(entity, entity_name)
        if entity_name == "provider":
            if isinstance(entity.get("nppes"), dict):
                _normalize_entity_scenario(entity["nppes"], f"{entity_name}.nppes")
                nppes_cdf = entity["nppes"].get("cdf")
                if isinstance(nppes_cdf, dict):
                    _normalize_entity_scenario(nppes_cdf, f"{entity_name}.nppes.cdf")
            cdf = entity.get("cdf")
            if isinstance(cdf, dict):
                _normalize_entity_scenario(cdf, f"{entity_name}.cdf")
        roster = entity.get("mr")
        if isinstance(roster, dict):
            _normalize_entity_scenario(roster, f"{entity_name}.mr")
    for domain_name in ("claims", "payments"):
        domain = config.get(domain_name)
        if not isinstance(domain, dict):
            continue
        _inherit_domain_scenario_values(domain, domain_name)
        for stream in ("professional", "institutional"):
            entity = domain.get(stream)
            if isinstance(entity, dict):
                _normalize_entity_scenario(entity, f"{domain_name}.{stream}")
                if domain_name == "claims" and isinstance(entity.get("history"), dict):
                    _normalize_entity_scenario(entity["history"], f"{domain_name}.{stream}.history")
        if domain_name == "claims" and isinstance(domain.get("claims_history"), dict):
            history = domain["claims_history"]
            _inherit_domain_scenario_values(history, "claims.claims_history")
            for stream in ("professional", "institutional"):
                entity = history.get(stream)
                if isinstance(entity, dict):
                    _normalize_entity_scenario(entity, f"claims.claims_history.{stream}")
    nppes = config.get("provider_nppes")
    if isinstance(nppes, dict):
        _normalize_entity_scenario(nppes, "provider_nppes")


def _normalize_entity_scenario(entity: dict[str, object], label: str) -> None:
    """Normalize one direct scenario without requiring named global profiles."""
    if "updates" in entity:
        if not isinstance(entity["updates"], Mapping):
            raise ConfigurationError(f"{label}.updates must be an object")
        return
    operations = entity.get("operations")
    modifications = entity.get("modifications")
    if operations is not None and modifications is not None:
        raise ConfigurationError(f"{label} cannot define both operations and modifications")
    plan = operations if operations is not None else modifications
    if plan is None:
        return
    if not isinstance(plan, list) or not plan:
        raise ConfigurationError(f"{label}.operations must be a non-empty array")
    if not all(isinstance(item, Mapping) for item in plan):
        raise ConfigurationError(f"{label}.operations entries must be objects")
    update: dict[str, object] = {
        key: deepcopy(value) for key, value in entity.items() if key in _SCENARIO_ATTRIBUTES
    }
    update["operation"] = {"type": "DUPLICATE"}
    update["modifications"] = deepcopy(plan)
    entity["updates"] = update


def _normalize_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    """Expand the short root entity form into the detailed internal form.

    The public form keeps a run focused on the only values people usually
    change: client and each selected entity's count. Professional and
    institutional claims are grouped under one ``claims`` object. Source
    profile, schema, module, and output filename are always internal defaults.

    Args:
        raw_config: Schema-valid decoded root configuration.

    Returns:
        A detailed configuration with hardcoded defaults for every known
        entity, including disabled entities that need stale-output cleanup.
    """
    defaults = _entity_defaults()
    entities = {name: dict(default) for name, default in defaults.items()}
    provider_config = raw_config.get("provider")
    nested_nppes = provider_config.get("nppes") if isinstance(provider_config, Mapping) else None
    legacy_nppes = raw_config.get("provider_nppes")
    if isinstance(nested_nppes, Mapping) and isinstance(legacy_nppes, Mapping):
        raise ConfigurationError(
            "Configure NPPES only under provider.nppes; provider_nppes cannot be used "
            "at the same time"
        )
    if isinstance(legacy_nppes, Mapping) and "additional_count" in legacy_nppes:
        raise ConfigurationError(
            "provider_nppes.additional_count is not supported; use "
            "provider.cdf.additional_count for CDF-only records"
        )
    for name in ("provider", "member"):
        value = raw_config.get(name)
        if isinstance(value, dict):
            if name == "provider" and (
                isinstance(value.get("nppes"), dict) or isinstance(value.get("cdf"), dict)
            ):
                nppes = value.get("nppes", {})
                legacy_cdf = value.get("cdf")
                nested_cdf = nppes.get("cdf") if isinstance(nppes, Mapping) else None
                if isinstance(nested_cdf, Mapping) and isinstance(legacy_cdf, Mapping):
                    raise ConfigurationError(
                        "Configure linked CDF settings either under provider.nppes.cdf or "
                        "provider.cdf, not both"
                    )
                nppes_count = _nppes_total(nppes)
                nppes_additional_count = (
                    nppes.get("additional_count", 0) if isinstance(nppes, Mapping) else 0
                )
                nested_cdf_additional_count = (
                    nested_cdf.get("additional_count", 0) if isinstance(nested_cdf, Mapping) else 0
                )
                legacy_cdf_additional_count = (
                    legacy_cdf.get("additional_count", 0) if isinstance(legacy_cdf, Mapping) else 0
                )
                configured_additional_counts = sum(
                    count > 0
                    for count in (
                        nppes_additional_count,
                        nested_cdf_additional_count,
                        legacy_cdf_additional_count,
                    )
                )
                if configured_additional_counts > 1:
                    raise ConfigurationError(
                        "Configure CDF-only records in one location: provider.nppes.cdf, "
                        "provider.nppes.additional_count, or provider.cdf.additional_count"
                    )
                # Either CDF block owns the linked CDF stream.  The sibling
                # form mirrors the public provider/NPPES split, while the
                # nested form remains a supported compatibility spelling.
                additional_count = (
                    nested_cdf_additional_count
                    or nppes_additional_count
                    or legacy_cdf_additional_count
                )
                if isinstance(nested_cdf, Mapping):
                    cdf_selection = {
                        key: item for key, item in nested_cdf.items() if key != "additional_count"
                    }
                    direct_selection = {
                        key: item
                        for key, item in value.items()
                        if key not in {"nppes", "cdf", "count"}
                    }
                    if _has_explicit_update_selection(
                        cdf_selection
                    ) and _has_explicit_update_selection(direct_selection):
                        raise ConfigurationError(
                            "Configure linked CDF operations under provider.nppes.cdf, not provider"
                        )
                    selection = {**direct_selection, **cdf_selection}
                elif isinstance(legacy_cdf, Mapping):
                    cdf_selection = {
                        key: item for key, item in legacy_cdf.items() if key != "additional_count"
                    }
                    direct_selection = {
                        key: item
                        for key, item in value.items()
                        if key not in {"nppes", "cdf", "count"}
                    }
                    if _has_explicit_update_selection(
                        cdf_selection
                    ) and _has_explicit_update_selection(direct_selection):
                        raise ConfigurationError(
                            "Configure linked CDF operations under provider.cdf, not provider"
                        )
                    selection = {**direct_selection, **cdf_selection}
                else:
                    selection = {
                        key: item for key, item in value.items() if key not in {"nppes", "cdf"}
                    }
                selection["count"] = int(nppes_count) + int(additional_count)
                entities[name] = _selected_entity(entities[name], selection)
                nppes_selection = dict(nppes) if isinstance(nppes, Mapping) else {}
                nppes_selection.pop("additional_count", None)
                nppes_selection.pop("cdf", None)
                nppes_selection["count"] = int(nppes_count)
                if _has_explicit_update_selection(nppes_selection):
                    entities["provider_nppes"] = _selected_entity(
                        entities["provider_nppes"], nppes_selection
                    )
                raw_config["provider_nppes"] = nppes_selection
                continue
            selection = {key: item for key, item in value.items() if key != "mr"}
            entities[name] = _selected_entity(entities[name], selection)
            if name == "member" and isinstance(value.get("mr"), dict):
                mr_selection = cast(Mapping[str, object], value["mr"])
                mr_count = mr_selection.get("count", 0)
                member_count = selection.get("count", 0)
                if not isinstance(mr_count, int) or isinstance(mr_count, bool):
                    raise ConfigurationError("Member Roster selection count must be an integer")
                if not isinstance(member_count, int) or isinstance(member_count, bool):
                    raise ConfigurationError("Member selection count must be an integer")
                if mr_count > member_count:
                    raise ConfigurationError(
                        "Member Roster count cannot exceed its source Member count"
                    )
                entities["member_mr"] = _selected_entity(entities["member_mr"], mr_selection)

    claims = raw_config.get("claims")
    if isinstance(claims, dict):
        claims_history = claims.get("claims_history")
        for stream, entity_name, history_entity_name in (
            ("professional", "claim_professional", "claim_history_professional"),
            ("institutional", "claim_institutional", "claim_history_institutional"),
        ):
            value = claims.get(stream)
            if isinstance(value, dict):
                claim_selection = {key: item for key, item in value.items() if key != "history"}
                entities[entity_name] = _selected_entity(entities[entity_name], claim_selection)
                history_value = value.get("history")
                if isinstance(history_value, Mapping):
                    history_selection = dict(history_value)
                    history_selection.setdefault("linked", False)
                else:
                    # Preserve the legacy paired 837/CH behavior when no
                    # explicit history selection is supplied.
                    history_selection = {
                        "count": claim_selection.get("count", 0),
                        "linked": True,
                    }
                entities[history_entity_name] = _selected_entity(
                    entities[history_entity_name], history_selection
                )
        if isinstance(claims_history, Mapping):
            for stream, history_entity_name in (
                ("professional", "claim_history_professional"),
                ("institutional", "claim_history_institutional"),
            ):
                history_value = claims_history.get(stream)
                if not isinstance(history_value, Mapping):
                    continue
                claim_value = claims.get(stream)
                if isinstance(claim_value, Mapping) and isinstance(
                    claim_value.get("history"), Mapping
                ):
                    raise ConfigurationError(
                        f"Claims History {stream!r} may be configured either under "
                        f"claims.{stream}.history or claims.claims_history.{stream}, not both"
                    )
                history_selection = dict(history_value)
                # A separately grouped Claims History stream is independent
                # unless the config expressly requests one-to-one Claim links.
                history_selection.setdefault("linked", False)
                entities[history_entity_name] = _selected_entity(
                    entities[history_entity_name], history_selection
                )

    if isinstance(legacy_nppes, Mapping):
        nppes_selection = dict(legacy_nppes)
        nppes_selection["count"] = _nppes_total(nppes_selection)
        if _has_explicit_update_selection(nppes_selection):
            entities["provider_nppes"] = _selected_entity(
                entities["provider_nppes"], nppes_selection
            )

    payments = raw_config.get("payments")
    if isinstance(payments, dict):
        for stream, entity_name in (
            ("professional", "payment_professional"),
            ("institutional", "payment_institutional"),
        ):
            value = payments.get(stream)
            if isinstance(value, dict):
                entities[entity_name] = _selected_entity(entities[entity_name], value)
    generation = raw_config.get("generation")
    output_order = generation.get("output_order") if isinstance(generation, dict) else None
    global_header_order = (
        output_order.get("headers", "source") if isinstance(output_order, dict) else "source"
    )
    for entity in entities.values():
        if entity.get("header_order") is None:
            entity["header_order"] = global_header_order
    return {
        "client": raw_config.get("client", "chc"),
        "seed": raw_config["seed"] if "seed" in raw_config else secrets.randbits(63),
        "output_directory": raw_config.get("output_directory", "./output"),
        "generation": raw_config.get("generation", {}),
        "provider_nppes": raw_config.get("provider_nppes", {}),
        "entities": entities,
    }


def _nppes_total(value: object) -> int:
    """Resolve legacy total or explicit individual/organizational counts."""
    if not isinstance(value, dict):
        return 0
    if "count" in value:
        return int(value.get("count", 0))
    return int(value.get("individual", 0)) + int(value.get("organizational", 0))


def _has_explicit_update_selection(selection: Mapping[str, object]) -> bool:
    """Return whether NPPES needs a generic entity or fixture execution path."""
    update = selection.get("updates")
    return (
        "match_codes" in selection
        or _variation_requested(selection)
        or (
            isinstance(update, Mapping)
            and bool({"operation", "expected_outcome", "modifications"}.intersection(update))
        )
    )


def _variation_requested(selection: Mapping[str, object]) -> bool:
    variation = selection.get("variation")
    return isinstance(variation, Mapping) and bool(variation.get("fields_per_record", 0))


def _selected_entity(
    defaults: Mapping[str, object], selection: Mapping[str, object]
) -> dict[str, object]:
    """Apply a compact public entity selection to internal defaults.

    ``layout`` is deliberately the only optional entity setting. The allowed
    layout is checked after normalization against the selected data type; all
    implementation details remain internal.

    Args:
        defaults: Hardcoded implementation defaults for one entity stream.
        selection: Public ``count`` and optional ``layout`` request.

    Returns:
        Enabled internal entity definition with the requested layout profile.
    """
    count_value = selection.get("count", 0)
    if not isinstance(count_value, int):
        raise ConfigurationError("Entity selection count must be an integer")
    count = count_value
    result = {
        **defaults,
        "enabled": count > 0 or "match_codes" in selection or _variation_requested(selection),
        "count": count,
    }
    if isinstance(selection.get("updates"), dict):
        updates = cast(dict[str, object], selection["updates"])
        result["updates"] = {str(key): value for key, value in updates.items()}
    if "match_codes" in selection:
        result["match_codes"] = deepcopy(selection["match_codes"])
    if "variation" in selection:
        result["variation"] = deepcopy(selection["variation"])
    if "source_claims" in selection:
        result["source_claims"] = selection["source_claims"]
    if "scenarios" in selection:
        result["scenarios"] = selection["scenarios"]
    if "frequencies" in selection:
        result["frequencies"] = selection["frequencies"]
    if "claim_frequency" in selection:
        result["claim_frequency"] = selection["claim_frequency"]
    if "linked" in selection:
        result["linked_to_claim"] = bool(selection["linked"])
    for key in ("individual", "organizational"):
        if key in selection:
            result[key] = selection[key]
    if "layout" in selection:
        result["profile"] = selection["layout"]
    output_order = selection.get("output_order")
    if isinstance(output_order, dict) and "headers" in output_order:
        result["header_order"] = output_order["headers"]
    return result


_PAYMENT_SCENARIOS = frozenset({"MATCHED", "REVERSAL", "REPLACEMENT", "STALE", "ORPHAN"})


def _source_claim_path(
    entity: str, raw_entity: Mapping[str, object], config_directory: Path
) -> Path | None:
    """Resolve and validate a source Claim JSONL path for a Payment stream."""
    value = raw_entity.get("source_claims")
    if value is None:
        return None
    if entity not in {"payment_professional", "payment_institutional"}:
        raise ConfigurationError("source_claims is supported only for Payment streams")
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Source Claims path for {entity!r} must be a non-empty string")
    return _resolve_path(value, config_directory)


def _payment_source_path(
    entity: str,
    raw_entity: Mapping[str, object],
    raw_entities: Mapping[str, object],
    config_directory: Path,
    creation_directory: Path,
) -> Path | None:
    """Resolve explicit or same-run Claim input for an enabled Payment stream."""
    explicit = _source_claim_path(entity, raw_entity, config_directory)
    if explicit is not None:
        return explicit
    if entity not in {"payment_professional", "payment_institutional"}:
        return None
    source_entity_names = _payment_source_entity_names(entity)
    for source_entity_name in source_entity_names:
        source_entity = raw_entities.get(source_entity_name)
        if not isinstance(source_entity, dict) or not source_entity.get("enabled"):
            continue
        filename = source_entity.get("filename")
        if isinstance(filename, str) and filename:
            return creation_directory / filename
    count = raw_entity.get("count", 0)
    if (
        raw_entity.get("enabled")
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
        and _payment_requires_claim_source(entity, raw_entity)
    ):
        supported_sources = " or ".join(repr(name) for name in source_entity_names)
        raise ConfigurationError(
            f"Payment stream {entity!r} requires source_claims or an enabled "
            f"{supported_sources} stream"
        )
    return None


def _payment_source_entity_names(entity: str) -> tuple[str, ...]:
    """Return preferred same-run Claim sources for a Payment stream.

    Claims History remains the preferred source when it is enabled.  A normal
    837 Claim is an equally valid source when History is intentionally disabled.
    """
    return {
        "payment_professional": (
            "claim_history_professional",
            "claim_professional",
        ),
        "payment_institutional": (
            "claim_history_institutional",
            "claim_institutional",
        ),
    }.get(entity, ())


def _scenario_counts(entity: str, raw_entity: Mapping[str, object]) -> Mapping[str, int]:
    """Normalize configured Payment source scenarios into immutable counts."""
    value = raw_entity.get("scenarios", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"Scenarios for {entity!r} must be an object")
    scenarios: dict[str, int] = {}
    for name, count in value.items():
        scenario = str(name).upper()
        if scenario not in _PAYMENT_SCENARIOS:
            raise ConfigurationError(f"Unknown Payment source scenario {name!r}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ConfigurationError(f"Scenario count for {scenario!r} must be non-negative")
        scenarios[scenario] = count
    configured_count = raw_entity.get("count", 0)
    if not isinstance(configured_count, int):
        raise ConfigurationError(f"Payment count for {entity!r} must be an integer")
    configured_scenarios = sum(scenarios.values())
    if configured_scenarios > configured_count:
        raise ConfigurationError(
            f"Payment scenario counts for {entity!r} cannot exceed the configured count"
        )
    if scenarios and configured_scenarios < configured_count:
        scenarios["MATCHED"] = scenarios.get("MATCHED", 0) + (
            configured_count - configured_scenarios
        )
    if scenarios.get("REVERSAL", 0) > 0 and not any(
        scenarios.get(name, 0) > 0 for name in ("MATCHED", "REPLACEMENT", "STALE")
    ):
        raise ConfigurationError(
            f"REVERSAL scenario for {entity!r} requires an earlier MATCHED, "
            "REPLACEMENT, or STALE Payment"
        )
    return scenarios


def _payment_requires_claim_source(entity: str, raw_entity: Mapping[str, object]) -> bool:
    """Return whether the configured Payment scenarios need an existing Claim."""
    scenarios = _scenario_counts(entity, raw_entity)
    if not scenarios:
        count = raw_entity.get("count", 0)
        return isinstance(count, int) and not isinstance(count, bool) and count > 0
    return any(scenario != "ORPHAN" and count > 0 for scenario, count in scenarios.items())


_CLAIM_FREQUENCY_CODES = ("1", "7", "8")
_INGESTION_RELATIONSHIPS = frozenset({"SAME", "NEWER", "OLDER"})
_INGESTION_ENTITY_GROUPS = {
    "member": "member",
    "member_mr": "member",
    "provider": "provider",
    "provider_nppes": "provider",
    "claim_professional": "claims",
    "claim_institutional": "claims",
    "claim_history_professional": "claims_history",
    "claim_history_institutional": "claims_history",
    "payment_professional": "payments",
    "payment_institutional": "payments",
}


def _ingestion_date_config(generation: Mapping[str, object]) -> IngestionDateConfig:
    """Parse the opt-in existing/update ingestion-date relationship settings."""
    value = generation.get("ingestion_dates")
    if value is None:
        return IngestionDateConfig(current_ingestion_date(), "SAME", {})
    if not isinstance(value, Mapping):
        raise ConfigurationError("generation.ingestion_dates must be an object")
    existing = value.get("existing", current_ingestion_date())
    relationship = value.get("update", "SAME")
    overrides = value.get("overrides", {})
    if not isinstance(existing, str):
        raise ConfigurationError("generation.ingestion_dates.existing must be YYYYMMDD")
    _parse_ingestion_date(existing)
    if relationship not in _INGESTION_RELATIONSHIPS:
        raise ConfigurationError("generation.ingestion_dates.update must be SAME, NEWER, or OLDER")
    if not isinstance(overrides, Mapping):
        raise ConfigurationError("generation.ingestion_dates.overrides must be an object")
    normalized_overrides: dict[str, IngestionDateOverride] = {}
    for group, override in overrides.items():
        if group not in {"member", "provider", "claims", "claims_history", "payments"}:
            raise ConfigurationError(f"Unsupported ingestion-date entity group {group!r}")
        if isinstance(override, str):
            if override not in _INGESTION_RELATIONSHIPS:
                raise ConfigurationError(
                    f"Ingestion-date override for {group!r} must be SAME, NEWER, or OLDER"
                )
            normalized_overrides[str(group)] = IngestionDateOverride(update_relationship=override)
            continue
        if not isinstance(override, Mapping):
            raise ConfigurationError(
                f"Ingestion-date override for {group!r} must be a relationship or an object"
            )
        override_existing = override.get("existing")
        override_relationship = override.get("update")
        if override_existing is not None:
            if not isinstance(override_existing, str):
                raise ConfigurationError(
                    f"Ingestion-date existing override for {group!r} must be YYYYMMDD"
                )
            _parse_ingestion_date(override_existing)
        if (
            override_relationship is not None
            and override_relationship not in _INGESTION_RELATIONSHIPS
        ):
            raise ConfigurationError(
                f"Ingestion-date update override for {group!r} must be SAME, NEWER, or OLDER"
            )
        normalized_overrides[str(group)] = IngestionDateOverride(
            existing_date=override_existing,
            update_relationship=cast(str | None, override_relationship),
        )
    return IngestionDateConfig(existing, str(relationship), normalized_overrides)


def _update_ingestion_date(entity: str, config: IngestionDateConfig) -> str:
    """Return the configured incoming date for one internal entity stream."""
    group = _INGESTION_ENTITY_GROUPS.get(entity)
    override = config.overrides.get(group) if group is not None else None
    relationship = override.update_relationship if override else None
    existing_value = (
        override.existing_date if override and override.existing_date else config.existing_date
    )
    existing = _parse_ingestion_date(existing_value)
    offset = {"OLDER": -1, "SAME": 0, "NEWER": 1}[relationship or config.update_relationship]
    return (existing + timedelta(days=offset)).strftime("%Y%m%d")


def _creation_ingestion_date(entity: str, config: IngestionDateConfig) -> str:
    """Return the configured creation date for one internal entity stream."""
    group = _INGESTION_ENTITY_GROUPS.get(entity)
    override = config.overrides.get(group) if group is not None else None
    return override.existing_date if override and override.existing_date else config.existing_date


def _parse_ingestion_date(value: str) -> datetime:
    """Validate the compact date contract used by all entity schemas."""
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError as error:
        raise ConfigurationError("INGESTION_DATE must be a valid YYYYMMDD value") from error


def _effective_record_count(
    entity: str, raw_entity: Mapping[str, object], raw_entities: Mapping[str, object]
) -> int:
    """Expand one Claim only when a Replacement Payment requires its original."""
    count = raw_entity.get("count", 0)
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or count > MAX_RECORD_COUNT
    ):
        raise ConfigurationError(f"Count for {entity!r} must be between 0 and {MAX_RECORD_COUNT:,}")
    if entity in {"claim_history_professional", "claim_history_institutional"}:
        # A disabled CH stream has no source dependency. In particular, a
        # zero-count linked selection must not require an otherwise disabled
        # paired 837 stream.
        if count == 0:
            return 0
        if not raw_entity.get("linked_to_claim", False):
            return count
        paired_entity = {
            "claim_history_professional": "claim_professional",
            "claim_history_institutional": "claim_institutional",
        }[entity]
        paired = raw_entities.get(paired_entity)
        if not isinstance(paired, Mapping):
            raise ConfigurationError(f"Claims History {entity!r} has no paired Claim stream")
        paired_configured_count = paired.get("count", 0)
        if count != paired_configured_count:
            raise ConfigurationError(
                f"Linked Claims History {entity!r} count must equal its configured "
                f"Claim count {paired_configured_count}"
            )
        return _effective_record_count(paired_entity, paired, raw_entities)
    if (
        entity in {"claim_professional", "claim_institutional"}
        and count == 1
        and raw_entity.get("frequencies") is None
        and _replacement_requested(entity, raw_entities)
    ):
        return 2
    return count


def _claim_lifecycles(
    entity: str,
    raw_entity: Mapping[str, object],
    count: int,
    seed: int,
    raw_entities: Mapping[str, object],
) -> tuple[tuple[str, int | None], ...]:
    """Resolve Claim lifecycles with deterministic random default frequencies."""
    value = raw_entity.get("frequencies")
    explicit_frequency = raw_entity.get("claim_frequency")
    if entity in {"claim_history_professional", "claim_history_institutional"}:
        return ()
    if entity not in {"claim_professional", "claim_institutional"}:
        if value is not None:
            raise ConfigurationError("frequencies is supported only for Claim streams")
        if explicit_frequency is not None:
            raise ConfigurationError("claim_frequency is supported only for Claim streams")
        return ()
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ConfigurationError(f"Claim count for {entity!r} must be a non-negative integer")
    if explicit_frequency is not None:
        if value is not None:
            raise ConfigurationError("claim_frequency cannot be combined with frequencies")
        if explicit_frequency not in _CLAIM_FREQUENCY_CODES:
            raise ConfigurationError("claim_frequency must be 1, 7, or 8")
        return tuple((str(explicit_frequency), index) for index in range(count))
    if value is None:
        codes = _random_claim_frequencies(entity, count, seed, raw_entities)
    else:
        if not isinstance(value, dict):
            raise ConfigurationError(f"Frequencies for {entity!r} must be an object")
        frequencies: dict[str, int] = {}
        for code, configured_count in value.items():
            frequency = str(code)
            if frequency not in _CLAIM_FREQUENCY_CODES:
                raise ConfigurationError(
                    f"Unsupported Claim frequency {code!r}; supported values are 1, 7, and 8"
                )
            if (
                not isinstance(configured_count, int)
                or isinstance(configured_count, bool)
                or configured_count < 0
            ):
                raise ConfigurationError(f"Frequency count for {frequency!r} must be non-negative")
            frequencies[frequency] = configured_count
        if sum(frequencies.values()) != count:
            raise ConfigurationError(
                f"Claim frequency counts for {entity!r} must add up to the configured count"
            )
        codes = [
            frequency for frequency in ("1", "7", "8") for _ in range(frequencies.get(frequency, 0))
        ]
    original_indexes = [index for index, frequency in enumerate(codes) if frequency == "1"]
    if any(frequency in {"7", "8"} for frequency in codes) and not original_indexes:
        raise ConfigurationError(
            f"Claim frequencies 7 and 8 for {entity!r} require at least one frequency 1 Claim"
        )
    original_cursor = 0
    lifecycles: list[tuple[str, int | None]] = []
    for index, frequency in enumerate(codes):
        if frequency == "1":
            lifecycles.append((frequency, index))
            continue
        lifecycles.append((frequency, original_indexes[original_cursor % len(original_indexes)]))
        original_cursor += 1
    return tuple(lifecycles)


def _random_claim_frequencies(
    entity: str,
    count: int,
    seed: int,
    raw_entities: Mapping[str, object],
) -> list[str]:
    """Choose valid lifecycles without requiring a configured distribution."""
    if count == 0:
        return []
    randomizer = Random(seed + (0 if entity == "claim_professional" else 1))
    replacement_requested = _replacement_requested(entity, raw_entities)
    if replacement_requested and count == 2:
        return ["1", "7"]
    codes = [randomizer.choice(_CLAIM_FREQUENCY_CODES) for _ in range(count)]
    required_originals = min(2, count)
    original_positions = set(randomizer.sample(range(count), required_originals))
    for position in original_positions:
        codes[position] = "1"
    if replacement_requested and "7" not in codes:
        replacement_positions = [
            position for position in range(count) if position not in original_positions
        ]
        codes[randomizer.choice(replacement_positions)] = "7"
    return codes


def _replacement_requested(entity: str, raw_entities: Mapping[str, object]) -> bool:
    """Return whether the corresponding enabled Payment stream requests a replacement."""
    payment_entity = {
        "claim_professional": "payment_professional",
        "claim_institutional": "payment_institutional",
    }[entity]
    payment_config = raw_entities.get(payment_entity, {})
    if not isinstance(payment_config, dict) or not payment_config.get("enabled"):
        return False
    scenarios = payment_config.get("scenarios", {})
    return (
        isinstance(scenarios, dict)
        and isinstance(scenarios.get("REPLACEMENT", 0), int)
        and scenarios.get("REPLACEMENT", 0) > 0
    )


def _entity_defaults() -> dict[str, dict[str, object]]:
    """Build internal defaults for all supported entities.

    The schema paths and implementation module names are intentionally not
    configurable by end users.  Keeping them here gives the public config a
    small, stable surface and prevents an input file from selecting arbitrary
    code to import.

    Returns:
        Per-entity internal defaults used while normalizing public config.
    """
    packaged_schema_root = Path(str(files("test_data_generator").joinpath("schema", "json")))
    schema_root = (
        packaged_schema_root
        if packaged_schema_root.is_dir()
        else Path(__file__).resolve().parents[3] / "schema" / "json"
    )
    return {
        "provider": {
            "enabled": False,
            "count": 0,
            "profile": "provider",
            "schema": str(schema_root / "provider/provider.schema.json"),
            "module": "test_data_generator.entities.provider",
            "filename": "provider_cdf.jsonl",
            "updates": {},
            "header_order": None,
        },
        "provider_nppes": {
            "enabled": False,
            "count": 0,
            # NPPES emits two explicit source shapes and therefore bypasses
            # layout/schema projection in the shared engine.  This profile and
            # schema only satisfy the normal entity contract for configuration
            # and client-value loading.
            "profile": "provider",
            "schema": str(schema_root / "provider/provider.schema.json"),
            "module": "test_data_generator.entities.provider_nppes",
            "filename": "provider_nppes.jsonl",
            "updates": {},
            "header_order": None,
        },
        "member": {
            "enabled": False,
            "count": 0,
            "profile": "member",
            "schema": str(schema_root / "member/member.schema.json"),
            "module": "test_data_generator.entities.member",
            "filename": "members.jsonl",
            "updates": {},
            "header_order": None,
        },
        "member_mr": {
            "enabled": False,
            "count": 0,
            "profile": "member",
            "schema": str(schema_root / "member/member.schema.json"),
            "module": "test_data_generator.entities.member",
            "filename": "member_roster.jsonl",
            "updates": {},
            "header_order": None,
            "source_entity": "member",
            "file_type": "MR",
        },
        "claim_professional": {
            "enabled": False,
            "count": 0,
            "profile": "claim-professional",
            "schema": str(schema_root / "claim/claim.schema.json"),
            "module": "test_data_generator.entities.claim",
            "filename": "claims_professional.jsonl",
            "updates": {},
            "header_order": None,
        },
        "claim_history_professional": {
            "enabled": False,
            "count": 0,
            "profile": "claim-professional",
            "schema": str(schema_root / "claim/claim.schema.json"),
            "module": "test_data_generator.entities.claim",
            "filename": "claims_history_professional.jsonl",
            "updates": {},
            "header_order": None,
            "file_type": "CH",
        },
        "claim_institutional": {
            "enabled": False,
            "count": 0,
            "profile": "claim-institutional",
            "schema": str(schema_root / "claim/claim.schema.json"),
            "module": "test_data_generator.entities.claim",
            "filename": "claims_institutional.jsonl",
            "updates": {},
            "header_order": None,
        },
        "claim_history_institutional": {
            "enabled": False,
            "count": 0,
            "profile": "claim-institutional",
            "schema": str(schema_root / "claim/claim.schema.json"),
            "module": "test_data_generator.entities.claim",
            "filename": "claims_history_institutional.jsonl",
            "updates": {},
            "header_order": None,
            "file_type": "CH",
        },
        "payment_professional": {
            "enabled": False,
            "count": 0,
            "profile": "payment-professional",
            "schema": str(schema_root / "payment/payment.schema.json"),
            "module": "test_data_generator.entities.payment",
            "filename": "payments_professional.jsonl",
            "updates": {},
            "header_order": None,
        },
        "payment_institutional": {
            "enabled": False,
            "count": 0,
            "profile": "payment-institutional",
            "schema": str(schema_root / "payment/payment.schema.json"),
            "module": "test_data_generator.entities.payment",
            "filename": "payments_institutional.jsonl",
            "updates": {},
            "header_order": None,
        },
    }


def _validate_filename(entity: str, filename: str, output_directory: Path) -> None:
    """Reject an unsafe configured output name for a known entity.

    Args:
        entity: Configured entity name.
        filename: Requested relative JSONL name.
        output_directory: Root directory containing generated files.

    Raises:
        ConfigurationError: If the output name escapes the output directory.
    """
    try:
        resolve_output_path(output_directory, filename)
    except ValueError as error:
        raise ConfigurationError(f"Invalid filename for entity {entity!r}: {error}") from error


def _validate_unique_filenames(entities: list[EntityConfig]) -> None:
    """Ensure enabled entities cannot overwrite one another's JSONL output.

    Args:
        entities: Fully resolved enabled entity definitions.

    Raises:
        ConfigurationError: If two enabled entities use the same filename.
    """
    filenames = [entity.filename for entity in entities]
    if len(filenames) != len(set(filenames)):
        raise ConfigurationError("Enabled entities must use distinct output filenames")


def _validate_profile(entity: str, profile: object) -> None:
    """Validate that an entity uses one of its supported layout profiles.

    Args:
        entity: Enabled entity name from the run configuration.
        profile: Requested profile identifier.

    Raises:
        ConfigurationError: If the profile is unknown or incompatible with the entity.
    """
    permitted_profiles = {
        "provider": frozenset({"provider"}),
        "provider_nppes": frozenset({"provider"}),
        "member": frozenset({"member"}),
        "member_mr": frozenset({"member"}),
        "claim_professional": frozenset({"claim-professional"}),
        "claim_institutional": frozenset({"claim-institutional"}),
        "claim_history_professional": frozenset({"claim-professional"}),
        "claim_history_institutional": frozenset({"claim-institutional"}),
        "payment_professional": frozenset({"payment-professional"}),
        "payment_institutional": frozenset({"payment-institutional"}),
    }
    if not isinstance(profile, str) or profile not in available_profiles():
        raise ConfigurationError(f"Enabled entity {entity!r} uses an unknown layout profile")
    if profile not in permitted_profiles.get(entity, frozenset()):
        raise ConfigurationError(
            f"Layout profile {profile!r} is not supported by entity {entity!r}"
        )
    try:
        load_layout(profile)
    except ValueError as error:
        raise ConfigurationError(
            f"Could not load layout profile for enabled entity {entity!r}"
        ) from error


def _load_json(path: Path, label: str) -> dict[str, Any]:
    """Read one JSON object and normalize read errors.

    Args:
        path: JSON file to read.
        label: Safe description used in error messages.

    Returns:
        Decoded JSON object.

    Raises:
        ConfigurationError: If the file cannot be read, decoded, or is not an object.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"Could not read {label} file {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"Could not decode {label} JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"The {label} file {path} must contain a JSON object")
    return value


def _validate_schema(raw_config: dict[str, Any]) -> None:
    """Validate raw configuration against the packaged run schema.

    Args:
        raw_config: Decoded root configuration object.

    Raises:
        ConfigurationError: If the object violates the run configuration schema.
    """
    schema = _load_packaged_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(raw_config), key=str)
    if errors:
        details = "; ".join(_safe_validation_detail(error) for error in errors)
        raise ConfigurationError(f"Invalid configuration: {details}")


def _validate_execution_schema(raw_config: dict[str, Any]) -> None:
    """Validate a focused execution request before resolving its generator path."""
    schema = _load_execution_schema()
    errors = sorted(Draft202012Validator(schema).iter_errors(raw_config), key=str)
    if errors:
        details = "; ".join(_safe_validation_detail(error) for error in errors)
        raise ConfigurationError(f"Invalid execution configuration: {details}")


def _load_packaged_schema() -> dict[str, Any]:
    """Load the run configuration schema bundled with the Python package.

    Returns:
        Decoded run configuration schema.

    Raises:
        ConfigurationError: If the packaged schema cannot be read, decoded, or
            is not a JSON object.
    """
    resource = files(__package__).joinpath("run_config.schema.json")
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError("Could not read packaged run configuration schema") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError("Could not decode packaged run configuration schema") from error
    if not isinstance(value, dict):
        raise ConfigurationError("The packaged run configuration schema must contain a JSON object")
    return value


def _load_execution_schema() -> dict[str, Any]:
    """Load the bundled schema for ``runconfig.json`` requests."""
    resource = files(__package__).joinpath("execution_config.schema.json")
    try:
        value = json.loads(resource.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(
            "Could not read packaged execution configuration schema"
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            "Could not decode packaged execution configuration schema"
        ) from error
    if not isinstance(value, dict):
        raise ConfigurationError(
            "The packaged execution configuration schema must contain a JSON object"
        )
    return value


def resolve_output_path(output_directory: Path, filename: str) -> Path:
    """Resolve a configured filename while keeping it inside the output directory.

    Args:
        output_directory: Root directory configured for generated JSONL files.
        filename: Relative JSONL filename from an entity configuration.

    Returns:
        Resolved destination path contained by ``output_directory``.

    Raises:
        ValueError: If the filename is absolute or escapes the output directory.
    """
    path = Path(filename)
    components = filename.replace("\\", "/").split("/")
    if path.is_absolute() or PureWindowsPath(filename).is_absolute():
        raise ValueError("must be relative to the output directory")
    if any(component in {".", ".."} for component in components):
        raise ValueError("must not contain '.' or '..' path components")

    output_root = output_directory.resolve()
    destination = (output_root / path).resolve()
    if not destination.is_relative_to(output_root):
        raise ValueError("resolves outside the output directory")
    return destination


def _output_subdirectory(output_directory: Path, value: object, label: str) -> Path:
    """Resolve one configured generation directory inside the output root."""
    directory = str(value)
    try:
        return resolve_output_path(output_directory, directory)
    except ValueError as error:
        raise ConfigurationError(f"Invalid {label} output directory: {error}") from error


def _safe_validation_detail(error: Any) -> str:
    """Describe a schema failure without echoing configured values.

    Args:
        error: JSON Schema validation error.

    Returns:
        Safe field and constraint summary for CLI output that excludes supplied
        values, which may contain sensitive data.
    """
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    validator = error.validator
    constraint = error.validator_value
    if validator == "type":
        return f"{path}: expected type {constraint}"
    if validator == "required":
        return f"{path}: is missing a required property"
    if validator == "additionalProperties":
        return f"{path}: contains an unsupported property"
    if validator == "pattern":
        return f"{path}: must match the required format"
    if validator == "minLength":
        return f"{path}: must not be empty"
    if validator == "minimum":
        return f"{path}: must be at least {constraint}"
    if validator == "minProperties":
        return f"{path}: must contain at least {constraint} property"
    if validator == "const":
        return f"{path}: must use the required value"
    return f"{path}: failed {validator} validation"


def _resolve_path(value: str, config_directory: Path) -> Path:
    """Resolve a configuration path relative to its configuration file.

    Args:
        value: Absolute or configuration-relative path string.
        config_directory: Directory containing the root configuration.

    Returns:
        Absolute resolved filesystem path. Relative values are interpreted from
        the configuration file rather than the process working directory.
    """
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (config_directory / candidate).resolve()
