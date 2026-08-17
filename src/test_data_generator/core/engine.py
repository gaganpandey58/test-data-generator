"""Generate, validate, and atomically publish layout-shaped JSONL files."""

import hashlib
import importlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import orjson
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from test_data_generator.configuration.config import EntityConfig, resolve_output_path
from test_data_generator.core.errors import GenerationError
from test_data_generator.layouts import project_record
from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import UpdateRequest, resolve_update
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
                record = _build_record(entity, seed, index, counts, generate_record)
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


def run_update_entity(
    entity: EntityConfig,
    seed: int,
    output_directory: Path,
    counts: Mapping[str, int],
    request: UpdateRequest,
    rules: EntityRules,
) -> tuple[Path, Path]:
    """Generate update JSONL and a paired explainability manifest atomically."""
    update_filename = entity.filename.removesuffix(".jsonl") + ".update.jsonl"
    final_path = resolve_output_path(output_directory, update_filename)
    manifest_path = final_path.with_suffix(".manifest.jsonl")
    temporary_path: Path | None = None
    temporary_manifest: Path | None = None
    try:
        generate_record = _load_generator(entity.module)
        validator = _load_validator(entity.schema)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{final_path.name}.",
                suffix=".tmp",
                dir=final_path.parent,
                delete=False,
            ) as output_file,
            tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{manifest_path.name}.",
                suffix=".tmp",
                dir=final_path.parent,
                delete=False,
            ) as manifest_file,
        ):
            temporary_path = Path(output_file.name)
            temporary_manifest = Path(manifest_file.name)
            for index in range(entity.count):
                base = _build_record(entity, seed, index, counts, generate_record)
                resolved = resolve_update(base, request, rules, seed, index)
                updated = project_record(resolved.record, entity.profile)
                validate_update_contract(base, updated, request, resolved, rules)
                missing_scenarios = {
                    "MISSING_REQUIRED_FIELD",
                    "MISSING_MULTIPLE_FIELDS",
                    "MISSING_SELECTED_FIELDS",
                }
                if request.scenario.value not in missing_scenarios:
                    try:
                        validator.validate(updated)
                    except ValidationError as error:
                        raise GenerationError(_validation_detail(error)) from error
                output_file.write(orjson.dumps(updated))
                output_file.write(b"\n")
                manifest_file.write(
                    orjson.dumps(
                        {
                            "base_record_id": f"{entity.name}-{index}",
                            "update_record_id": f"{entity.name}-{index}-update",
                            "entity": entity.name,
                            "profile": entity.profile,
                            "scenario": request.scenario.value,
                            "changed_fields": resolved.changed_fields,
                            "removed_fields": resolved.removed_fields,
                            "invalidated_keys": resolved.invalidated_keys,
                            "changed_weight": str(resolved.total_weight),
                            "threshold_relation": resolved.threshold_relation,
                            "expected_match": resolved.expected_match,
                            "expected_apply": resolved.expected_apply,
                            "catalog_version": rules.catalog_version,
                            "base_record_hash": hashlib.sha256(
                                orjson.dumps(base, option=orjson.OPT_SORT_KEYS)
                            ).hexdigest(),
                            "changed_field_weights": {
                                field: str(rules.fields[field].weight)
                                for field in resolved.changed_fields
                            },
                        }
                    )
                )
                manifest_file.write(b"\n")
        temporary_path.replace(final_path)
        temporary_manifest.replace(manifest_path)
        return final_path, manifest_path
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if temporary_manifest is not None:
            temporary_manifest.unlink(missing_ok=True)
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
    return project_record(record, entity.profile)


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
