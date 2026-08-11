"""Generate, validate, and atomically publish source-shaped entity JSONL.

The engine owns the generic generation lifecycle: load an approved entity
generator, plan record variations, validate every generated row against its
JSON Schema, and replace the final output only after a complete successful
write. Entity modules supply domain fields but do not manage files.
"""

import importlib
import inspect
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import orjson
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from healthcare_test_data.config import EntityConfig, resolve_output_path
from healthcare_test_data.errors import GenerationError
from healthcare_test_data.layouts import project_record
from healthcare_test_data.scenarios import Scenario


def run_entity(
    entity: EntityConfig,
    seed: int,
    output_directory: Path,
    entity_counts: Mapping[str, int] | None = None,
) -> Path:
    """Generate and publish one configured entity as a validated JSONL file.

    The destination is replaced atomically only when all requested records are
    generated and schema-valid.  Any failure removes the temporary file and
    leaves an existing destination untouched.

    Args:
        entity: Enabled entity definition to generate.
        seed: Shared deterministic seed from the run configuration.
        output_directory: Root directory for generated output.
        entity_counts: Optional enabled-entity counts for relational generators.

    Returns:
        Final published JSONL path.

    Raises:
        GenerationError: If module loading, schema validation, or output fails.
    """
    try:
        final_path = resolve_output_path(output_directory, entity.filename)
    except ValueError as error:
        raise GenerationError(f"Configured filename for entity {entity.name!r} {error}") from error
    temporary_path: Path | None = None

    try:
        generate_record = _load_generator(entity.module)
        accepts_entity_counts = _accepts_entity_counts(generate_record)
        accepts_scenario = _accepts_scenario(generate_record)
        accepts_profile = _accepts_profile(generate_record)
        accepts_entity_name = _accepts_entity_name(generate_record)
        accepts_client_headers = _accepts_client_headers(generate_record)
        accepts_client_values = _accepts_client_values(generate_record)
        validator = _load_validator(entity.schema)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            dir=final_path.parent,
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            for index in range(entity.count):
                record = _generate_record(
                    generate_record,
                    seed,
                    index,
                    entity_counts,
                    accepts_entity_counts,
                    None,
                    accepts_scenario,
                    entity.profile,
                    accepts_profile,
                    entity.name,
                    accepts_entity_name,
                    entity.client_headers,
                    accepts_client_headers,
                    entity.client_values,
                    accepts_client_values,
                )
                record = project_record(record, entity.profile)
                try:
                    validator.validate(record)
                except ValidationError as error:
                    raise GenerationError(_validation_detail(error)) from error
                output_file.write(orjson.dumps(record))
                output_file.write(b"\n")
        if temporary_path is None:
            raise RuntimeError("Could not create a temporary output file")
        return temporary_path.replace(final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _load_generator(module_name: str) -> Callable[..., dict[str, object]]:
    """Import one entity module and obtain its record generator.

    Args:
        module_name: Dotted Python module path from entity configuration.

    Returns:
        Callable that produces one record from a seed and index. It may also
        accept supported optional relationship, scenario, and profile context.

    Raises:
        GenerationError: If importing or locating ``generate_record`` fails.
    """
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        missing_module = error.name or module_name
        raise GenerationError(
            f"Could not import entity module {module_name!r}: missing module {missing_module!r}"
        ) from error
    except ImportError as error:
        raise GenerationError(
            f"Could not import entity module {module_name!r} because an import failed"
        ) from error

    generate_record = getattr(module, "generate_record", None)
    if not callable(generate_record):
        raise GenerationError(f"Entity module {module_name!r} must expose callable generate_record")
    return cast(Callable[..., dict[str, object]], generate_record)


def _accepts_entity_counts(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts the optional relationship context.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable can accept positional seed, index, and
        entity-count relationship context.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, {})
    except TypeError:
        return False
    return True


def _accepts_scenario(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts one planned scenario variation.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable accepts a ``scenario`` keyword argument.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, scenario=None)
    except TypeError:
        return False
    return True


def _accepts_profile(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts the configured layout profile.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable accepts a ``profile`` keyword argument.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, profile=None)
    except TypeError:
        return False
    return True


def _accepts_entity_name(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts its resolved entity identity.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable accepts an ``entity_name`` keyword argument.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, entity_name=None)
    except TypeError:
        return False
    return True


def _accepts_client_headers(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts resolved client header values.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable accepts a ``client_headers`` keyword.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, client_headers={})
    except TypeError:
        return False
    return True


def _accepts_client_values(generate_record: Callable[..., dict[str, object]]) -> bool:
    """Determine whether a generator accepts client-owned body values.

    Args:
        generate_record: Entity record generator loaded from its module.

    Returns:
        ``True`` when the callable accepts a ``client_values`` keyword.
    """
    try:
        inspect.signature(generate_record).bind(0, 0, client_values={})
    except TypeError:
        return False
    return True


def _generate_record(
    generate_record: Callable[..., dict[str, object]],
    seed: int,
    index: int,
    entity_counts: Mapping[str, int] | None,
    accepts_entity_counts: bool,
    scenario: Scenario | None,
    accepts_scenario: bool,
    profile: str,
    accepts_profile: bool,
    entity_name: str,
    accepts_entity_name: bool,
    client_headers: Mapping[str, object],
    accepts_client_headers: bool,
    client_values: Mapping[str, object],
    accepts_client_values: bool,
) -> dict[str, object]:
    """Call an entity generator with only the context it supports.

    The signature inspection performed by the helper functions lets older,
    simple generators remain compatible while newer generators receive their
    scenario plan, GDF profile, and relationship context.

    Args:
        generate_record: Imported callable that builds one entity record.
        seed: Deterministic run seed.
        index: Zero-based output row index.
        entity_counts: Enabled entity counts available for relationship links.
        accepts_entity_counts: Whether the callable accepts ``entity_counts``.
        scenario: Planned variation for this row, or ``None`` for a baseline.
        accepts_scenario: Whether the callable accepts the scenario keyword.
        profile: Configured source-layout profile identifier.
        accepts_profile: Whether the callable accepts the profile keyword.
        entity_name: Resolved configuration identity for this output stream.
        accepts_entity_name: Whether the callable accepts the identity keyword.
        client_headers: Resolved client-specific envelope header values.
        accepts_client_headers: Whether the callable accepts those values.
        client_values: Resolved client-specific body generation values.
        accepts_client_values: Whether the callable accepts those values.

    Returns:
        One unvalidated record for the engine to validate and serialize.
    """
    kwargs: dict[str, object] = {}
    if accepts_scenario:
        kwargs["scenario"] = scenario
    if accepts_profile:
        kwargs["profile"] = profile
    if accepts_entity_name:
        kwargs["entity_name"] = entity_name
    if accepts_client_headers:
        kwargs["client_headers"] = client_headers
    if accepts_client_values:
        kwargs["client_values"] = client_values
    if kwargs:
        if accepts_entity_counts:
            return generate_record(seed, index, entity_counts, **kwargs)
        return generate_record(seed, index, **kwargs)
    if entity_counts is not None and accepts_entity_counts:
        return generate_record(seed, index, entity_counts)
    return generate_record(seed, index)


def _load_validator(schema_path: Path) -> Draft202012Validator:
    """Load one entity JSON Schema into a Draft 2020-12 validator.

    Args:
        schema_path: JSON Schema file for the configured entity.

    Returns:
        Ready-to-use Draft 2020-12 validator.

    Raises:
        GenerationError: If the schema file cannot be read or decoded.
    """
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise GenerationError(f"Could not read entity schema {schema_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise GenerationError(f"Could not decode entity schema {schema_path}: {error}") from error
    return Draft202012Validator(cast(dict[str, Any], schema))


def _validation_detail(error: ValidationError) -> str:
    """Format a safe schema-validation error for generated records.

    Args:
        error: Validation error raised for one generated record.

    Returns:
        Field path and failed constraint without including generated values,
        which keeps failures useful without echoing test data.
    """
    validation_path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    detail = (
        f": expected type {error.validator_value}"
        if error.validator == "type"
        else f": failed {error.validator} validation"
    )
    return f"Generated record failed schema validation at {validation_path}{detail}"
