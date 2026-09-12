"""Generate, validate, and atomically publish layout-shaped JSONL files."""

import importlib
import json
import tempfile
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import orjson
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]

from test_data_generator.configuration.config import EntityConfig, resolve_output_path
from test_data_generator.core.errors import GenerationError
from test_data_generator.entities.provider_nppes import validate_record as validate_nppes_record
from test_data_generator.layouts import load_layout, project_record
from test_data_generator.update.rules import EntityRules
from test_data_generator.update.scenarios import (
    ExpectedOutcome,
    FailureMode,
    OperationType,
    ResolvedUpdate,
    UpdateRequest,
    may_violate_schema,
    resolve_update,
)
from test_data_generator.update.validation import validate_update_contract

_CLAIM_HISTORY_IDENTIFIER_FIELDS = (
    "CH_CLIENT_CLAIM_UNIQUE_ID",
    "CH_CLIENT_CLAIM_ID",
    "CH_CLIENT_ORIGINAL_CLAIM_ID",
)


def run_entity(
    entity: EntityConfig,
    seed: int,
    output_directory: Path,
    counts: Mapping[str, int],
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
) -> Path:
    """Generate one configured JSONL stream and publish it atomically.

    All entity modules use the same small call contract: seed, row index,
    enabled counts, profile headers, profile values, and layout name. The
    engine does not inspect signatures or plan variations because current
    generation is happy-path only.
    """
    records = build_entity_records(entity, seed, counts, related_records)
    return run_records(entity, records, output_directory)


def build_entity_records(
    entity: EntityConfig,
    seed: int,
    counts: Mapping[str, int],
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
) -> list[dict[str, object]]:
    """Materialize one entity stream without publishing files."""
    generate_record = _load_generator(entity.module)
    records = [
        _build_record(entity, seed, index, counts, generate_record, related_records)
        for index in range(entity.count)
    ]
    # Current 837 Claim output always exposes empty client claim identifiers.
    # Paired CH construction intentionally bypasses this post-processing so it
    # can retain the generated identifiers for the corresponding History row.
    if entity.name in {"claim_professional", "claim_institutional"}:
        for record in records:
            _blank_current_claim_identifiers(record)
    return records


def build_related_records(
    entity: EntityConfig, source_records: Iterable[Mapping[str, object]]
) -> list[dict[str, object]]:
    """Derive a configured related stream from already-generated source rows.

    The Member Roster stream uses this path to reuse the exact emitted 834
    Member rows.  It changes only the configured file-type discriminator before
    any separately configured update operation is applied.
    """
    records = list(source_records)
    if len(records) < entity.count:
        raise GenerationError(
            f"Related entity {entity.name!r} requires {entity.count} source records, "
            f"but only {len(records)} are available"
        )
    result: list[dict[str, object]] = []
    for source in records[: entity.count]:
        derived = deepcopy(dict(source))
        if entity.file_type is not None:
            derived["FILE_TYPE"] = entity.file_type
        result.append(_order_headers(project_record(derived, entity.profile), entity))
    return result


def run_claim_pair(
    claim_entity: EntityConfig,
    history_entity: EntityConfig,
    seed: int,
    output_directory: Path,
    counts: Mapping[str, int],
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
) -> tuple[Path, Path]:
    """Publish paired current Claim and Claims History records from one base row.

    History retains the generated client claim identifiers. The corresponding
    current Claim retains the same complete record but exposes those three
    declared identifiers as empty values.
    """
    claim_records, history_records = build_claim_pair_records(
        claim_entity, seed, counts, related_records
    )
    claim_path = run_records(claim_entity, claim_records, output_directory)
    history_path = run_records(history_entity, history_records, output_directory)
    return claim_path, history_path


def build_claim_pair_records(
    claim_entity: EntityConfig,
    seed: int,
    counts: Mapping[str, int],
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Materialize current and History Claims from each shared base record."""
    generate_record = _load_generator(claim_entity.module)
    history_records: list[dict[str, object]] = []
    claim_records: list[dict[str, object]] = []
    for index in range(claim_entity.count):
        history_record = _build_record(
            claim_entity, seed, index, counts, generate_record, related_records
        )
        current_claim = deepcopy(history_record)
        _blank_current_claim_identifiers(current_claim)
        claim_records.append(current_claim)
        # Claims History is the existing-claim (CH) stream. It shares the
        # Claim layout and business attributes with its paired 837 record but
        # has its own envelope file type.
        history_record["FILE_TYPE"] = "CH"
        history_records.append(history_record)
    return claim_records, history_records


def run_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    output_directory: Path,
) -> Path:
    """Validate and atomically publish already-derived records for one entity."""
    return _publish_records(
        entity,
        records,
        output_directory,
        entity.filename,
        validate_schema=True,
        ingestion_date=entity.ingestion_date,
    )


def run_derived_update_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    output_directory: Path,
    *,
    validate_schema: bool,
) -> Path:
    """Atomically publish records derived from an already-resolved parent update."""
    filename = entity.filename.removesuffix(".jsonl") + ".update.jsonl"
    return _publish_records(
        entity,
        records,
        output_directory,
        filename,
        validate_schema,
        entity.update_ingestion_date,
    )


def _publish_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    output_directory: Path,
    filename: str,
    validate_schema: bool,
    ingestion_date: str,
) -> Path:
    """Write one record stream atomically using its declared schema when required."""
    final_path = resolve_output_path(output_directory, filename)
    temporary_path: Path | None = None
    try:
        validator = None if entity.name == "provider_nppes" else _load_validator(entity.schema)
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
                normalized["INGESTION_DATE"] = ingestion_date
                _ensure_claim_history_identifiers(entity, normalized)
                if validate_schema:
                    _validate_emitted_record(entity, normalized, validator)
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
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
    resolved_updates: list[ResolvedUpdate] | None = None,
) -> Path:
    """Generate update JSONL atomically without sidecar metadata files."""
    records = build_entity_records(entity, seed, counts, related_records)
    return run_update_records(
        entity, records, seed, output_directory, request, rules, resolved_updates
    )


def run_update_records(
    entity: EntityConfig,
    records: Iterable[Mapping[str, object]],
    seed: int,
    output_directory: Path,
    request: UpdateRequest,
    rules: EntityRules,
    resolved_updates: list[ResolvedUpdate] | None = None,
    *,
    validate_schema: bool = True,
) -> Path:
    """Apply the shared update engine to already-derived base records."""
    update_filename = entity.filename.removesuffix(".jsonl") + ".update.jsonl"
    final_path = resolve_output_path(output_directory, update_filename)
    temporary_path: Path | None = None
    try:
        validator = None if entity.name == "provider_nppes" else _load_validator(entity.schema)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            dir=final_path.parent,
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            written = 0
            for index, base in enumerate(records):
                base_record = dict(base)
                resolved = resolve_update(base_record, request, rules, seed, index)
                if (
                    not resolved.changed_fields
                    and not resolved.removed_fields
                    and not _allows_unchanged_result(request)
                ):
                    if rules.catalog_version == "code-defined" or rules.allow_absent_fields:
                        continue
                    raise GenerationError("Requested update did not change an applicable field")
                reconciled = (
                    resolved.record
                    if may_violate_schema(request)
                    else _reconcile_update_record(entity, resolved.record, resolved.changed_fields)
                )
                if reconciled is not resolved.record:
                    resolved = replace(resolved, record=reconciled)
                updated = (
                    deepcopy(resolved.record)
                    if entity.name == "provider_nppes"
                    else _order_headers(project_record(resolved.record, entity.profile), entity)
                )
                if entity.name in {"claim_professional", "claim_institutional"}:
                    for field in _CLAIM_HISTORY_IDENTIFIER_FIELDS:
                        updated[field] = ""
                validate_update_contract(base_record, updated, request, resolved, rules)
                # A configured recency date is the default for every update,
                # but an explicit field-level INGESTION_DATE scenario must
                # remain observable (including EMPTY/MISSING negative cases).
                if "INGESTION_DATE" not in set(resolved.changed_fields).union(
                    resolved.removed_fields
                ):
                    updated["INGESTION_DATE"] = entity.update_ingestion_date
                _ensure_claim_history_identifiers(entity, updated)
                schema_invalid_match_fixture = (
                    request.expected_outcome == ExpectedOutcome.NO_MATCH
                    and request.failure_mode
                    in {FailureMode.INVALID_VALUE, FailureMode.MISSING_VALUE}
                )
                if (
                    validate_schema
                    and not may_violate_schema(request)
                    and not schema_invalid_match_fixture
                ):
                    _validate_emitted_record(entity, updated, validator)
                output_file.write(orjson.dumps(updated))
                output_file.write(b"\n")
                written += 1
                if resolved_updates is not None:
                    resolved_updates.append(resolved)
            if written == 0:
                raise GenerationError("Requested operation is not applicable to any source record")
        return temporary_path.replace(final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _allows_unchanged_result(request: UpdateRequest) -> bool:
    """Return whether an unchanged record is the explicit fixture contract."""
    if request.modifications:
        return all(item.operation == OperationType.DUPLICATE for item in request.modifications)
    return request.operation == OperationType.DUPLICATE


def _reconcile_update_record(
    entity: EntityConfig, record: dict[str, object], changed_fields: tuple[str, ...]
) -> dict[str, object]:
    """Apply entity business invariants after the generic field mutation pass."""
    if entity.name in {"claim_professional", "claim_institutional"}:
        from test_data_generator.entities.claim import reconcile_financials as reconcile_claim

        return reconcile_claim(record, frozenset(changed_fields))
    if entity.name in {"payment_professional", "payment_institutional"}:
        from test_data_generator.entities.payment import reconcile_financials as reconcile_payment

        return reconcile_payment(record, entity.profile, frozenset(changed_fields))
    return record


def _validate_emitted_record(
    entity: EntityConfig,
    record: Mapping[str, object],
    validator: Draft202012Validator | None,
) -> None:
    """Validate ordinary JSON-schema streams and subtype-specific NPPES rows."""
    if entity.name == "provider_nppes":
        try:
            validate_nppes_record(record)
        except ValueError as error:
            raise GenerationError(str(error)) from error
        return
    assert validator is not None
    try:
        validator.validate(record)
    except ValidationError as error:
        raise GenerationError(_validation_detail(error)) from error


def _build_record(
    entity: EntityConfig,
    seed: int,
    index: int,
    counts: Mapping[str, int],
    generate_record: Callable[..., dict[str, object]],
    related_records: Mapping[str, tuple[Mapping[str, object], ...]] | None = None,
) -> dict[str, object]:
    """Build and project one creation record for reuse by update generation."""
    arguments: tuple[object, ...] = (
        seed,
        index,
        counts,
        entity.client_headers,
        entity.client_values,
        entity.profile,
    )
    if entity.claim_lifecycles:
        record = generate_record(*(arguments + (entity.claim_lifecycles[index], related_records)))
    elif entity.name.startswith("claim_"):
        record = generate_record(*(arguments + (None, related_records)))
    else:
        record = generate_record(*arguments)
    if entity.file_type is not None:
        record["FILE_TYPE"] = entity.file_type
    return _order_headers(project_record(record, entity.profile), entity)


def _ensure_claim_history_identifiers(entity: EntityConfig, record: Mapping[str, object]) -> None:
    """Keep the three CH client identifiers populated in every History row."""
    if entity.name not in {"claim_history_professional", "claim_history_institutional"}:
        return
    missing = next(
        (
            field
            for field in _CLAIM_HISTORY_IDENTIFIER_FIELDS
            if not isinstance(record.get(field), str) or not str(record[field]).strip()
        ),
        None,
    )
    if missing is not None:
        raise GenerationError(
            f"Claims History field {missing!r} must be populated; CH identifiers cannot be empty, "
            "missing, or invalid"
        )


def _blank_current_claim_identifiers(record: dict[str, object]) -> None:
    """Apply the current-837 contract for client Claim identifier fields."""
    for field in _CLAIM_HISTORY_IDENTIFIER_FIELDS:
        record[field] = ""


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
    # Polymorphic source streams may retain a smaller public generator while
    # exposing an adapter for the shared six-argument entity-engine contract.
    generate_record = getattr(module, "generate_entity_record", None)
    if generate_record is None:
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
