"""Generate, validate, and atomically publish layout-shaped JSONL files."""

import importlib
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


def run_entity(
    entity: EntityConfig, seed: int, output_directory: Path, counts: Mapping[str, int]
) -> Path:
    """Generate one configured JSONL stream and publish it atomically.

    All entity modules use the same small call contract: seed, row index,
    enabled counts, profile headers, profile values, and layout name. The
    engine does not inspect signatures or plan variations because current
    generation is happy-path only.
    """
    final_path = resolve_output_path(output_directory, entity.filename)
    temporary_path: Path | None = None
    try:
        generate_record = _load_generator(entity.module)
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
                record = generate_record(
                    seed, index, counts, entity.client_headers, entity.client_values, entity.profile
                )
                record = project_record(record, entity.profile)
                try:
                    validator.validate(record)
                except ValidationError as error:
                    raise GenerationError(_validation_detail(error)) from error
                output_file.write(orjson.dumps(record))
                output_file.write(b"\n")
        return temporary_path.replace(final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


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
