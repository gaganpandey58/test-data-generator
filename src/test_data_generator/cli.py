"""Coordinate configuration loading and JSONL generation for command-line use.

This module is the intentionally small public boundary around the generator.
It translates domain-specific failures into concise messages that are safe to
show in a terminal, while the configuration and engine modules retain the
details of parsing, validation, and atomic file publication.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from test_data_generator.configuration.config import RunConfig, load_config
from test_data_generator.core.engine import (
    build_claim_pair_records,
    build_entity_records,
    run_claim_pair,
    run_derived_update_records,
    run_entity,
    run_records,
    run_update_entity,
    run_update_records,
)
from test_data_generator.core.errors import ConfigurationError, GenerationError
from test_data_generator.entities.payment import (
    derive_payments_from_claims,
    derive_payments_from_records,
    generate_orphan_payments,
)
from test_data_generator.entities.provider_cdf import (
    build_linked_provider_records,
    generate_linked_provider_fixtures,
    generate_nppes_file,
    generate_provider_cdf,
)
from test_data_generator.update.rules import load_rule_catalog
from test_data_generator.update.scenarios import (
    OperationType,
    UpdateRequest,
    load_invalid_values,
)


class CommandError(RuntimeError):
    """Represent an expected command failure that is safe to display.

    The CLI raises this error only after adding contextual information to a
    configuration or generation failure.  :func:`main` catches it and returns
    the documented non-zero command exit status instead of exposing a stack
    trace to the user.
    """


def generate(config: Path, mode: str = "all") -> None:
    """Generate every enabled entity described by one configuration file.

    The function loads and validates the supplied configuration once, passes
    shared entity-count context to each enabled generator, prints
    the resulting JSONL path, and removes only stale output files belonging to
    disabled known entities.

    Args:
        config: Path to the root JSON generation configuration.
        mode: ``all``, ``creation``, or ``updates``.

    Raises:
        CommandError: If configuration loading or entity generation fails.
    """
    if mode not in {"all", "creation", "updates"}:
        raise CommandError(f"Unknown generation mode {mode!r}")
    _refresh_gdf_schemas()
    try:
        run_config = load_config(config)
    except ConfigurationError as error:
        raise CommandError(f"Configuration failed for {config.resolve()}: {error}") from error

    rules = None
    if mode in {"all", "updates"} and run_config.updates_enabled:
        if run_config.rule_catalog is None:
            raise CommandError("Updates are enabled but no rule_catalog is configured")
        try:
            rules = load_rule_catalog(run_config.rule_catalog)
        except ConfigurationError as error:
            raise CommandError(f"Update rule catalog failed: {error}") from error

    transaction = _begin_output_transaction(run_config, mode)
    run_config = transaction.staged_config
    entity_counts = {entity.name: entity.count for entity in run_config.entities}
    generated_records: dict[str, tuple[Mapping[str, object], ...]] = {}
    if mode in {"all", "creation"} and run_config.creation_enabled:
        histories = {
            entity.name: entity
            for entity in run_config.entities
            if entity.name in {"claim_history_professional", "claim_history_institutional"}
        }
        for entity in run_config.entities:
            if entity.name in histories:
                continue
            history_entity_name = {
                "claim_professional": "claim_history_professional",
                "claim_institutional": "claim_history_institutional",
            }.get(entity.name)
            if history_entity_name is not None:
                history_entity = histories.get(history_entity_name)
                if history_entity is None:
                    raise CommandError(f"Claim stream {entity.name!r} has no Claims History stream")
                try:
                    claim_path, history_path = run_claim_pair(
                        entity,
                        history_entity,
                        run_config.seed,
                        run_config.creation_directory,
                        entity_counts,
                        generated_records,
                    )
                except GenerationError as error:
                    raise CommandError(
                        f"Claim generation failed for entity {entity.name!r}: {error}"
                    ) from error
                print(
                    f"{entity.name}: {entity.count} records -> {transaction.final_path(claim_path)}"
                )
                print(
                    f"{history_entity.name}: {history_entity.count} records -> "
                    f"{transaction.final_path(history_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(claim_path)
                generated_records[history_entity.name] = _read_jsonl_records(history_path)
                continue
            claim_source_name = _payment_claim_history_name(entity.name)
            if claim_source_name in generated_records:
                try:
                    records = derive_payments_from_records(
                        generated_records[claim_source_name],
                        entity.profile,
                        entity.scenarios,
                        run_config.seed,
                        entity.count,
                    )
                    output_path = run_records(entity, records, run_config.creation_directory)
                except (GenerationError, ValueError) as error:
                    raise CommandError(
                        f"Payment generation from Claims failed for entity {entity.name!r}: {error}"
                    ) from error
                print(
                    f"{entity.name}: {entity.count} records -> "
                    f"{transaction.final_path(output_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(output_path)
                continue
            if entity.source_claims is not None:
                try:
                    records = derive_payments_from_claims(
                        entity.source_claims,
                        entity.profile,
                        entity.scenarios,
                        run_config.seed,
                        entity.count,
                    )
                    output_path = run_records(entity, records, run_config.creation_directory)
                except (GenerationError, OSError, ValueError) as error:
                    raise CommandError(
                        f"Payment generation from Claims failed for entity {entity.name!r}: {error}"
                    ) from error
                print(
                    f"{entity.name}: {entity.count} records -> "
                    f"{transaction.final_path(output_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(output_path)
                continue
            if _is_orphan_only_payment(entity.name, entity.scenarios):
                try:
                    records = generate_orphan_payments(
                        entity.profile, entity.count, run_config.seed
                    )
                    output_path = run_records(entity, records, run_config.creation_directory)
                except (GenerationError, ValueError) as error:
                    raise CommandError(
                        "Standalone orphan Payment generation failed for entity "
                        f"{entity.name!r}: {error}"
                    ) from error
                print(
                    f"{entity.name}: {entity.count} records -> "
                    f"{transaction.final_path(output_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(output_path)
                continue
            if (
                entity.name == "provider"
                and run_config.provider_linked
                and run_config.nppes_count > 0
            ):
                try:
                    paths = generate_linked_provider_fixtures(
                        run_config.creation_directory,
                        run_config.nppes_count,
                        entity.count - run_config.nppes_count,
                        run_config.seed,
                        entity.client_headers,
                        entity.client_values,
                        run_config.nppes_individual_count,
                        run_config.nppes_organizational_count,
                        entity.header_order,
                    )
                except (OSError, ValueError) as error:
                    raise CommandError(f"Linked provider generation failed: {error}") from error
                print(
                    f"provider: {entity.count} records -> "
                    f"{transaction.final_path(paths['provider_cdf'])}"
                )
                print(
                    f"provider_nppes: {run_config.nppes_count} records -> "
                    f"{transaction.final_path(paths['provider_nppes'])}"
                )
                generated_records[entity.name] = _read_jsonl_records(paths["provider_cdf"])
                continue
            try:
                output_path = run_entity(
                    entity,
                    run_config.seed,
                    run_config.creation_directory,
                    entity_counts,
                    generated_records,
                )
            except GenerationError as error:
                raise CommandError(
                    f"Generation failed for entity {entity.name!r} using schema "
                    f"{entity.schema}: {error}"
                ) from error
            except Exception as error:
                raise CommandError(
                    f"Generation failed for entity {entity.name!r} using schema {entity.schema}"
                ) from error
            print(f"{entity.name}: {entity.count} records -> {transaction.final_path(output_path)}")
            generated_records[entity.name] = _read_jsonl_records(output_path)
        if run_config.nppes_count > 0 and not run_config.provider_linked:
            try:
                nppes_path = generate_nppes_file(
                    run_config.creation_directory / run_config.nppes_filename,
                    run_config.nppes_count,
                    run_config.seed,
                    run_config.nppes_individual_count,
                    run_config.nppes_organizational_count,
                )
            except (OSError, ValueError) as error:
                raise CommandError(f"NPPES generation failed: {error}") from error
            print(
                f"provider_nppes: {run_config.nppes_count} records -> "
                f"{transaction.final_path(nppes_path)}"
            )
    if mode in {"all", "updates"} and run_config.updates_enabled:
        assert rules is not None
        _materialize_update_bases(run_config, entity_counts, generated_records)
        entities_by_name = {entity.name: entity for entity in run_config.entities}
        propagated_payment_updates: set[str] = set()
        for entity in run_config.entities:
            if entity.name in {"claim_history_professional", "claim_history_institutional"}:
                continue
            # A Claim update derives its related Payment update below. A direct
            # Payment operation remains an independent adjudication fixture.
            if (
                entity.name in {"payment_professional", "payment_institutional"}
                and "operation" not in entity.update
            ):
                continue
            entity_rules = rules.get(entity.name)
            if entity_rules is None:
                raise CommandError(f"Update rule catalog has no rules for {entity.name!r}")
            request = _update_request(run_config, entity)
            try:
                if entity.name in generated_records:
                    output_path = run_update_records(
                        entity,
                        generated_records[entity.name],
                        run_config.seed,
                        run_config.update_directory,
                        request,
                        entity_rules,
                    )
                elif _payment_claim_history_name(entity.name) in generated_records:
                    records = derive_payments_from_records(
                        generated_records[_payment_claim_history_name(entity.name)],
                        entity.profile,
                        entity.scenarios,
                        run_config.seed,
                        entity.count,
                    )
                    output_path = run_update_records(
                        entity,
                        records,
                        run_config.seed,
                        run_config.update_directory,
                        request,
                        entity_rules,
                    )
                elif entity.source_claims is not None:
                    records = derive_payments_from_claims(
                        entity.source_claims,
                        entity.profile,
                        entity.scenarios,
                        run_config.seed,
                        entity.count,
                    )
                    output_path = run_update_records(
                        entity,
                        records,
                        run_config.seed,
                        run_config.update_directory,
                        request,
                        entity_rules,
                    )
                elif _is_orphan_only_payment(entity.name, entity.scenarios):
                    records = generate_orphan_payments(
                        entity.profile, entity.count, run_config.seed
                    )
                    output_path = run_update_records(
                        entity,
                        records,
                        run_config.seed,
                        run_config.update_directory,
                        request,
                        entity_rules,
                    )
                else:
                    output_path = run_update_entity(
                        entity,
                        run_config.seed,
                        run_config.update_directory,
                        entity_counts,
                        request,
                        entity_rules,
                        generated_records,
                    )
            except (GenerationError, ValueError) as error:
                raise CommandError(
                    f"Update generation failed for entity {entity.name!r}: {error}"
                ) from error
            print(f"{entity.name}: {entity.count} updates -> {transaction.final_path(output_path)}")
            if entity.name in {"claim_professional", "claim_institutional"}:
                history_name = {
                    "claim_professional": "claim_history_professional",
                    "claim_institutional": "claim_history_institutional",
                }[entity.name]
                payment_name = {
                    "claim_professional": "payment_professional",
                    "claim_institutional": "payment_institutional",
                }[entity.name]
                history_entity = entities_by_name.get(history_name)
                payment_entity = entities_by_name.get(payment_name)
                if history_entity is None:
                    raise CommandError(f"Claim stream {entity.name!r} has no Claims History stream")
                try:
                    history_bases = generated_records[history_name]
                    history_path = run_update_records(
                        history_entity,
                        history_bases,
                        run_config.seed,
                        run_config.update_directory,
                        request,
                        entity_rules,
                    )
                    updated_history = _read_jsonl_records(history_path)
                    changed_history_fields = tuple(
                        _changed_field_names(base, updated)
                        for base, updated in zip(history_bases, updated_history, strict=True)
                    )
                    generated_records[history_name] = updated_history
                    print(
                        f"{history_entity.name}: {history_entity.count} updates -> "
                        f"{transaction.final_path(history_path)}"
                    )
                    if payment_entity is None:
                        continue
                    payment_records = derive_payments_from_records(
                        updated_history,
                        payment_entity.profile,
                        payment_entity.scenarios,
                        run_config.seed,
                        payment_entity.count,
                        changed_history_fields,
                    )
                    payment_path = run_derived_update_records(
                        payment_entity,
                        payment_records,
                        run_config.update_directory,
                        validate_schema=request.operation
                        not in {
                            OperationType.MISSING,
                            OperationType.EMPTY,
                            OperationType.INVALID,
                        },
                    )
                except (GenerationError, ValueError) as error:
                    raise CommandError(
                        f"Claim update propagation failed for entity {entity.name!r}: {error}"
                    ) from error
                generated_records[payment_name] = tuple(payment_records)
                propagated_payment_updates.add(payment_name)
                print(
                    f"{payment_entity.name}: {payment_entity.count} updates -> "
                    f"{transaction.final_path(payment_path)}"
                )
        _remove_unrequested_payment_updates(run_config, propagated_payment_updates)
    _remove_disabled_outputs(run_config)
    transaction.commit()


@dataclass
class _OutputTransaction:
    """Stage requested streams and publish their directories as one unit."""

    temporary: tempfile.TemporaryDirectory[str]
    staged_config: RunConfig
    directory_pairs: tuple[tuple[Path, Path], ...]

    def __del__(self) -> None:
        """Clean abandoned staging after a generation exception."""
        self.temporary.cleanup()

    def final_path(self, staged_path: Path) -> Path:
        """Translate a staged path to the durable path reported to users."""
        for staged_directory, final_directory in self.directory_pairs:
            try:
                relative = staged_path.resolve().relative_to(staged_directory.resolve())
            except ValueError:
                continue
            return final_directory / relative
        return staged_path

    def commit(self) -> None:
        """Swap staged directories into place and roll back a failed swap."""
        completed: list[tuple[Path, Path | None]] = []
        temporary_root = Path(self.temporary.name)
        try:
            for index, (staged, final) in enumerate(self.directory_pairs):
                final.parent.mkdir(parents=True, exist_ok=True)
                backup: Path | None = None
                if final.exists():
                    backup = temporary_root / f"backup-{index}"
                    final.replace(backup)
                completed.append((final, backup))
                staged.replace(final)
        except Exception:
            for final, backup in reversed(completed):
                if final.is_dir():
                    shutil.rmtree(final)
                elif final.exists() or final.is_symlink():
                    final.unlink()
                if backup is not None:
                    backup.replace(final)
            raise
        finally:
            self.temporary.cleanup()


def _begin_output_transaction(run_config: RunConfig, mode: str) -> _OutputTransaction:
    """Copy current output into a same-filesystem run staging area."""
    run_config.output_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
        prefix=".test-data-generator-", dir=run_config.output_directory.parent
    )
    temporary_root = Path(temporary.name)
    staged_creation = temporary_root / "creation"
    staged_updates = temporary_root / "updates"
    for source, staged in (
        (run_config.creation_directory, staged_creation),
        (run_config.update_directory, staged_updates),
    ):
        if source.is_dir():
            shutil.copytree(source, staged)
        else:
            staged.mkdir(parents=True)
    staged_config = replace(
        run_config,
        output_directory=temporary_root / "legacy",
        creation_directory=staged_creation,
        update_directory=staged_updates,
    )
    pairs: list[tuple[Path, Path]] = []
    if mode in {"all", "creation"} and run_config.creation_enabled:
        pairs.append((staged_creation, run_config.creation_directory))
    if mode in {"all", "updates"} and run_config.updates_enabled:
        pairs.append((staged_updates, run_config.update_directory))
    return _OutputTransaction(temporary, staged_config, tuple(pairs))


def _is_orphan_only_payment(name: str, scenarios: Mapping[str, int]) -> bool:
    """Return whether a Payment stream needs no Claim-backed source records."""
    return (
        name in {"payment_professional", "payment_institutional"}
        and scenarios.get("ORPHAN", 0) > 0
        and all(scenario == "ORPHAN" or count == 0 for scenario, count in scenarios.items())
    )


def _read_jsonl_records(path: Path) -> tuple[Mapping[str, object], ...]:
    """Read validated records back into the run-scoped relationship registry."""
    records: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CommandError(f"Generated JSONL record in {path} is not an object")
            records.append(value)
    return tuple(records)


def _changed_field_names(
    original: Mapping[str, object], updated: Mapping[str, object]
) -> frozenset[str]:
    """Return field names changed between paired source records, including lines."""
    absent = object()
    changed: set[str] = set()
    for field in set(original).union(updated):
        old = original.get(field, absent)
        new = updated.get(field, absent)
        if old is absent or new is absent:
            changed.add(field)
        elif isinstance(old, Mapping) and isinstance(new, Mapping):
            changed.update(_changed_field_names(old, new))
        elif isinstance(old, list) and isinstance(new, list):
            if len(old) != len(new):
                changed.add(field)
            for old_item, new_item in zip(old, new, strict=False):
                if isinstance(old_item, Mapping) and isinstance(new_item, Mapping):
                    changed.update(_changed_field_names(old_item, new_item))
                elif old_item != new_item:
                    changed.add(field)
        elif old != new:
            changed.add(field)
    return frozenset(changed)


def _payment_claim_history_name(name: str) -> str:
    """Return the in-run Claims History source for a Payment stream."""
    return {
        "payment_professional": "claim_history_professional",
        "payment_institutional": "claim_history_institutional",
    }.get(name, "")


def _materialize_update_bases(
    run_config: RunConfig,
    entity_counts: Mapping[str, int],
    generated_records: dict[str, tuple[Mapping[str, object], ...]],
) -> None:
    """Build exact update bases in memory when creation output is disabled."""
    histories = {
        entity.name: entity
        for entity in run_config.entities
        if entity.name in {"claim_history_professional", "claim_history_institutional"}
    }
    for entity in run_config.entities:
        if entity.name in generated_records or entity.name in histories:
            continue
        if entity.name in {"payment_professional", "payment_institutional"}:
            continue
        if entity.name == "provider" and run_config.provider_linked:
            records = build_linked_provider_records(
                run_config.nppes_count,
                entity.count - run_config.nppes_count,
                run_config.seed,
                entity.client_headers,
                entity.client_values,
                run_config.nppes_individual_count,
                run_config.nppes_organizational_count,
                entity.header_order,
            )
            generated_records[entity.name] = tuple(records["provider_cdf"])
            continue
        history_name = {
            "claim_professional": "claim_history_professional",
            "claim_institutional": "claim_history_institutional",
        }.get(entity.name)
        if history_name is not None:
            current, history = build_claim_pair_records(
                entity, run_config.seed, entity_counts, generated_records
            )
            generated_records[entity.name] = tuple(current)
            generated_records[history_name] = tuple(history)
            continue
        generated_records[entity.name] = tuple(
            build_entity_records(entity, run_config.seed, entity_counts, generated_records)
        )


def _update_request(run_config: RunConfig, entity: object) -> UpdateRequest:
    """Resolve global and entity-specific update settings into one request."""
    entity_config = entity
    raw = dict(run_config.update_defaults)
    global_selection = raw.get("field_selection")
    if isinstance(global_selection, dict):
        raw.update(global_selection)
    entity_update = getattr(entity_config, "update", {})
    if isinstance(entity_update, dict):
        raw.update(entity_update)
    operation_config = raw.get("operation")
    if not isinstance(operation_config, dict):
        raise CommandError("Updates require an operation object")
    try:
        operation_type = OperationType(str(operation_config.get("type", "")))
    except ValueError as error:
        raise CommandError("Unknown update operation") from error
    fields = _string_tuple(operation_config, "fields")
    operation_condition = (
        str(operation_config["condition"]) if "condition" in operation_config else None
    )
    include = _string_tuple(raw, "include")
    exclude = _string_tuple(raw, "exclude")
    threshold = raw.get("threshold")
    try:
        parsed_threshold = Decimal(str(threshold)) if threshold is not None else None
    except (InvalidOperation, ValueError) as error:
        raise CommandError("Update threshold must be a decimal number") from error
    return UpdateRequest(
        fields=fields,
        include=include,
        exclude=exclude,
        matching_method=str(raw["matching_method"]) if "matching_method" in raw else None,
        threshold=parsed_threshold,
        operation=operation_type,
        condition=operation_condition,
        invalid_values=(
            load_invalid_values(run_config.invalid_values_catalog)
            if operation_type == OperationType.INVALID
            and run_config.invalid_values_catalog is not None
            else None
        ),
    )


def _string_tuple(values: dict[str, object], key: str) -> tuple[str, ...]:
    """Read an optional string-list setting from normalized configuration."""
    value = values.get(key, ())
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(
        item.strip() for value_item in value for item in value_item.split(",") if item.strip()
    )


def _remove_unrequested_payment_updates(run_config: RunConfig, propagated: set[str]) -> None:
    """Remove stale 835 updates unless explicitly or Claim-derived requested."""
    for entity in run_config.entities:
        if entity.name not in {"payment_professional", "payment_institutional"}:
            continue
        if "operation" in entity.update or entity.name in propagated:
            continue
        path = run_config.update_directory / entity.filename.removesuffix(".jsonl")
        path = path.with_suffix(".update.jsonl")
        if path.is_file() or path.is_symlink():
            path.unlink()


def _remove_disabled_outputs(run_config: RunConfig) -> None:
    """Remove stale files for disabled known entities after a successful run.

    Only filenames resolved and validated from the supplied configuration are
    considered, and this happens only after all enabled entities have been
    generated successfully. Unrelated files in the configured output directory
    remain untouched.

    Args:
        run_config: Loaded generation configuration carrying disabled filenames.
    """
    output_directory = run_config.output_directory
    enabled_filenames = {entity.filename for entity in run_config.entities}
    for filename in run_config.disabled_filenames:
        if filename in enabled_filenames:
            continue
        update_filename = filename.removesuffix(".jsonl") + ".update.jsonl"
        paths = (
            output_directory / filename,
            run_config.creation_directory / filename,
            run_config.update_directory / update_filename,
        )
        for path in paths:
            if path.is_file() or path.is_symlink():
                path.unlink()


def main() -> int:
    """Parse command-line arguments and execute the generation command.

    This function is kept separate from :func:`generate` so library callers do
    not need to depend on ``argparse`` or process exit codes.  Unexpected
    failures intentionally receive a generic message to avoid leaking internal
    details in a normal CLI run.

    Returns:
        Zero after successful generation or two for a safe user-facing error.
    """
    parser = argparse.ArgumentParser(description="Generate configured healthcare test data.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate_parser = subcommands.add_parser(
        "generate", help="Generate enabled entity JSONL files."
    )
    generate_parser.add_argument("--config", required=True, type=Path)
    generate_parser.add_argument("--mode", choices=("all", "creation", "updates"), default="all")
    provider_cdf_parser = subcommands.add_parser(
        "provider-cdf", help="Generate code-defined NPPES and provider CDF fixtures."
    )
    provider_cdf_parser.add_argument("--output", required=True, type=Path)
    provider_cdf_parser.add_argument("--count", type=int, default=10)
    provider_cdf_parser.add_argument("--unmatched-count", type=int, default=2)
    provider_cdf_parser.add_argument("--seed", type=int, default=20260805)
    arguments = parser.parse_args()

    try:
        if arguments.command == "provider-cdf":
            paths = generate_provider_cdf(
                arguments.output,
                arguments.count,
                arguments.unmatched_count,
                arguments.seed,
            )
            for name, path in paths.items():
                print(f"{name}: {path}")
        else:
            generate(arguments.config, arguments.mode)
    except CommandError as error:
        print(error, file=sys.stderr)
        return 2
    except Exception:
        print("Generation failed", file=sys.stderr)
        return 2
    return 0


def run_default() -> int:
    """Generate using the repository's standard ``generator.config.json`` file.

    This is the short console command installed as ``generate-data``. It keeps
    normal use to one command while ``main`` remains available for an optional
    alternate configuration path.
    """
    try:
        generate(Path("generator.config.json"))
    except CommandError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


def _refresh_gdf_schemas() -> None:
    """Synchronize schemas with the newest workbook under ``schema/gdf``.

    Replacing the existing workbook or adding a newer ``.xlsx`` file is enough:
    the next generation run refreshes available GDF properties before records
    are produced. Layouts still decide which fields appear in JSONL.
    """
    project_root = Path(__file__).resolve().parents[2]
    gdf_directory = project_root / "schema" / "gdf"
    workbooks = sorted(gdf_directory.glob("*.xlsx"), key=lambda path: path.stat().st_mtime)
    if not workbooks:
        return
    command = [
        sys.executable,
        str(project_root / "schema" / "tools" / "extract-gdf-catalogs.py"),
        str(workbooks[-1]),
    ]
    result = subprocess.run(command, cwd=project_root, check=False, capture_output=True, text=True)
    if result.returncode:
        raise CommandError(
            "GDF schema refresh failed. Check schema/gdf for a valid Excel workbook."
        )
