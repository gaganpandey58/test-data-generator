"""Load, normalize, and validate the public generator configuration.

The external JSON config remains deliberately short: callers choose a client,
entity record counts, a seed, and an output directory. This
module expands those choices into immutable internal entity definitions with
hardcoded schema, module, profile, and filename defaults, then validates paths
and relationships before generation can begin.
"""

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from test_data_generator.configuration.profiles import (
    available_clients,
    load_client_headers,
    load_client_values,
)
from test_data_generator.core.errors import ConfigurationError
from test_data_generator.layouts import available_profiles, load_layout


@dataclass(frozen=True)
class EntityConfig:
    """Describe one fully resolved enabled-entity generation request.

    Attributes:
        name: Internal entity identifier, such as ``member`` or
            ``claim_professional``. Claim stream names are derived from the
            public ``claims.professional`` and ``claims.institutional`` keys.
        count: Exact number of rows the entity must emit.
        client_headers: Immutable client-specific envelope values for this
            output stream.
        client_values: Immutable client-specific non-header generation values.
        profile: GDF source-layout profile applied to generated records.
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


@dataclass(frozen=True)
class RunConfig:
    """Describe the resolved settings needed for one complete generator run.

    Attributes:
        client: Selected checked-in client header profile.
        seed: Deterministic seed shared by every enabled entity generator.
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
    invalid_values_catalog: Path | None = None
    nppes_count: int = 0
    nppes_filename: str = "provider_nppes.jsonl"
    provider_linked: bool = False


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
    _validate_schema(raw_config)
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
        entities.append(
            EntityConfig(
                name=name,
                count=raw_entity["count"],
                client_headers=load_client_headers(client, name),
                client_values=load_client_values(client, name),
                profile=profile,
                schema=schema,
                module=raw_entity["module"],
                filename=filename,
                update=raw_entity.get("updates", {}),
                header_order=str(raw_entity.get("header_order", "source")),
                source_claims=_source_claim_path(name, raw_entity, config_path.parent),
                scenarios=_scenario_counts(name, raw_entity),
            )
        )
    _validate_unique_filenames(entities)
    generation_config = generation if isinstance(generation, dict) else {}
    creation_config = generation_config.get("creation", {})
    update_config = generation_config.get("updates", {})
    creation_directory = output_directory / str(creation_config.get("directory", "new-test-data"))
    update_directory = output_directory / str(update_config.get("directory", "update-test-data"))
    rule_catalog_value = update_config.get("rule_catalog")
    rule_catalog = (
        _resolve_path(str(rule_catalog_value), config_path.parent)
        if isinstance(rule_catalog_value, str)
        else None
    )
    invalid_catalog_value = update_config.get("invalid_values_catalog")
    invalid_values_catalog = (
        _resolve_path(str(invalid_catalog_value), config_path.parent)
        if isinstance(invalid_catalog_value, str)
        else Path(__file__).with_name("invalid-values.json")
    )
    if not invalid_values_catalog.is_file():
        raise ConfigurationError(f"Invalid-value catalog does not exist: {invalid_values_catalog}")
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
        update_defaults=update_config,
        invalid_values_catalog=invalid_values_catalog,
        nppes_count=nppes_count,
        provider_linked=provider_linked,
    )


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
    for name in ("provider", "member"):
        value = raw_config.get(name)
        if isinstance(value, dict):
            if name == "provider" and (
                isinstance(value.get("nppes"), dict) or isinstance(value.get("cdf"), dict)
            ):
                nppes = value.get("nppes", {})
                cdf = value.get("cdf", {})
                nppes_count = _nppes_total(nppes)
                additional_count = cdf.get("additional_count", 0) if isinstance(cdf, dict) else 0
                selection = {
                    key: item for key, item in value.items() if key not in {"nppes", "cdf"}
                }
                selection["count"] = int(nppes_count) + int(additional_count)
                entities[name] = _selected_entity(entities[name], selection)
                normalized_nppes = {
                    "count": int(nppes_count),
                    **(
                        {
                            key: int(nppes[key])
                            for key in ("individual", "organizational")
                            if key in nppes
                        }
                        if isinstance(nppes, dict)
                        else {}
                    ),
                }
                raw_config["provider_nppes"] = normalized_nppes
                continue
            entities[name] = _selected_entity(entities[name], value)

    claims = raw_config.get("claims")
    if isinstance(claims, dict):
        for stream, entity_name in (
            ("professional", "claim_professional"),
            ("institutional", "claim_institutional"),
        ):
            value = claims.get(stream)
            if isinstance(value, dict):
                entities[entity_name] = _selected_entity(entities[entity_name], value)

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
        "seed": raw_config.get("seed", 20260805),
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
    count_value = selection.get("count")
    if not isinstance(count_value, int):
        raise ConfigurationError("Entity selection count must be an integer")
    count = count_value
    result = {**defaults, "enabled": count > 0, "count": count}
    if isinstance(selection.get("updates"), dict):
        updates = cast(dict[str, object], selection["updates"])
        result["updates"] = {str(key): value for key, value in updates.items()}
    if "source_claims" in selection:
        result["source_claims"] = selection["source_claims"]
    if "scenarios" in selection:
        result["scenarios"] = selection["scenarios"]
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
    path = _resolve_path(value, config_directory)
    if raw_entity.get("enabled") and not path.is_file():
        raise ConfigurationError(f"Source Claims file does not exist: {path}")
    return path


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
    if scenarios and sum(scenarios.values()) != configured_count:
        raise ConfigurationError(
            f"Payment scenario counts for {entity!r} must add up to the configured count"
        )
    return scenarios


def _entity_defaults() -> dict[str, dict[str, object]]:
    """Build internal defaults for all supported entities.

    The schema paths and implementation module names are intentionally not
    configurable by end users.  Keeping them here gives the public config a
    small, stable surface and prevents an input file from selecting arbitrary
    code to import.

    Returns:
        Per-entity internal defaults used while normalizing public config.
    """
    schema_root = Path(__file__).resolve().parents[3] / "schema" / "json"
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
        "member": frozenset({"member"}),
        "claim_professional": frozenset({"claim-professional"}),
        "claim_institutional": frozenset({"claim-institutional"}),
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
