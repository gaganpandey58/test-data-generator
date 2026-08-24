"""Generate, validate, and atomically publish layout-shaped JSONL files."""

import importlib
import json
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import orjson
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from test_data_generator.configuration.config import EntityConfig, resolve_output_path
from test_data_generator.core.errors import GenerationError
from test_data_generator.layouts import load_layout, project_record
from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import OperationType, UpdateRequest, resolve_update
from test_data_generator.update.validation import validate_update_contract


def run_entity(
    entity: EntityConfig, seed: int, output_directory: Path, counts: Mapping[str, int]
) -> Path:
    """Generate one configured JSONL stream and publish it atomically.

    All entity modules use the same small call contract: seed, row index,
    enabled counts, profile headers, profile values, and layout name. The
    engine does not inspect signatures or plan variations because current
    generation is happy-path only.
    """
    generate_record = _load_generator(entity.module)
    records = (
        _build_record(entity, seed, index, counts, generate_record) for index in range(entity.count)
    )
    return run_records(entity, records, output_directory)


def run_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    output_directory: Path,
) -> Path:
    """Validate and atomically publish already-derived records for one entity."""
    final_path = resolve_output_path(output_directory, entity.filename)
    temporary_path: Path | None = None
    try:
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
            for record in records:
                normalized = dict(record)
                try:
                    validator.validate(normalized)
                except ValidationError as error:
                    raise GenerationError(_validation_detail(error)) from error
                output_file.write(orjson.dumps(normalized))
                output_file.write(b"\n")
        return temporary_path.replace(final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def run_update_entity(
    entity: EntityConfig,
    seed: int,
    output_directory: Path,
    counts: Mapping[str, int],
    request: UpdateRequest,
    rules: EntityRules,
) -> Path:
    """Generate update JSONL atomically without sidecar metadata files."""
    generate_record = _load_generator(entity.module)
    records = (
        _build_record(entity, seed, index, counts, generate_record) for index in range(entity.count)
    )
    return run_update_records(entity, records, seed, output_directory, request, rules)


def run_update_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    seed: int,
    output_directory: Path,
    request: UpdateRequest,
    rules: EntityRules,
) -> Path:
    """Apply the shared update engine to already-derived base records."""
    update_filename = entity.filename.removesuffix(".jsonl") + ".update.jsonl"
    final_path = resolve_output_path(output_directory, update_filename)
    temporary_path: Path | None = None
    try:
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
            for index, base in enumerate(records):
                base_record = dict(base)
                resolved = resolve_update(base_record, request, rules, seed, index)
                updated = _order_headers(project_record(resolved.record, entity.profile), entity)
                validate_update_contract(base_record, updated, request, resolved, rules)
                if request.operation not in {OperationType.MISSING, OperationType.INVALID}:
                    try:
                        validator.validate(updated)
                    except ValidationError as error:
                        raise GenerationError(_validation_detail(error)) from error
                output_file.write(orjson.dumps(updated))
                output_file.write(b"\n")
        return temporary_path.replace(final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _build_record(
    entity: EntityConfig,
    seed: int,
    index: int,
    counts: Mapping[str, int],
    generate_record: Callable[..., dict[str, object]],
) -> dict[str, object]:
    """Build and project one creation record for reuse by update generation."""
    record = generate_record(
        seed, index, counts, entity.client_headers, entity.client_values, entity.profile
    )
    return _order_headers(project_record(record, entity.profile), entity)


def _order_headers(record: dict[str, object], entity: EntityConfig) -> dict[str, object]:
    """Apply the configured root-header order after layout projection."""
    if entity.header_order == "source":
        return record
    headers = {field.name for field in load_layout(entity.profile).headers}.intersection(record)
    ordered_headers = {key: record[key] for key in record if key in headers}
    body = {key: value for key, value in record.items() if key not in headers}
    if entity.header_order == "first":
        return {**ordered_headers, **body}
    if entity.header_order == "last":
        return {**body, **ordered_headers}
    raise GenerationError(f"Unsupported header order {entity.header_order!r}")


def _load_generator(module_name: str) -> Callable[..., dict[str, object]]:
    """Import a configured entity module and return its record generator."""
    try:
        module = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as error:
        raise GenerationError(f"Could not import entity module {module_name!r}") from error
    generate_record = getattr(module, "generate_record", None)
    if not callable(generate_record):
        raise GenerationError(f"Entity module {module_name!r} must expose callable generate_record")
    return cast(Callable[..., dict[str, object]], generate_record)


def _load_validator(schema_path: Path) -> Draft202012Validator:
    """Load the JSON Schema used to validate one emitted record."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GenerationError(f"Could not read entity schema {schema_path}") from error
    return Draft202012Validator(cast(dict[str, Any], schema))


def _validation_detail(error: ValidationError) -> str:
    """Format a concise schema validation error without exposing record data."""
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    return f"Generated record failed schema validation at {path}: failed {error.validator}"
