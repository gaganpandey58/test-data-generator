"""Load the small, file-based configuration for synthetic data generation."""

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from healthcare_test_data.errors import ConfigurationError
from healthcare_test_data.layouts import available_profiles, load_layout


@dataclass(frozen=True)
class EntityConfig:
    """Describe one enabled entity generation request."""

    name: str
    count: int
    scenarios: Mapping[str, int]
    profile: str
    schema: Path
    module: str
    filename: str


@dataclass(frozen=True)
class RunConfig:
    """Describe the enabled entities and shared settings for one generator run."""

    seed: int
    output_directory: Path
    entities: tuple[EntityConfig, ...]
    disabled_filenames: tuple[str, ...]


def load_config(path: Path) -> RunConfig:
    """Load one simple generation configuration from disk.

    Args:
        path: Path to the root JSON configuration.

    Returns:
        Resolved shared settings and enabled entity definitions.

    Raises:
        ConfigurationError: If the configuration or an enabled schema is invalid.
    """
    config_path = path.resolve()
    raw_config = _load_json(config_path, "configuration")
    _validate_schema(raw_config)
    raw_config = _normalize_config(raw_config)
    try:
        seed = raw_config["seed"]
        output_directory = _resolve_path(raw_config["output_directory"], config_path.parent)
        raw_entities = raw_config["entities"]
    except KeyError as error:
        raise ConfigurationError(f"Invalid configuration: missing {error.args[0]!r}") from error

    entities: list[EntityConfig] = []
    disabled_filenames: list[str] = []
    for name, raw_entity in raw_entities.items():
        if not raw_entity["enabled"]:
            _validate_filename(name, raw_entity["filename"], output_directory)
            disabled_filenames.append(raw_entity["filename"])
            continue
        profile = raw_entity.get("profile", "claim-professional" if name == "claim" else name)
        _validate_profile(name, profile)
        scenarios = raw_entity.get("scenarios", {})
        _validate_scenarios(name, scenarios, raw_entity["count"])
        schema = _resolve_path(raw_entity["schema"], config_path.parent)
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
                scenarios=dict(scenarios),
                profile=profile,
                schema=schema,
                module=raw_entity["module"],
                filename=filename,
            )
        )
    _validate_unique_filenames(entities)
    return RunConfig(
        seed=seed,
        output_directory=output_directory,
        entities=tuple(entities),
        disabled_filenames=tuple(disabled_filenames),
    )


def _normalize_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    """Expand the short root entity form into the detailed internal form.

    The public short form keeps a run focused on the only values people usually
    change: each selected entity's count and scenarios.  The detailed
    ``entities`` form remains supported for output filenames and profiles.

    Args:
        raw_config: Schema-valid decoded root configuration.

    Returns:
        A detailed configuration with defaults for every known entity.
    """
    defaults = _entity_defaults()
    supplied_entities = raw_config.get("entities")
    if isinstance(supplied_entities, dict):
        entities = {name: {**defaults[name], **value} for name, value in supplied_entities.items()}
        for name, default in defaults.items():
            entities.setdefault(name, default)
    else:
        entities = {
            name: {**default, "enabled": name in raw_config, **raw_config.get(name, {})}
            for name, default in defaults.items()
        }
    return {
        "seed": raw_config.get("seed", 20260805),
        "output_directory": raw_config.get("output_directory", "./output"),
        "entities": entities,
    }


def _entity_defaults() -> dict[str, dict[str, object]]:
    """Return immutable-by-convention defaults for the supported entities."""
    schema_root = Path(__file__).resolve().parents[2] / "schemas"
    return {
        "provider": {
            "enabled": False,
            "count": 0,
            "profile": "provider",
            "scenarios": {},
            "schema": str(schema_root / "provider/provider.schema.json"),
            "module": "healthcare_test_data.entities.provider",
            "filename": "providers.jsonl",
        },
        "member": {
            "enabled": False,
            "count": 0,
            "profile": "member",
            "scenarios": {},
            "schema": str(schema_root / "member/member.schema.json"),
            "module": "healthcare_test_data.entities.member",
            "filename": "members.jsonl",
        },
        "claim": {
            "enabled": False,
            "count": 0,
            "profile": "claim-professional",
            "scenarios": {},
            "schema": str(schema_root / "claim/claim.schema.json"),
            "module": "healthcare_test_data.entities.claim",
            "filename": "claims.jsonl",
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
    """Ensure enabled entities cannot overwrite one another's JSONL output."""
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
        "claim": frozenset({"claim-professional", "claim-institutional"}),
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


def _validate_scenarios(entity: str, scenarios: object, count: int) -> None:
    """Validate configured scenario quantities for one enabled entity.

    Args:
        entity: Enabled entity name from the run configuration.
        scenarios: Raw scenario name-to-quantity mapping.
        count: Exact entity output count.

    Raises:
        ConfigurationError: If a scenario is unsupported, negative, or exceeds count.
    """
    supported_scenarios = {
        "provider": frozenset({"new", "changed", "duplicate", "stale", "incomplete"}),
        "member": frozenset({"new", "changed", "duplicate", "stale", "incomplete"}),
        "claim": frozenset(
            {
                "new",
                "changed",
                "duplicate",
                "stale",
                "incomplete",
                "replacement",
                "void",
                "orphan_payment",
            }
        ),
    }
    if not isinstance(scenarios, dict):
        raise ConfigurationError(f"Enabled entity {entity!r} scenarios must be an object")
    permitted = supported_scenarios.get(entity, frozenset())
    total = 0
    has_non_new_scenario = False
    for scenario, quantity in scenarios.items():
        if scenario not in permitted:
            raise ConfigurationError(f"Scenario {scenario!r} is not supported by entity {entity!r}")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise ConfigurationError(
                f"Scenario {scenario!r} for entity {entity!r} must be a non-negative integer"
            )
        total += quantity
        if scenario != "new" and quantity > 0:
            has_non_new_scenario = True
    if total > count:
        raise ConfigurationError(
            f"Scenario total for enabled entity {entity!r} must not exceed count"
        )
    if has_non_new_scenario and total >= count:
        raise ConfigurationError(
            f"Non-new scenarios for enabled entity {entity!r} require at least one baseline record"
        )


def _validate_claim_relationships(entities: list[EntityConfig]) -> None:
    """Require claim generation to include the member and provider it links.

    Args:
        entities: Enabled entity configurations from the root config.

    Raises:
        ConfigurationError: If enabled claims would reference an absent entity.
    """
    enabled_names = {entity.name for entity in entities}
    if "claim" in enabled_names and not {"member", "provider"} <= enabled_names:
        raise ConfigurationError(
            "Enabled claim generation requires enabled member and provider entities"
        )


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
    resource = files("healthcare_test_data").joinpath("run_config.schema.json")
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
        Safe field and constraint summary for CLI output.
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
        Absolute resolved filesystem path.
    """
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (config_directory / candidate).resolve()
